"""Offline CLI and artifact acceptance checks; no Freya or model calls."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import builder  # noqa: E402
import freya  # noqa: E402
from qa import validate_workflow  # noqa: E402


class BuilderAcceptanceTests(unittest.TestCase):
    def test_model_trace_records_model_and_purpose_without_prompt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btb-model-trace-") as directory:
            trace_path = Path(directory) / "trace.jsonl"
            started = time.monotonic()
            trace = builder.Trace(trace_path, started)
            envelope = {"structured_output": {"global_rules": "ok"},
                        "usage": {"input_tokens": 12, "output_tokens": 3}}
            finished = subprocess.CompletedProcess(args=["claude"], returncode=0,
                                                   stdout=json.dumps(envelope), stderr="")
            with mock.patch.object(builder.shutil, "which", side_effect=lambda name: "/fake/claude" if name == "claude" else None), \
                 mock.patch.object(builder.subprocess, "run", return_value=finished):
                caller = builder._model_caller(trace, started)
                self.assertIsNotNone(caller)
                self.assertEqual({"global_rules": "ok"}, caller("private prompt text", purpose="compile bank brief"))
            trace_text = trace_path.read_text(encoding="utf-8")
            self.assertNotIn("private prompt text", trace_text)
            rows = [json.loads(line) for line in trace_text.splitlines()]
            self.assertEqual(["model_call_start", "model_call_end"], [row["event"] for row in rows])
            self.assertEqual("compile bank brief", rows[0]["purpose"])
            self.assertEqual(rows[0]["model"], rows[1]["model"])
            self.assertEqual("ok", rows[1]["status"])

    def test_one_command_no_live_writes_reproducible_a_and_b_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btb-cli-") as directory:
            out = Path(directory) / "out"
            # An empty PATH makes Claude CLI unavailable. Calling the Python
            # interpreter by absolute path keeps this a true offline CLI run.
            env = os.environ.copy()
            env["PATH"] = ""
            for key in ("FREYA_API_KEY", "FREYA_BANK_TEAM_KEY", "ANTHROPIC_API_KEY"):
                env.pop(key, None)
            for slot, briefs in (
                ("public-a", ["brief-a.md"]),
                ("public-b", ["brief-a.md", "brief-b.md"]),
            ):
                command = [sys.executable, str(ROOT / "builder.py")]
                for brief in briefs:
                    command += ["--brief", str(ROOT / "briefs" / brief)]
                command += ["--openapi", str(ROOT / "tools" / "openapi.json"),
                            "--slot", slot, "--out", str(out), "--no-live"]
                completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                                           text=True, timeout=30, check=False)
                self.assertEqual(0, completed.returncode, completed.stderr)
                result = json.loads(completed.stdout)
                for key in ("policy", "local_workflow", "workflow", "trace"):
                    self.assertTrue(Path(result[key]).is_file(), key)
                policy = json.loads(Path(result["policy"]).read_text(encoding="utf-8"))
                self.assertIn("SOURCE: brief-a.md", policy["raw_brief"])
                if slot == "public-b":
                    self.assertIn("SOURCE: brief-b.md", policy["raw_brief"])
                    self.assertLess(policy["raw_brief"].index("SOURCE: brief-a.md"),
                                    policy["raw_brief"].index("SOURCE: brief-b.md"))
                workflow = json.loads(Path(result["workflow"]).read_text(encoding="utf-8"))
                openapi = json.loads((ROOT / "tools" / "openapi.json").read_text(encoding="utf-8"))
                self.assertEqual([], validate_workflow(workflow, openapi))
                events = [json.loads(line) for line in Path(result["trace"]).read_text(encoding="utf-8").splitlines()]
                self.assertEqual("start", events[0]["event"])
                self.assertEqual("end", events[-1]["event"])
                self.assertEqual("ok", events[-1]["status"])
                self.assertIn("workflow_written", {row["event"] for row in events})
                self.assertIn("model_unavailable", {row["event"] for row in events})
                self.assertIn("test_skipped", {row["event"] for row in events})
                self.assertIn("freya_skipped", {row["event"] for row in events})

    def test_live_path_exports_freya_version_and_trace_without_network(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btb-export-") as directory:
            out = Path(directory) / "out"
            secret = "fak_OFFLINE_TEST_SECRET_123456789"
            args = argparse.Namespace(
                brief=[str(ROOT / "briefs" / "brief-a.md")],
                openapi=str(ROOT / "tools" / "openapi.json"),
                slot="offline-export", out=str(out), no_live=False, no_test=True,
            )

            def fake_export(workflow_id: str, path: str) -> str:
                self.assertEqual("wf_fake", workflow_id)
                local = json.loads((out / "offline-export.local.workflow.json").read_text(encoding="utf-8"))
                Path(path).write_text(json.dumps({"workflow_id": workflow_id,
                                                  "agent_id": "ag_fake", "workflow": local}), encoding="utf-8")
                return path

            with mock.patch.dict(os.environ, {"FREYA_API_KEY": secret}, clear=False), \
                 mock.patch.object(builder, "_model_caller", return_value=None), \
                 mock.patch.object(freya, "validate_workflow", return_value={"valid": True}) as dry_run, \
                 mock.patch.object(freya, "save_workflow", return_value={"workflow_id": "wf_fake", "agent_id": "ag_fake", "version": 7}) as save, \
                 mock.patch.object(freya, "export_workflow", side_effect=fake_export) as export:
                result = builder.run(args)

            self.assertEqual("wf_fake", result["workflow_id"])
            self.assertEqual("ag_fake", result["agent_id"])
            self.assertEqual(7, result["version"])
            self.assertTrue(dry_run.called)
            self.assertTrue(save.called)
            self.assertTrue(export.called)
            exported = json.loads(Path(result["workflow"]).read_text(encoding="utf-8"))
            self.assertEqual("wf_fake", exported["workflow_id"])
            self.assertEqual("ag_fake", exported["agent_id"])
            trace_text = Path(result["trace"]).read_text(encoding="utf-8")
            self.assertNotIn(secret, trace_text)
            events = [json.loads(line) for line in trace_text.splitlines()]
            names = [row["event"] for row in events]
            self.assertLess(names.index("freya_saved_published"), names.index("freya_exported"))
            self.assertIn("test_skipped", names)
            self.assertGreater(next(row["node_count"] for row in events if row["event"] == "workflow_written"), 0)
            self.assertEqual(7, next(row["tool_count"] for row in events if row["event"] == "policy_written"))
            saved = next(row for row in events if row["event"] == "freya_saved_published")
            self.assertEqual(("wf_fake", "ag_fake"), (saved["workflow_id"], saved["agent_id"]))
            self.assertEqual("end", names[-1])
            self.assertEqual("ok", events[-1]["status"])

    def test_live_error_trace_redacts_workspace_key(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btb-redact-") as directory:
            out = Path(directory) / "out"
            secret = "fak_OFFLINE_TEST_SECRET_987654321"
            args = argparse.Namespace(
                brief=[str(ROOT / "briefs" / "brief-a.md")],
                openapi=str(ROOT / "tools" / "openapi.json"),
                slot="redacted-error", out=str(out), no_live=False, no_test=True,
            )
            with mock.patch.dict(os.environ, {"FREYA_API_KEY": secret}, clear=False), \
                 mock.patch.object(builder, "_model_caller", return_value=None), \
                 mock.patch.object(freya, "validate_workflow", side_effect=RuntimeError("dry run failed for " + secret)):
                with self.assertRaises(RuntimeError):
                    builder.run(args)
            trace_text = (out / "redacted-error.trace.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(secret, trace_text)
            self.assertIn("[REDACTED]", trace_text)
            self.assertEqual("failed", json.loads(trace_text.splitlines()[-1])["status"])


if __name__ == "__main__":
    unittest.main()

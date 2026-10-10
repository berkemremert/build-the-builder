"""Offline checks for publication gates, scoring, and input provenance."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import builder
import freya


ROOT = Path(__file__).resolve().parents[1]


class BuilderReadinessTests(unittest.TestCase):
    def _model_policy(self):
        from policy_compiler import compile_policy
        policy = compile_policy([str(ROOT / "briefs/brief-a.md")], str(ROOT / "tools/openapi.json"), None)
        policy["constraints"].pop("extraction_status")
        return policy

    def test_model_failure_blocks_live_publish_but_records_input_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            args = argparse.Namespace(brief=[str(ROOT / "briefs/brief-a.md")],
                                      openapi=str(ROOT / "tools/openapi.json"),
                                      slot="readiness", out=str(out), no_live=False, no_test=True)
            with mock.patch.dict(os.environ, {"FREYA_API_KEY": "fak_OFFLINE_TEST_KEY_123456"}), \
                 mock.patch.object(builder, "_model_caller", return_value=None), \
                 mock.patch.object(freya, "validate_workflow") as validate, \
                 mock.patch.object(freya, "save_workflow") as save:
                with self.assertRaisesRegex(RuntimeError, "Refusing to publish a source-only policy"):
                    builder.run(args)
            validate.assert_not_called()
            save.assert_not_called()
            rows = [json.loads(line) for line in (out / "readiness.trace.jsonl").read_text().splitlines()]
            fingerprint = next(row for row in rows if row["event"] == "inputs")
            self.assertEqual(hashlib.sha256((ROOT / "briefs/brief-a.md").read_bytes()).hexdigest(),
                             fingerprint["briefs"][0]["sha256"])
            self.assertEqual(hashlib.sha256((ROOT / "tools/openapi.json").read_bytes()).hexdigest(),
                             fingerprint["openapi"]["sha256"])
            self.assertIn("publish_blocked", [row["event"] for row in rows])
            self.assertEqual("failed", rows[-1]["status"])

    def test_zero_passes_is_failed_even_when_runner_exits_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            trace = builder.Trace(out / "trace.jsonl", time.monotonic())

            def fake_run(_command, **_kwargs):
                (out / "practice-results.json").write_text(json.dumps({"passed": 0, "total": 1,
                    "simulation_id": "sim", "iteration_id": "iter", "seconds": 30}))
                return subprocess.CompletedProcess(args=["runner"], returncode=0, stdout="0 / 1 passed", stderr="")

            with mock.patch.object(builder.subprocess, "run", side_effect=fake_run):
                result = builder._run_practice_test("agent", "A", out, trace, time.monotonic())
            self.assertEqual("failed", result["status"])
            self.assertEqual((0, 1), (result["passed"], result["total"]))

    def test_failed_practice_score_marks_whole_build_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            args = argparse.Namespace(brief=[str(ROOT / "briefs/brief-a.md")],
                                      openapi=str(ROOT / "tools/openapi.json"),
                                      slot="scored", out=str(out), no_live=False, no_test=False)
            # The source-only policy is a convenient valid fixture; the mocked
            # model marks it as a model result for this orchestration test.
            policy = self._model_policy()
            def export(_workflow_id, path):
                Path(path).write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {"FREYA_API_KEY": "fak_OFFLINE_TEST_KEY_123456",
                                               "FREYA_BANK_TEAM_KEY": "synthetic-test-key"}), \
                 mock.patch.object(builder, "_model_caller", return_value=lambda *_a, **_kw: policy), \
                 mock.patch.object(freya, "validate_workflow", return_value={"valid": True}), \
                 mock.patch.object(freya, "save_workflow", return_value={"workflow_id": "wf", "agent_id": "ag", "version": 1}), \
                 mock.patch.object(freya, "export_workflow", side_effect=export), \
                 mock.patch.object(builder, "_run_practice_test", return_value={"status": "failed", "passed": 0, "total": 1}):
                with self.assertRaisesRegex(RuntimeError, "Practice test failed: 0/1"):
                    builder.run(args)
            rows = [json.loads(line) for line in (out / "scored.trace.jsonl").read_text().splitlines()]
            self.assertEqual("failed", rows[-1]["status"])

    def test_new_slot_records_confirmed_published_version(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            args = argparse.Namespace(brief=[str(ROOT / "briefs/brief-a.md")],
                                      openapi=str(ROOT / "tools/openapi.json"),
                                      slot="new-slot", out=str(out), no_live=False, no_test=True)
            def export(_workflow_id, path):
                Path(path).write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {"FREYA_API_KEY": "fak_OFFLINE_TEST_KEY_123456"}), \
                 mock.patch.object(builder, "_model_caller", return_value=lambda *_a, **_kw: self._model_policy()), \
                 mock.patch.object(freya, "validate_workflow", return_value={"valid": True}), \
                 mock.patch.object(freya, "save_workflow", return_value={"workflow_id": "wf_new", "agent_id": "ag_new"}), \
                 mock.patch.object(freya, "workspace_id", return_value="ws"), \
                 mock.patch.object(freya, "api", return_value={"versions": [
                     {"id": "ver_new", "versionNumber": 1, "isPublished": True}]} ) as version_lookup, \
                 mock.patch.object(freya, "export_workflow", side_effect=export):
                result = builder.run(args)
            self.assertEqual(1, result["version"])
            version_lookup.assert_called_once_with("GET", "/api/v2/versions/workflow/wf_new", workspaceId="ws")
            rows = [json.loads(line) for line in (out / "new-slot.trace.jsonl").read_text().splitlines()]
            published = next(row for row in rows if row["event"] == "freya_saved_published")
            self.assertEqual((1, "ver_new"), (published["version"], published["version_id"]))
            self.assertEqual("ok", rows[-1]["status"])

    def test_new_slot_without_published_version_stops_before_export(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            args = argparse.Namespace(brief=[str(ROOT / "briefs/brief-a.md")],
                                      openapi=str(ROOT / "tools/openapi.json"),
                                      slot="unpublished", out=str(out), no_live=False, no_test=True)
            with mock.patch.dict(os.environ, {"FREYA_API_KEY": "fak_OFFLINE_TEST_KEY_123456"}), \
                 mock.patch.object(builder, "_model_caller", return_value=lambda *_a, **_kw: self._model_policy()), \
                 mock.patch.object(freya, "validate_workflow", return_value={"valid": True}), \
                 mock.patch.object(freya, "save_workflow", return_value={"workflow_id": "wf_new", "agent_id": "ag_new"}), \
                 mock.patch.object(freya, "workspace_id", return_value="ws"), \
                 mock.patch.object(freya, "api", return_value={"versions": [
                     {"id": "ver_new", "versionNumber": 1, "isPublished": False}]}), \
                 mock.patch.object(freya, "export_workflow") as export:
                with self.assertRaisesRegex(RuntimeError, "Cannot confirm one published version"):
                    builder.run(args)
            export.assert_not_called()
            rows = [json.loads(line) for line in (out / "unpublished.trace.jsonl").read_text().splitlines()]
            self.assertIn("freya_created", [row["event"] for row in rows])
            self.assertNotIn("freya_saved_published", [row["event"] for row in rows])
            self.assertEqual("failed", rows[-1]["status"])


if __name__ == "__main__":
    unittest.main()

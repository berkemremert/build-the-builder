"""Offline CLI and artifact acceptance checks; no Freya or model calls."""

from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import urllib.error
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import builder  # noqa: E402
import freya  # noqa: E402
from policy_compiler import compile_policy  # noqa: E402
from qa import validate_workflow  # noqa: E402
from api_servers import operation_url  # noqa: E402


def _fixture_model_call(prompt: str, *, purpose: str) -> dict:
    """Provide a valid extracted policy without invoking a model or Freya."""
    paths = [str(ROOT / "briefs" / "brief-a.md")]
    if "SOURCE: brief-b.md" in prompt:
        paths.append(str(ROOT / "briefs" / "brief-b.md"))
    policy = compile_policy(paths,
                            str(ROOT / "tools" / "openapi.json"), None)
    policy["constraints"]["extraction_status"] = "model"
    return policy


class BuilderAcceptanceTests(unittest.TestCase):
    def test_case_sources_follow_dates_and_summarize_large_test_fixture(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btb-sources-") as directory:
            folder = Path(directory) / "case"
            (folder / "sources" / "meetings").mkdir(parents=True)
            (folder / "sources" / "emails").mkdir(parents=True)
            (folder / "test-environment").mkdir()
            (folder / "BRIEF.md").write_text("Overview", encoding="utf-8")
            (folder / "sources" / "emails" / "2026-10-09-recap.txt").write_text(
                "Later decision", encoding="utf-8")
            (folder / "sources" / "meetings" / "2026-10-07-session.txt").write_text(
                "Earlier decision", encoding="utf-8")
            fixture = {"description": "Synthetic customers", "customers": [
                {"customerNumber": str(index), "privateSample": "x" * 300}
                for index in range(400)]}
            (folder / "test-environment" / "customers.json").write_text(
                json.dumps(fixture), encoding="utf-8")
            documents, _, _ = builder._case_inputs(folder)
            self.assertLess(documents.index(folder / "sources" / "meetings" / "2026-10-07-session.txt"),
                            documents.index(folder / "sources" / "emails" / "2026-10-09-recap.txt"))
            out = Path(directory) / "out"
            trace = builder.Trace(out / "trace.jsonl", time.monotonic())
            sources = builder._materialize_case_documents(folder, documents, out, "synthetic", trace)
            text = "\n".join(path.read_text(encoding="utf-8") for path in sources)
            self.assertLess(text.index("Earlier decision"), text.index("Later decision"))
            self.assertIn("Record count: 400", text)
            self.assertNotIn("x" * 300, text)

    def test_two_positional_arguments_select_case_mode_defaults(self) -> None:
        with mock.patch.object(sys, "argv", ["builder.py", "/tmp/final-case", "https://mock.example/base"]), \
             mock.patch.object(builder, "run", return_value={"ok": True}) as execute, \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(0, builder.main())
        args = execute.call_args.args[0]
        self.assertEqual("/tmp/final-case", args.case_folder)
        self.assertEqual("https://mock.example/base", args.mock_url)
        self.assertEqual("final-case", args.slot)
        self.assertEqual("out/final-case", args.out)

    def test_case_folder_discovers_sources_and_routes_every_tool_to_mock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btb-case-") as directory:
            folder = Path(directory) / "Turkish case"
            folder.mkdir()
            (folder / "README.md").write_text("Genel açıklama ve servis kuralları.", encoding="utf-8")
            (folder / "BRIEF.md").write_text("Kart ekstresi borcu için görüşme kuralları.", encoding="utf-8")
            (folder / "questions.json").write_text('{"soru":"Ödeme tarihi?"}', encoding="utf-8")
            with zipfile.ZipFile(folder / "conversation.docx", "w") as archive:
                archive.writestr("word/document.xml",
                                 '<w:document xmlns:w="urn:test"><w:body><w:p><w:r><w:t>Merhaba müşteri</w:t>'
                                 '</w:r></w:p></w:body></w:document>')
            api = json.loads((ROOT / "tools/openapi.json").read_text(encoding="utf-8"))
            for path_item in api["paths"].values():
                path_item["servers"] = [{"url": "https://old.example/path"}]
                for operation in path_item.values():
                    if isinstance(operation, dict):
                        operation["servers"] = [{"url": "https://old.example/operation"}]
            (folder / "service.json").write_text(json.dumps(api), encoding="utf-8")
            out = Path(directory) / "out"
            args = argparse.Namespace(case_folder=str(folder), mock_url="https://mock.example/api",
                                      brief=None, openapi=None, slot="synthetic", out=str(out),
                                      no_live=True, no_test=True)
            with mock.patch.object(builder, "_model_caller", return_value=_fixture_model_call):
                result = builder.run(args)
            effective = json.loads(Path(result["effective_openapi"]).read_text(encoding="utf-8"))
            self.assertEqual([{"url": "https://mock.example/api"}], effective["servers"])
            self.assertTrue(all("servers" not in item for item in effective["paths"].values()))
            self.assertTrue(all("servers" not in operation for item in effective["paths"].values()
                                for operation in item.values() if isinstance(operation, dict)))
            workflow = json.loads(Path(result["workflow"]).read_text(encoding="utf-8"))
            self.assertEqual([], validate_workflow(workflow, effective))
            workflow_text = json.dumps(workflow)
            self.assertIn("https://mock.example/api/verify_identity", workflow_text)
            self.assertNotIn("old.example", workflow_text)
            policy = json.loads(Path(result["policy"]).read_text(encoding="utf-8"))
            self.assertIn("Merhaba müşteri", policy["raw_brief"])
            self.assertIn("Kart ekstresi", policy["raw_brief"])
            self.assertIn("Ödeme tarihi", policy["raw_brief"])
            events = [json.loads(line) for line in Path(result["trace"]).read_text().splitlines()]
            self.assertEqual(4, sum(row["event"] == "case_document_extracted" for row in events))
            self.assertEqual("ok", events[-1]["status"])

    def test_mock_url_rejects_credentials_and_query(self) -> None:
        for url in ("https://user:password@mock.example", "https://mock.example/?token=secret", "file:///tmp/mock"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                builder._mock_openapi({"paths": {}}, url)

    def test_mock_origin_and_optional_prefix_match_worker_convention(self) -> None:
        api = {"openapi": "3.0.0", "paths": {"/v1/account": {"post": {"responses": {"200": {}}}}}}
        origin = builder._mock_openapi(api, "https://worker.example")
        prefixed = builder._mock_openapi(api, "https://worker.example/mock")
        self.assertEqual("https://worker.example/v1/account",
                         operation_url(origin, "/v1/account", "post"))
        self.assertEqual("https://worker.example/mock/v1/account",
                         operation_url(prefixed, "/v1/account", "post"))
        with self.assertRaisesRegex(ValueError, "repeats a path prefix"):
            builder._mock_openapi(api, "https://worker.example/v1")

    def test_prose_service_contract_uses_model_then_validates_workflow(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btb-prose-contract-") as directory:
            folder = Path(directory) / "case"
            folder.mkdir()
            (folder / "BRIEF.md").write_text("Müşteri araması kuralları.", encoding="utf-8")
            (folder / "service.txt").write_text(
                "The documented service has GET /get_account and POST /create_promise with explicit schemas.",
                encoding="utf-8")
            api = json.loads((ROOT / "tools/openapi.json").read_text(encoding="utf-8"))
            calls = []

            def model(prompt: str, *, purpose: str):
                calls.append(purpose)
                if purpose == "extract documented service contract":
                    self.assertIn("service.txt", prompt)
                    self.assertIn("Müşteri", prompt)
                    return api
                return _fixture_model_call(prompt, purpose=purpose)

            args = argparse.Namespace(case_folder=str(folder), mock_url="https://mock.example/bank",
                                      brief=None, openapi=None, slot="prose", out=str(Path(directory) / "out"),
                                      no_live=True, no_test=True)
            with mock.patch.object(builder, "_model_caller", return_value=model):
                result = builder.run(args)
            self.assertEqual(["extract documented service contract",
                              "compile bank brief and tool policy"], calls)
            self.assertTrue((Path(args.out) / "prose.derived.openapi.json").is_file())
            self.assertIn("https://mock.example/bank/get_account",
                          Path(result["workflow"]).read_text(encoding="utf-8"))
            events = [json.loads(line) for line in Path(result["trace"]).read_text().splitlines()]
            self.assertIn("contract_synthesis_passed", [row["event"] for row in events])
            self.assertNotIn("The documented service", Path(result["trace"]).read_text())

    def test_incomplete_prose_contract_fails_without_workflow(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btb-prose-invalid-") as directory:
            folder = Path(directory) / "case"
            folder.mkdir()
            (folder / "BRIEF.md").write_text("Bank policy.", encoding="utf-8")
            (folder / "service.txt").write_text("Service details are missing.", encoding="utf-8")
            args = argparse.Namespace(case_folder=str(folder), mock_url="https://mock.example",
                                      brief=None, openapi=None, slot="invalid-contract",
                                      out=str(Path(directory) / "out"), no_live=True, no_test=True)
            with mock.patch.object(builder, "_model_caller",
                                   return_value=lambda *_a, **_kw: '{"error":"no endpoint methods"}'):
                with self.assertRaisesRegex(ValueError, "no endpoint methods"):
                    builder.run(args)
            self.assertFalse((Path(args.out) / "invalid-contract.workflow.json").exists())
            events = [json.loads(line) for line in
                      (Path(args.out) / "invalid-contract.trace.jsonl").read_text().splitlines()]
            self.assertEqual("failed", events[-1]["status"])

    def test_openai_trace_and_request_do_not_expose_prompt_or_key(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btb-model-trace-") as directory:
            trace = builder.Trace(Path(directory) / "trace.jsonl", time.monotonic())
            key = "sk-OFFLINE_TEST_SECRET"
            response = {"id": "resp_offline", "model": "gpt-6-sol", "status": "completed",
                        "output": [{"type": "reasoning"}, {"type": "message", "content": [
                            {"type": "output_text", "text": "{\"global_rules\": \"ok\"}"}]}],
                        "usage": {"input_tokens": 12, "output_tokens": 30,
                                  "output_tokens_details": {"reasoning_tokens": 10}}}
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": key}, clear=True), \
                 mock.patch.object(builder, "_openai_response", return_value=response) as api:
                caller = builder._model_caller(trace, trace.started)
                self.assertEqual('{"global_rules": "ok"}', caller("private prompt text", purpose="compile bank brief"))
            payload, sent_key, timeout = api.call_args.args
            self.assertEqual(key, sent_key)
            self.assertEqual("gpt-6-sol", payload["model"])
            self.assertEqual({"effort": "medium"}, payload["reasoning"])
            self.assertEqual({"format": {"type": "json_object"}}, payload["text"])
            self.assertEqual(25000, payload["max_output_tokens"])
            self.assertIs(False, payload["store"])
            self.assertIs(False, payload["stream"])
            self.assertEqual("private prompt text", payload["input"][0]["content"])
            self.assertEqual(900, timeout)
            trace_text = trace.path.read_text(encoding="utf-8")
            self.assertNotIn("private prompt text", trace_text)
            self.assertNotIn(key, trace_text)
            rows = [json.loads(line) for line in trace_text.splitlines()]
            self.assertEqual(["model_call_start", "model_call_end"], [row["event"] for row in rows])
            self.assertEqual("compile bank brief", rows[0]["purpose"])
            self.assertEqual("gpt-6-sol", rows[1]["requested_model"])
            self.assertEqual("gpt-6-sol", rows[1]["actual_model"])
            self.assertEqual("ok", rows[1]["status"])
            self.assertEqual((12, 30, 10), (rows[1]["input_tokens"], rows[1]["output_tokens"], rows[1]["reasoning_tokens"]))

    def test_openai_transport_uses_direct_responses_endpoint(self) -> None:
        payload = {"model": "gpt-6-sol", "input": "offline"}
        opener = mock.Mock()
        opener.open.return_value = io.BytesIO(b'{"status":"completed"}')
        with mock.patch.object(builder.urllib.request, "build_opener", return_value=opener):
            result = builder._openai_response(payload, "sk-offline", 900)
        self.assertEqual("completed", result["status"])
        request = opener.open.call_args.args[0]
        self.assertEqual("https://api.openai.com/v1/responses", request.full_url)
        self.assertEqual("POST", request.get_method())
        self.assertEqual(payload, json.loads(request.data))
        self.assertEqual("Bearer sk-offline", request.get_header("Authorization"))
        self.assertEqual(900, opener.open.call_args.kwargs["timeout"])

    def test_missing_openai_key_fails_without_router_or_cli_fallback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btb-no-key-") as directory:
            out = Path(directory) / "out"
            env = os.environ.copy()
            env["PATH"] = ""
            env.pop("OPENAI_API_KEY", None)
            env["OPENROUTER_API_KEY"] = "sk-or-v1-ignored"
            command = [sys.executable, str(ROOT / "builder.py"), "--brief", str(ROOT / "briefs/brief-a.md"),
                       "--openapi", str(ROOT / "tools/openapi.json"), "--slot", "missing-key",
                       "--out", str(out), "--no-live"]
            completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                                       text=True, timeout=30, check=False)
            self.assertEqual(1, completed.returncode)
            self.assertIn("OPENAI_API_KEY is required", completed.stderr)
            rows = [json.loads(line) for line in (out / "missing-key.trace.jsonl").read_text().splitlines()]
            self.assertIn("model_unavailable", [row["event"] for row in rows])
            self.assertEqual("failed", rows[-1]["status"])

    def test_no_live_build_writes_a_and_b_artifacts_with_mocked_openai(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btb-offline-") as directory:
            out = Path(directory) / "out"
            for slot, briefs in (
                ("public-a", ["brief-a.md"]),
                ("public-b", ["brief-a.md", "brief-b.md"]),
            ):
                args = argparse.Namespace(
                    brief=[str(ROOT / "briefs" / name) for name in briefs],
                    openapi=str(ROOT / "tools" / "openapi.json"), slot=slot,
                    out=str(out), no_live=True, no_test=True,
                )
                with mock.patch.object(builder, "_model_caller", return_value=_fixture_model_call):
                    result = builder.run(args)
                for key in ("policy", "local_workflow", "workflow", "trace"):
                    self.assertTrue(Path(result[key]).is_file(), key)
                policy = json.loads(Path(result["policy"]).read_text(encoding="utf-8"))
                self.assertIn("SOURCE: brief-a.md", policy["raw_brief"])
                if slot == "public-b":
                    self.assertIn("SOURCE: brief-b.md", policy["raw_brief"])
                    self.assertLess(policy["raw_brief"].index("SOURCE: brief-a.md"),
                                    policy["raw_brief"].index("SOURCE: brief-b.md"))
                workflow = json.loads(Path(result["workflow"]).read_text(encoding="utf-8"))
                openapi = json.loads((ROOT / "tools/openapi.json").read_text(encoding="utf-8"))
                self.assertEqual([], validate_workflow(workflow, openapi))
                events = [json.loads(line) for line in Path(result["trace"]).read_text(encoding="utf-8").splitlines()]
                self.assertEqual("ok", events[-1]["status"])
                self.assertIn("policy_validation_passed", {row["event"] for row in events})
                self.assertIn("workflow_written", {row["event"] for row in events})
                self.assertIn("freya_skipped", {row["event"] for row in events})

    def test_incomplete_and_empty_openai_responses_fail_clearly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btb-model-errors-") as directory:
            for label, response, expected in (
                ("incomplete", {"model": "gpt-6-sol", "status": "incomplete",
                                "incomplete_details": {"reason": "max_output_tokens"}, "output": []},
                 "did not complete"),
                ("empty", {"model": "gpt-6-sol", "status": "completed", "output": []},
                 "no policy text"),
            ):
                trace = builder.Trace(Path(directory) / f"{label}.trace.jsonl", time.monotonic())
                with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-offline"}, clear=True), \
                     mock.patch.object(builder, "_openai_response", return_value=response) as api:
                    caller = builder._model_caller(trace, trace.started)
                    with self.assertRaisesRegex(RuntimeError, expected):
                        caller("private prompt", purpose="compile bank brief")
                api.assert_called_once()
                rows = [json.loads(line) for line in trace.path.read_text().splitlines()]
                self.assertEqual(label, rows[-1]["status"])
                self.assertEqual("gpt-6-sol", rows[-1]["actual_model"])

    def test_openai_http_error_trace_does_not_expose_key(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btb-http-error-") as directory:
            trace = builder.Trace(Path(directory) / "trace.jsonl", time.monotonic())
            key = "sk-OFFLINE_HTTP_SECRET"
            error = urllib.error.HTTPError(builder.OPENAI_URL, 403, key, None, io.BytesIO(b""))
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": key}, clear=True), \
                 mock.patch.object(builder, "_openai_response", side_effect=error):
                caller = builder._model_caller(trace, trace.started)
                with self.assertRaisesRegex(RuntimeError, "OpenAI HTTP 403"):
                    caller("private prompt", purpose="compile bank brief")
            error.close()
            text = trace.path.read_text()
            self.assertNotIn(key, text)
            self.assertNotIn("private prompt", text)
            row = json.loads(text.splitlines()[-1])
            self.assertEqual("http_error", row["status"])
            self.assertEqual(403, row["http_status"])

    def test_invalid_model_policy_fails_build_without_source_only_fallback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btb-invalid-policy-") as directory:
            out = Path(directory)
            args = argparse.Namespace(brief=[str(ROOT / "briefs/brief-a.md")],
                                      openapi=str(ROOT / "tools/openapi.json"),
                                      slot="invalid", out=str(out), no_live=True, no_test=True)
            response = {"model": "gpt-6-sol", "status": "completed",
                        "output": [{"type": "message", "content": [
                            {"type": "output_text", "text": "{\"global_rules\": \"only one key\"}"}]}]}
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-offline"}, clear=True), \
                 mock.patch.object(builder, "_openai_response", return_value=response):
                with self.assertRaisesRegex(ValueError, "omitted keys"):
                    builder.run(args)
            self.assertFalse((out / "invalid.policy.json").exists())
            rows = [json.loads(line) for line in (out / "invalid.trace.jsonl").read_text().splitlines()]
            self.assertIn("policy_compile_failed", [row["event"] for row in rows])
            self.assertEqual("failed", rows[-1]["status"])

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
                 mock.patch.object(builder, "_model_caller", return_value=_fixture_model_call), \
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
                 mock.patch.object(builder, "_model_caller", return_value=_fixture_model_call), \
                 mock.patch.object(freya, "validate_workflow", side_effect=RuntimeError("dry run failed for " + secret)):
                with self.assertRaises(RuntimeError):
                    builder.run(args)
            trace_text = (out / "redacted-error.trace.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(secret, trace_text)
            self.assertIn("[REDACTED]", trace_text)
            self.assertEqual("failed", json.loads(trace_text.splitlines()[-1])["status"])


if __name__ == "__main__":
    unittest.main()

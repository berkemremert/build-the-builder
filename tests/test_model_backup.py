"""Offline provider fallback tests for the policy model caller."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import builder


def _primary(text: str = '{"ok":true}') -> dict:
    return {"id": "resp_primary", "model": "gpt-6-sol", "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 4, "output_tokens": 5}}


def _backup(text: str = '{"ok":true}') -> dict:
    return {"id": "gen_backup", "model": "openai/gpt-6-sol", "choices": [
        {"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 6, "completion_tokens": 7}}


class PolicyModelBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.trace_path = Path(self.temp.name) / "trace.jsonl"
        self.started = time.monotonic()
        self.trace = builder.Trace(self.trace_path, self.started)

    def rows(self):
        return [json.loads(line) for line in self.trace_path.read_text(encoding="utf-8").splitlines()]

    def test_openai_remains_primary_without_backup_request(self):
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "primary-secret",
                                         "OPENROUTER_API_KEY": "backup-secret"}), \
             mock.patch.object(builder, "_openai_response", return_value=_primary()) as primary, \
             mock.patch.object(builder, "_openrouter_response") as backup:
            result = builder._model_caller(self.trace, self.started)("prompt", purpose="policy")
        self.assertEqual('{"ok":true}', result)
        primary.assert_called_once()
        backup.assert_not_called()
        self.assertEqual(["openai", "openai"], [row["provider"] for row in self.rows()
                                                  if row["event"].startswith("model_call_")])

    def test_rate_limit_and_service_unavailability_use_explicit_backup_model(self):
        for status in (429, 503):
            with self.subTest(status=status):
                self.trace = builder.Trace(self.trace_path, self.started)
                error = urllib.error.HTTPError(builder.OPENAI_URL, status, "unavailable", {}, io.BytesIO())
                with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "primary-secret",
                                                 "OPENROUTER_API_KEY": "backup-secret"}), \
                     mock.patch.object(builder, "_openai_response", side_effect=error), \
                     mock.patch.object(builder, "_openrouter_response", return_value=_backup()) as backup:
                    result = builder._model_caller(self.trace, self.started)("prompt", purpose="policy")
                self.assertEqual('{"ok":true}', result)
                payload, key, _ = backup.call_args.args
                self.assertEqual("backup-secret", key)
                self.assertEqual("openai/gpt-6-sol", payload["model"])
                self.assertEqual({"type": "json_object"}, payload["response_format"])
                self.assertEqual("medium", payload["reasoning"]["effort"])
                fallback = next(row for row in self.rows() if row["event"] == "model_fallback")
                self.assertEqual(f"openai_http_{status}", fallback["reason"])
                end = [row for row in self.rows() if row["event"] == "model_call_end"
                       and row["provider"] == "openrouter"][0]
                self.assertEqual("openai/gpt-6-sol", end["actual_model"])
                self.assertNotIn("primary-secret", self.trace_path.read_text())
                self.assertNotIn("backup-secret", self.trace_path.read_text())

    def test_missing_primary_key_uses_backup_when_configured(self):
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "",
                                         "OPENROUTER_API_KEY": "backup-secret",
                                         "OPENROUTER_MODEL": "openai/gpt-6-sol"}), \
             mock.patch.object(builder, "_openai_response") as primary, \
             mock.patch.object(builder, "_openrouter_response", return_value=_backup()) as backup:
            self.assertEqual('{"ok":true}', builder._model_caller(self.trace, self.started)(
                "prompt", purpose="policy"))
        primary.assert_not_called()
        backup.assert_called_once()
        self.assertEqual("openai_key_missing", next(row for row in self.rows()
                         if row["event"] == "model_fallback")["reason"])

    def test_bad_request_and_malformed_primary_response_do_not_fallback(self):
        for failure in (urllib.error.HTTPError(builder.OPENAI_URL, 400, "bad request", {}, io.BytesIO()),
                        {"model": "gpt-6-sol", "status": "completed", "output": []}):
            with self.subTest(failure=type(failure).__name__):
                self.trace = builder.Trace(self.trace_path, self.started)
                with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "primary-secret",
                                                 "OPENROUTER_API_KEY": "backup-secret"}), \
                     mock.patch.object(builder, "_openai_response",
                                       side_effect=failure if isinstance(failure, Exception) else None,
                                       return_value=None if isinstance(failure, Exception) else failure), \
                     mock.patch.object(builder, "_openrouter_response") as backup:
                    with self.assertRaises(RuntimeError):
                        builder._model_caller(self.trace, self.started)("prompt", purpose="policy")
                backup.assert_not_called()
                self.assertFalse(any(row["event"] == "model_fallback" for row in self.rows()))

    def test_network_failure_falls_back_but_backup_malformed_response_is_rejected(self):
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "primary-secret",
                                         "OPENROUTER_API_KEY": "backup-secret"}), \
             mock.patch.object(builder, "_openai_response", side_effect=urllib.error.URLError("offline")), \
             mock.patch.object(builder, "_openrouter_response", return_value={"choices": []}):
            with self.assertRaisesRegex(RuntimeError, "OpenRouter"):
                builder._model_caller(self.trace, self.started)("prompt", purpose="policy")
        self.assertEqual("network_error", next(row for row in self.rows()
                         if row["event"] == "model_call_end" and row["provider"] == "openai")["status"])
        self.assertEqual("invalid_response", next(row for row in self.rows()
                         if row["event"] == "model_call_end" and row["provider"] == "openrouter")["status"])


if __name__ == "__main__":
    unittest.main()

"""Offline tests ensuring policy extraction uses OpenRouter exclusively."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import builder


class OpenRouterOnlyModelTests(unittest.TestCase):
    def test_both_keys_present_still_uses_only_openrouter(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = builder.Trace(Path(directory) / "trace.jsonl", time.monotonic())
            response = {"id": "gen_test", "model": "openai/gpt-6-sol",
                        "choices": [{"message": {"content": '{"ok":true}'},
                                     "finish_reason": "stop"}]}
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "unused-direct-secret",
                                              "OPENROUTER_API_KEY": "router-secret"}, clear=True), \
                 mock.patch.object(builder, "_openrouter_response", return_value=response) as router, \
                 mock.patch.object(builder.urllib.request, "build_opener") as opener:
                result = builder._model_caller(trace, trace.started)("private brief", purpose="policy")
            self.assertEqual('{"ok":true}', result)
            router.assert_called_once()
            opener.assert_not_called()
            payload, key, _ = router.call_args.args
            self.assertEqual("router-secret", key)
            self.assertEqual("openai/gpt-6-sol", payload["model"])
            rows = [json.loads(line) for line in trace.path.read_text().splitlines()]
            self.assertEqual(["openrouter", "openrouter"], [row["provider"] for row in rows])
            self.assertEqual("openai/gpt-6-sol", rows[-1]["actual_model"])
            self.assertNotIn("unused-direct-secret", trace.path.read_text())
            self.assertNotIn("router-secret", trace.path.read_text())

    def test_missing_router_key_fails_even_when_direct_openai_key_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = builder.Trace(Path(directory) / "trace.jsonl", time.monotonic())
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "unused-direct-secret"}, clear=True), \
                 mock.patch.object(builder, "_openrouter_response") as router, \
                 mock.patch.object(builder.urllib.request, "build_opener") as opener:
                with self.assertRaisesRegex(RuntimeError, "OPENROUTER_API_KEY is required"):
                    builder._model_caller(trace, trace.started)("private brief", purpose="policy")
            router.assert_not_called()
            opener.assert_not_called()
            rows = [json.loads(line) for line in trace.path.read_text().splitlines()]
            self.assertEqual("openrouter", rows[-1]["provider"])

    def test_configurable_model_stays_on_router_path(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = builder.Trace(Path(directory) / "trace.jsonl", time.monotonic())
            response = {"model": "openai/gpt-6-sol", "choices": [{
                "message": {"content": "{}"}, "finish_reason": "stop"}]}
            with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "router-secret",
                                              "OPENROUTER_MODEL": "openai/gpt-6-sol"}, clear=True), \
                 mock.patch.object(builder, "_openrouter_response", return_value=response) as router:
                builder._model_caller(trace, trace.started)("prompt", purpose="policy")
            self.assertEqual("openai/gpt-6-sol", router.call_args.args[0]["model"])


if __name__ == "__main__":
    unittest.main()

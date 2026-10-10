"""Focused checks for source-driven policy extraction."""

import json
from pathlib import Path
import tempfile
import unittest

from policy_compiler import compile_policy


class PolicyCompilerReadinessTests(unittest.TestCase):
    def test_model_receives_full_openapi_source(self):
        api = {
            "openapi": "3.0.3",
            "servers": [{"url": "https://example.invalid/v2"}],
            "security": [{"bankKey": []}],
            "components": {"securitySchemes": {"bankKey": {"type": "apiKey", "in": "header", "name": "X-Bank-Key"}},
                           "schemas": {"Result": {"type": "object", "properties": {"status": {"enum": ["accepted"]}}}}},
            "paths": {"/customers/{customerId}/verify": {
                "parameters": [{"name": "customerId", "in": "path", "required": True, "schema": {"type": "string"}}],
                "post": {"operationId": "verify_identity", "security": [{"bankKey": []}],
                         "parameters": [{"name": "locale", "in": "query", "schema": {"enum": ["en"]}}],
                         "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Result"}}}}}},
            }},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            brief = root / "brief.md"
            spec = root / "openapi.json"
            brief.write_text("Verify the customer.", encoding="utf-8")
            spec.write_text(json.dumps(api), encoding="utf-8")

            def model(prompt, *, purpose):
                self.assertEqual("compile bank brief and tool policy", purpose)
                self.assertIn('"name": "customerId"', prompt)
                self.assertIn('"name": "locale"', prompt)
                self.assertIn('"X-Bank-Key"', prompt)
                self.assertIn('"$ref": "#/components/schemas/Result"', prompt)
                self.assertIn('"url": "https://example.invalid/v2"', prompt)
                return {"global_rules": "Verify", "opening": "Ask", "identity": "Verify",
                        "verified_call": "Continue", "outcomes": {},
                        "tool_policy": {"verify_identity": "Use the required path and query parameters."},
                        "raw_brief": "", "constraints": {}}

            policy = compile_policy([str(brief)], str(spec), model)
            self.assertEqual("Verify the customer.", policy["raw_brief"].splitlines()[-1])

    def test_missing_tool_instructions_rejected(self):
        api = {"paths": {"/verify_identity": {"post": {}}}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            brief = root / "brief.md"
            spec = root / "openapi.json"
            brief.write_text("Verify the customer.", encoding="utf-8")
            spec.write_text(json.dumps(api), encoding="utf-8")
            policy = {"global_rules": "a", "opening": "b", "identity": "c",
                      "verified_call": "d", "outcomes": {}, "tool_policy": {},
                      "raw_brief": "", "constraints": {}}
            with self.assertRaisesRegex(ValueError, "omitted tool instructions: verify_identity"):
                compile_policy([str(brief)], str(spec), lambda *_args, **_kwargs: policy)


if __name__ == "__main__":
    unittest.main()

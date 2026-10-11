"""Identity request selector fields come from the supplied OpenAPI schema."""

from __future__ import annotations

import unittest

from api_bindings import identity_selector_field
from probe_changed_brief import synthetic_api, synthetic_policy
from qa import validate_workflow
from workflow_generator import build_workflow


class IdentitySelectorTests(unittest.TestCase):
    def test_renamed_selector_uses_its_enum(self) -> None:
        schema = synthetic_api()["paths"]["/verify_identity"]["post"]
        schema = schema["requestBody"]["content"]["application/json"]["schema"]
        schema["properties"]["question_code"] = schema["properties"].pop("field")
        self.assertEqual(("question_code", ["birth_year", "passcode"]),
                         identity_selector_field(schema))

    def test_ambiguous_selector_fails_closed(self) -> None:
        schema = {"type": "object", "properties": {
            "choice_a": {"type": "string", "enum": ["A", "B"]},
            "choice_b": {"type": "string", "enum": ["X", "Y"]},
        }}
        with self.assertRaisesRegex(ValueError, "unambiguous security selector"):
            identity_selector_field(schema)

    def test_qa_requires_fixed_selector_on_verify_tools(self) -> None:
        api = synthetic_api()
        workflow = build_workflow(synthetic_policy(), api)
        body = (workflow["nodes"]["identity_dob"]["tools"][0]["function"]
                ["parameters"]["properties"]["body"]["properties"])
        body["field"]["default"] = "{{field}}"
        self.assertTrue(any("bind a fixed security selector" in error
                            for error in validate_workflow(workflow, api)))


if __name__ == "__main__":
    unittest.main()

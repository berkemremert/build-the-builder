"""Identity request selector fields come from the supplied OpenAPI schema."""

from __future__ import annotations

import unittest

from api_bindings import identity_selector_field
from probe_changed_brief import synthetic_api, synthetic_policy
from qa import validate_workflow
from workflow_generator import _identity_fields, build_workflow


class IdentitySelectorTests(unittest.TestCase):
    def test_three_required_questions_cannot_build_two_check_workflow(self) -> None:
        api = synthetic_api(identity_order=["birth_year", "passcode", "postcode"])
        policy = synthetic_policy()
        policy["raw_brief"] += (
            "\nAsk birth year first, then passcode, then postcode; "
            "all three must pass before get_account.\n"
        )
        with self.assertRaisesRegex(ValueError, "more than two identity checks"):
            build_workflow(policy, api)

    def test_enumerated_three_questions_cannot_build_two_check_workflow(self) -> None:
        api = synthetic_api(identity_order=["birth_year", "passcode", "postcode"])
        policy = synthetic_policy()
        policy["raw_brief"] += "\nAsk birth year, passcode, and postcode in that order.\n"
        with self.assertRaisesRegex(ValueError, "more than two identity checks"):
            build_workflow(policy, api)

    def test_later_source_memo_replaces_extracted_identity_order(self) -> None:
        api = synthetic_api()
        policy = synthetic_policy(identity="Ask birth year first, then passcode.")
        policy["raw_brief"] += (
            "\nLater memo: replace identity order. Ask passcode first, then birth year.\n"
        )
        self.assertEqual(["passcode", "birth_year"], _identity_fields(api, policy))
        workflow = build_workflow(policy, api)
        body = (workflow["nodes"]["identity_dob"]["tools"][0]["function"]
                ["parameters"]["properties"]["body"]["properties"])
        self.assertEqual("passcode", body["field"]["default"])

    def test_renamed_selector_uses_its_enum(self) -> None:
        schema = synthetic_api()["paths"]["/verify_identity"]["post"]
        schema = schema["requestBody"]["content"]["application/json"]["schema"]
        schema["properties"]["question_code"] = schema["properties"].pop("field")
        self.assertEqual(("question_code", ["birth_year", "passcode"]),
                         identity_selector_field(schema))

    def test_workflow_binds_both_verification_tools_to_renamed_selector(self) -> None:
        api = synthetic_api()
        schema = api["paths"]["/verify_identity"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        schema["properties"]["question_code"] = schema["properties"].pop("field")
        schema["properties"]["response_value"] = schema["properties"].pop("answer")
        schema["required"] = ["question_code" if field == "field" else
                              "response_value" if field == "answer" else field
                              for field in schema["required"]]
        workflow = build_workflow(synthetic_policy(), api)
        self.assertEqual([], validate_workflow(workflow, api))
        selectors = []
        for node in ("identity_dob", "identity_phone"):
            props = (workflow["nodes"][node]["tools"][0]["function"]
                     ["parameters"]["properties"]["body"]["properties"])
            self.assertNotIn("field", props)
            self.assertNotIn("answer", props)
            self.assertEqual("{{response_value}}", props["response_value"]["default"])
            selectors.append(props["question_code"]["default"])
        self.assertEqual(["birth_year", "passcode"], selectors)

    def test_ambiguous_selector_fails_closed(self) -> None:
        schema = {"type": "object", "properties": {
            "choice_a": {"type": "string", "enum": ["A", "B"]},
            "choice_b": {"type": "string", "enum": ["X", "Y"]},
        }}
        with self.assertRaisesRegex(ValueError, "unambiguous security selector"):
            identity_selector_field(schema)

    def test_qa_rejects_selector_outside_source_enum(self) -> None:
        api = synthetic_api()
        workflow = build_workflow(synthetic_policy(), api)
        body = (workflow["nodes"]["identity_dob"]["tools"][0]["function"]
                ["parameters"]["properties"]["body"]["properties"])
        body["field"]["default"] = "unknown_check"
        self.assertTrue(any("bind a valid security selector" in error
                            for error in validate_workflow(workflow, api)))


if __name__ == "__main__":
    unittest.main()

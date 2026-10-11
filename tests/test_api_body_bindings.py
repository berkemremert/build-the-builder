"""Required request constants must be present in generated API calls."""

from __future__ import annotations

import copy
import unittest

from api_body_bindings import fixed_required_body_values
from probe_changed_brief import synthetic_api, synthetic_policy
from qa import validate_workflow
from workflow_generator import build_workflow


def _account_field(api: dict, spec: dict) -> None:
    schema = api["paths"]["/get_account"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    schema["properties"]["channel"] = spec
    schema["required"].append("channel")


def _account_tool(workflow: dict) -> dict:
    return workflow["nodes"]["load_account"]["pre_actions"][0]


class RequiredBodyBindingsTests(unittest.TestCase):
    def test_single_enum_is_bound_and_qa_rejects_unbound_placeholder(self) -> None:
        api = synthetic_api()
        _account_field(api, {"type": "string", "enum": ["voice"],
                             "description": "Required contact channel"})
        workflow = build_workflow(synthetic_policy(), api)
        body = _account_tool(workflow)["function"]["parameters"]["properties"]["body"]["properties"]
        self.assertEqual("voice", body["channel"]["default"])
        self.assertEqual([], validate_workflow(workflow, api))
        body["channel"]["default"] = "{{channel}}"
        self.assertTrue(any("channel" in error and "fixed" in error
                            for error in validate_workflow(workflow, api)))

    def test_const_and_default_are_bound_when_consistent(self) -> None:
        api = synthetic_api()
        _account_field(api, {"type": "string", "const": "voice", "default": "voice",
                             "enum": ["voice", "branch"]})
        values = fixed_required_body_values("get_account",
            api["paths"]["/get_account"]["post"]["requestBody"]["content"]["application/json"]["schema"])
        self.assertEqual({"channel": "voice"}, values)
        workflow = build_workflow(synthetic_policy(), api)
        body = _account_tool(workflow)["function"]["parameters"]["properties"]["body"]["properties"]
        self.assertEqual("voice", body["channel"]["default"])

    def test_ambiguous_or_conflicting_required_field_fails_closed(self) -> None:
        for spec in ({"type": "string", "enum": ["voice", "branch"]},
                     {"type": "string"},
                     {"type": "string", "const": "voice", "default": "branch"}):
            with self.subTest(spec=spec):
                api = synthetic_api()
                _account_field(api, copy.deepcopy(spec))
                with self.assertRaisesRegex(ValueError, "required request field 'channel'"):
                    build_workflow(synthetic_policy(), api)


if __name__ == "__main__":
    unittest.main()

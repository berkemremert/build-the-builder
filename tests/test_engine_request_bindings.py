"""Renamed engine action request fields must remain bound to call state."""

from __future__ import annotations

import copy
import unittest

from api_bindings import close_request_fields, transfer_request_fields
from probe_changed_brief import synthetic_api, synthetic_policy
from qa import validate_workflow
from workflow_generator import build_workflow


def renamed_api() -> dict:
    api = synthetic_api()
    transfer = api["paths"]["/transfer_to_agent"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    transfer["properties"]["routing_code"] = transfer["properties"].pop("queue")
    transfer["required"] = ["routing_code" if item == "queue" else item for item in transfer["required"]]
    close = api["paths"]["/close_call"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    close["properties"]["result_code"] = close["properties"].pop("outcome")
    close["properties"]["detail_code"] = close["properties"].pop("reason")
    close["required"] = [{"outcome": "result_code", "reason": "detail_code"}.get(item, item)
                         for item in close["required"]]
    return api


def body_fields(workflow: dict, node: str) -> dict:
    action = workflow["nodes"][node]["pre_actions"][0]
    return action["function"]["parameters"]["properties"]["body"]["properties"]


class EngineRequestBindingTests(unittest.TestCase):
    def test_renamed_transfer_and_close_fields_are_bound(self) -> None:
        api = renamed_api()
        workflow = build_workflow(synthetic_policy(), api)
        self.assertEqual([], validate_workflow(workflow, api))
        for role, code in (("support", "Q2"), ("human", "Q3"), ("restructuring", "Q1")):
            fields = body_fields(workflow, "transfer_" + role)
            self.assertEqual(code, fields["routing_code"]["default"])
        close = body_fields(workflow, "close_call")
        self.assertEqual("{{close_outcome}}", close["result_code"]["default"])
        self.assertEqual("{{close_reason}}", close["detail_code"]["default"])
        self.assertNotIn("{{routing_code}}", str(workflow["nodes"]["transfer_support"]))

    def test_qa_rejects_unbound_required_engine_fields(self) -> None:
        api = renamed_api()
        workflow = build_workflow(synthetic_policy(), api)
        for node, field in (("transfer_support", "routing_code"), ("close_call", "result_code")):
            broken = copy.deepcopy(workflow)
            body_fields(broken, node)[field]["default"] = "{{" + field + "}}"
            errors = validate_workflow(broken, api)
            self.assertTrue(any(field in error and "bind" in error for error in errors), errors)

    def test_ambiguous_request_semantics_fail_closed(self) -> None:
        self.assertRaisesRegex(ValueError, "unambiguous queue", transfer_request_fields,
                               {"properties": {"routing_code": {}, "destination_code": {}}})
        self.assertRaisesRegex(ValueError, "one outcome", close_request_fields,
                               {"properties": {"result_code": {}, "outcome_code": {}}})


if __name__ == "__main__":
    unittest.main()

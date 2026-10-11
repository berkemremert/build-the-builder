"""Customer identifiers in API requests are bound from the call variable."""

from __future__ import annotations

import copy
import unittest

from probe_changed_brief import synthetic_api, synthetic_policy
from qa import validate_workflow
from workflow_generator import build_workflow


def _renamed_api() -> dict:
    api = synthetic_api()
    for path_item in api["paths"].values():
        for operation in path_item.values():
            schema = operation["requestBody"]["content"]["application/json"]["schema"]
            schema["properties"]["client_id"] = schema["properties"].pop("customer_id")
            schema["required"] = ["client_id" if field == "customer_id" else field
                                  for field in schema["required"]]
    return api


def _api_actions(workflow: dict):
    for node in workflow["nodes"].values():
        for placement in ("tools", "pre_actions"):
            for action in node.get(placement, []):
                if action.get("function", {}).get("handler") == "__function__:api_call":
                    yield placement, action


class CustomerIdentifierWorkflowTests(unittest.TestCase):
    def test_renamed_customer_id_is_bound_in_model_and_engine_calls(self) -> None:
        api = _renamed_api()
        workflow = build_workflow(synthetic_policy(), api)
        self.assertEqual([], validate_workflow(workflow, api))
        placements = set()
        names = set()
        for placement, action in _api_actions(workflow):
            placements.add(placement)
            names.add(action["function"]["name"])
            body = action["function"]["parameters"]["properties"]["body"]
            self.assertEqual("{{customer_id}}", body["properties"]["client_id"]["default"])
            self.assertNotIn("customer_id", body["properties"])
        self.assertEqual({"tools", "pre_actions"}, placements)
        self.assertEqual({"verify_identity", "get_account", "create_promise", "add_note",
                          "schedule_callback", "transfer_to_agent", "close_call"}, names)

    def test_qa_rejects_unbound_client_id_in_model_and_engine_calls(self) -> None:
        api = _renamed_api()
        workflow = build_workflow(synthetic_policy(), api)
        for placement in ("tools", "pre_actions"):
            with self.subTest(placement=placement):
                broken = copy.deepcopy(workflow)
                action = next(action for kind, action in _api_actions(broken)
                              if kind == placement)
                body = action["function"]["parameters"]["properties"]["body"]
                body["properties"]["client_id"]["default"] = "{{client_id}}"
                self.assertTrue(any("client_id.default" in error and "{{customer_id}}" in error
                                    for error in validate_workflow(broken, api)))

    def test_ambiguous_customer_identifier_schema_fails_closed(self) -> None:
        api = _renamed_api()
        schema = (api["paths"]["/get_account"]["post"]["requestBody"]
                  ["content"]["application/json"]["schema"])
        schema["properties"]["customer_id"] = {"type": "string"}
        schema["required"].append("customer_id")
        with self.assertRaisesRegex(ValueError, "one required customer identifier"):
            build_workflow(synthetic_policy(), api)


if __name__ == "__main__":
    unittest.main()

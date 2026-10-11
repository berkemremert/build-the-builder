"""Bank write hooks follow renamed response success fields."""

from __future__ import annotations

import copy
import json
import unittest

from probe_changed_brief import synthetic_api, synthetic_policy
from qa import validate_workflow
from workflow_generator import build_workflow


def _renamed_api(operation: str, renamed: str) -> dict:
    api = synthetic_api()
    response = api["paths"]["/" + operation]["post"]["responses"]["200"]
    properties = response["content"]["application/json"]["schema"]["properties"]
    properties[renamed] = properties.pop("ok")
    response["description"] = f"`{renamed}` is true when the action was carried out."
    return api


def _actions(workflow: dict, name: str) -> list[dict]:
    return [action for node in workflow["nodes"].values()
            for action in [*node.get("tools", []), *node.get("pre_actions", [])]
            if action.get("function", {}).get("name") == name]


class ChangedWriteResultTests(unittest.TestCase):
    def test_renamed_note_success_updates_hook_and_engine_route(self) -> None:
        api = _renamed_api("add_note", "saved")
        workflow = build_workflow(synthetic_policy(), api)
        self.assertEqual([], validate_workflow(workflow, api))
        state = {"pending_note_product_id": "p1", "pending_note_reason": "R5", "note_reasons": "{}"}
        result = {"data": {"saved": True}}
        exec(_actions(workflow, "add_note")[0]["post_process_code"], {"vars": state, "result": result})
        self.assertEqual({"p1": "R5"}, json.loads(state["note_reasons"]))
        for name in ("add_note_call", "retry_note_call"):
            success = workflow["nodes"][name]["transitions"][0]
            self.assertIn(name + "_response.data.saved", str(success["condition"]))

    def test_renamed_transfer_and_callback_success_updates_hooks(self) -> None:
        for operation, field, expected_key in (("transfer_to_agent", "accepted", "transfer_reason"),
                                               ("schedule_callback", "booked", "callback_recorded")):
            with self.subTest(operation=operation):
                api = _renamed_api(operation, field)
                workflow = build_workflow(synthetic_policy(), api)
                self.assertEqual([], validate_workflow(workflow, api))
                state = {"pending_transfer_queue": "Q2", "pending_transfer_reason": "support"}
                exec(_actions(workflow, operation)[0]["post_process_code"],
                     {"vars": state, "result": {"data": {field: True}}})
                self.assertIn(expected_key, state)

    def test_ambiguous_write_result_fails_and_qa_detects_stale_binding(self) -> None:
        api = _renamed_api("add_note", "saved")
        response = api["paths"]["/add_note"]["post"]["responses"]["200"]
        response["content"]["application/json"]["schema"]["properties"]["ok"] = {"type": "boolean"}
        with self.assertRaisesRegex(ValueError, "unambiguous boolean write-success field"):
            build_workflow(synthetic_policy(), api)

        api = _renamed_api("add_note", "saved")
        workflow = build_workflow(synthetic_policy(), api)
        stale = copy.deepcopy(workflow)
        action = _actions(stale, "add_note")[0]
        action["post_process_code"] = action["post_process_code"].replace("data.get('saved')", "data.get('ok')")
        self.assertTrue(any("write-success field" in error for error in validate_workflow(stale, api)))
        stale = copy.deepcopy(workflow)
        stale["nodes"]["add_note_call"]["transitions"][0]["condition"]["conditions"][0]["variable"] = (
            "add_note_call_response.data.ok")
        self.assertTrue(any("write-success field" in error for error in validate_workflow(stale, api)))


if __name__ == "__main__":
    unittest.main()

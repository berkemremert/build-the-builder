"""Required close reasons remain valid when the outcome has no reason."""

from __future__ import annotations

import json
import unittest

from probe_changed_brief import _code, synthetic_api, synthetic_policy
from qa import validate_workflow
from workflow_generator import build_workflow


def _required_api() -> dict:
    api = synthetic_api()
    schema = api["paths"]["/close_call"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    schema["properties"]["reason"]["enum"].append("R0")
    schema["required"].append("reason")
    return api


def _mapped_policy() -> dict:
    policy = synthetic_policy()
    policy["raw_brief"] += "\n| Reason | Close code |\n|---|---|\n| none | R0 |\n"
    return policy


def _close_body(workflow: dict, state: dict) -> dict:
    close = workflow["nodes"]["close_call"]["pre_actions"][0]
    body = {"customer_id": "cust-1", "outcome": state.get("close_outcome", ""),
            "reason": state.get("close_reason", "")}
    exec(close["pre_process_code"], {"vars": state, "config": {"body": body}})
    return body


class RequiredCloseReasonIntegrationTests(unittest.TestCase):
    def test_required_reason_uses_source_code_for_promise_paid_and_failure(self) -> None:
        api = _required_api()
        workflow = build_workflow(_mapped_policy(), api)
        self.assertEqual([], validate_workflow(workflow, api))
        product = {"product_id": "p1", "past_due": 100, "floor": 20,
                   "deadline": "2031-04-24"}
        for recorded, notes, outcome in ((["p1"], {}, "C2"), ([], {"p1": "R1"}, "C3")):
            with self.subTest(outcome=outcome):
                state = {"product_json": json.dumps([product]),
                         "recorded_ids": json.dumps(recorded),
                         "note_reasons": json.dumps(notes), "promise_attempts": "{}"}
                exec(_code(workflow, "finalize"), {"vars": state})
                self.assertEqual(outcome, state["close_outcome"])
                body = _close_body(workflow, state)
                self.assertEqual({"outcome": outcome, "reason": "R0"},
                                 {key: body[key] for key in ("outcome", "reason")})
        failed = {"recorded_ids": '["p1"]', "prepare_account_failure_count": "1"}
        body = _close_body(workflow, failed)
        self.assertEqual(("C2", "R0"), (body["outcome"], body["reason"]))

    def test_optional_reason_is_omitted_and_unmapped_required_reason_fails(self) -> None:
        optional = synthetic_api()
        workflow = build_workflow(synthetic_policy(), optional)
        body = _close_body(workflow, {"close_outcome": "C2", "close_reason": ""})
        self.assertNotIn("reason", body)
        with self.assertRaisesRegex(ValueError, "no unique no-reason value"):
            build_workflow(synthetic_policy(), _required_api())


if __name__ == "__main__":
    unittest.main()

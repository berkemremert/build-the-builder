"""Local measurement of synthetic changed-brief behavior; run explicitly.

This file invents its bank, dates, codes, and API schemas. It makes no network
calls and is deliberately outside the default ``test_*.py`` test discovery.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qa import validate_workflow  # noqa: E402
from workflow_generator import build_workflow  # noqa: E402


def _field(kind: str = "string", *, choices: list[str] | None = None) -> dict:
    result = {"type": kind}
    if choices is not None:
        result["enum"] = choices
    return result


def _operation(body: dict, response: dict) -> dict:
    return {
        "post": {
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object", "properties": body, "required": list(body),
            }}}},
            "responses": {
                "200": {"description": "A true success flag confirms the write.",
                        "content": {"application/json": {"schema": {
                            "type": "object", "properties": response,
                        }}}},
                "500": {"description": "The operation failed; nothing was recorded."},
            },
        }
    }


def synthetic_api(*, identity_order: list[str] | None = None) -> dict:
    customer = {"customer_id": _field()}
    products = {"type": "array", "items": {"type": "object", "properties": {
        "product_id": _field(), "type": _field(), "past_due": _field("number"),
        "days_late": _field("integer"), "floor_amount": _field("number"),
    }}}
    paths = {
        "/verify_identity": _operation({**customer,
            "field": _field(choices=identity_order or ["birth_year", "passcode"]),
            "answer": _field()},
            {"ok": _field("boolean"), "verified": _field("boolean"),
             "all_verified": _field("boolean"), "wrong_answers_left": _field("integer")}),
        "/get_account": _operation(customer, {"ok": _field("boolean"), "products": products}),
        "/create_promise": _operation({**customer,
            "account_ref": _field(), "payment_total": _field("number"),
            "settlement_day": _field()}, {"saved": _field("boolean")}),
        "/add_note": _operation({**customer, "account_ref": _field(),
            "note_code": _field(choices=["R1", "R2", "R3", "R4", "R5"])},
            {"ok": _field("boolean")}),
        "/schedule_callback": _operation({**customer, "callback_day": _field(),
            "callback_slot": _field()}, {"ok": _field("boolean")}),
        "/transfer_to_agent": _operation({**customer,
            "queue": _field(choices=["Q1", "Q2", "Q3"])},
            {"ok": _field("boolean")}),
        "/close_call": _operation({**customer,
            "outcome": _field(choices=["C1", "C2", "C3", "C4", "C5", "C6"]),
            "reason": _field(choices=["R1", "R2", "R3", "R4", "R5"])},
            {"ok": _field("boolean")}),
    }
    close_schema = paths["/close_call"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    close_schema["required"].remove("reason")
    return {"openapi": "3.0.3", "servers": [{"url": "https://synthetic.invalid/v7"}],
            "paths": paths}


def synthetic_policy(*, identity: str = "Ask birth year, then passcode.") -> dict:
    initial = (
        "Meridian Bank records calls. Ask identity before account discussion. "
        "The bank holiday is 2031-04-18.\n"
        "| days_late | band | deadline | persuasion attempts |\n"
        "| 1 to 8 | early | the 9th business day after today | 1 |\n"
    )
    memo = (
        "Later memo overrides the prior calendar and deadline: "
        "2031-04-18 is no longer a bank holiday; 2031-04-21 is now a bank holiday. "
        "For 1 to 8 days late, deadline is the 5th business day after today "
        "with 2 persuasion attempts.\n"
        "Code list: promise=C2, paid=C3, transfer=C4, no promise=C5, "
        "identity failure=C6, not reached=C1. Support=Q2, human=Q3, "
        "restructuring=Q1.\n"
        "| Reason | Note code |\n|---|---|\n"
        "| refused | R5 |\n| hardship | R2 |\n| dispute | R3 |\n"
        "| paid | R1 |\n| service error | R4 |\n"
        "| Reason | Close code |\n|---|---|\n"
        "| refused | R5 |\n| hardship | R2 |\n| dispute | R3 |\n"
        "| service error | R4 |\n| human | R1 |\n| support | R2 |\n"
        "| restructuring | R3 |\n| failed | R1 |\n| declined | R2 |\n"
        "| callback | R1 |\n| no callback | R2 |\n| wrong number | R3 |\n"
        "| deceased | R4 |\n| unclear | R5 |\n"
    )
    source = initial + memo
    return {
        "global_rules": source,
        "opening": "Say this is Meridian Bank and the call is recorded.",
        "identity": identity,
        "verified_call": "After identity, discuss the amount and deadline in the later memo.",
        "outcomes": {"promise": "C2", "paid": "C3", "transferred": "C4",
                     "no_promise": "C5", "not_verified": "C6", "not_reached": "C1"},
        "tool_policy": {}, "raw_brief": source,
        "constraints": {"bank_name": "Meridian Bank", "identity_order": identity,
                        "outcome_codes": memo},
    }


def _code(workflow: dict, name: str) -> str:
    return workflow["nodes"][name]["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"]


def _api_action(workflow: dict, name: str) -> dict:
    for node in workflow["nodes"].values():
        for action in [*node.get("pre_actions", []), *node.get("tools", [])]:
            if action.get("function", {}).get("name") == name:
                return action
    raise AssertionError(f"Missing action {name}")


class ChangedBriefProbe(unittest.TestCase):
    def test_later_memo_replaces_holiday_and_deadline(self) -> None:
        workflow = build_workflow(synthetic_policy(), synthetic_api())
        state = {"now": "2031-04-16T10:00", "account_products": [{
            "product_id": "a7", "type": "reserve_account", "past_due": 87,
            "days_late": 4, "floor_amount": 21,
        }]}
        exec(_code(workflow, "prepare_account"), {"vars": state})
        product = json.loads(state["product_json"])[0]
        with self.subTest("deadline override"):
            self.assertEqual("2031-04-24", product["deadline"],
                             "later memo sets five business days and replaces April 18 holiday")
        with self.subTest("revoked holiday"):
            self.assertNotIn("2031-04-18", _code(workflow, "prepare_confirmation"),
                             "revoked holiday must not remain in date validation")

    def test_identity_sequence_comes_from_brief_not_enum_position(self) -> None:
        policy = synthetic_policy(identity="Ask passcode first, then birth year.")
        workflow = build_workflow(policy, synthetic_api(identity_order=["birth_year", "passcode"]))
        first = workflow["nodes"]["identity_dob"]["tools"][0]
        first_field = first["function"]["parameters"]["properties"]["body"]["properties"]["field"]["default"]
        self.assertEqual("passcode", first_field)

    def test_opaque_codes_and_renamed_fields_are_semantically_wired(self) -> None:
        api = synthetic_api()
        workflow = build_workflow(synthetic_policy(), api)
        self.assertEqual([], validate_workflow(workflow, api),
                         "structural QA should establish the baseline for this probe")
        with self.subTest("opaque outcome mapping"):
            self.assertIn("'C6'", _code(workflow, "finish_identity_failed"),
                          "identity failure should map to C6, not the first allowed code")
        support = workflow["nodes"]["transfer_support"]["pre_actions"][0]
        support_queue = support["function"]["parameters"]["properties"]["body"]["properties"]["queue"]["default"]
        with self.subTest("opaque support queue"):
            self.assertEqual("Q2", support_queue)
        promise = _api_action(workflow, "create_promise")
        body = promise["function"]["parameters"]["properties"]["body"]["properties"]
        for field, expected in (("account_ref", "{{pending_product_id}}"),
                                ("payment_total", "{{pending_amount}}"),
                                ("settlement_day", "{{pending_date}}")):
            with self.subTest(renamed_promise_field=field):
                self.assertEqual(expected, body[field]["default"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

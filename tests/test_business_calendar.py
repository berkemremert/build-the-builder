"""Source weekday calendars must reach every generated date check."""

from __future__ import annotations

import json
import unittest

from business_calendar import business_weekdays
from probe_changed_brief import _code, synthetic_api, synthetic_policy
from qa import validate_workflow
from workflow_generator import build_workflow


def _changed_policy() -> dict:
    policy = synthetic_policy()
    policy["raw_brief"] += (
        "\nLater calendar memo: Business days now include Saturday. "
        "Workdays run Monday through Saturday; only Sunday and named bank holidays are closed. "
        "Offer on the last business day of this week."
    )
    return policy


class BusinessCalendarTests(unittest.TestCase):
    def test_later_calendar_override_and_ambiguous_rule(self) -> None:
        self.assertEqual(tuple(range(6)), business_weekdays(_changed_policy()))
        policy = _changed_policy()
        policy["raw_brief"] += " Later memo: Business days are Monday through Friday or Saturday."
        with self.assertRaisesRegex(ValueError, "Ambiguous business weekday"):
            business_weekdays(policy)

    def test_saturday_reaches_deadline_offer_and_date_checks(self) -> None:
        api = synthetic_api()
        workflow = build_workflow(_changed_policy(), api)
        self.assertEqual([], validate_workflow(workflow, api))
        state = {"now": "2031-04-25T10:00", "account_products": [{
            "product_id": "a7", "type": "reserve_account", "past_due": 87,
            "days_late": 4, "floor_amount": 21,
        }]}
        exec(_code(workflow, "prepare_account"), {"vars": state})
        product = json.loads(state["product_json"])[0]
        self.assertEqual("2031-05-01", product["deadline"])
        self.assertEqual("2031-04-26", state["last_business_day_offer"])
        state["proposed_terms_json"] = json.dumps([{
            "product_id": "a7", "amount": 21, "date": "2031-04-26",
        }])
        exec(_code(workflow, "prepare_confirmation"), {"vars": state})
        self.assertEqual("ready", state["proposal_validation_state"], state.get("proposal_error"))
        self.assertEqual("2031-04-26", json.loads(state["readback_terms_json"])[0]["date"])
        state["confirmation_status"] = "confirmed"
        exec(_code(workflow, "validate_confirmation"), {"vars": state})
        self.assertEqual("ready", state["validation_state"])
        self.assertEqual("2031-04-26", json.loads(state["pending_promises_json"])[0]["date"])
        self.assertIn("Saturday", workflow["nodes"]["third_party"]["task_message"])
        callback = workflow["nodes"]["third_party"]["tools"][0]
        guard = callback["pre_process_code"]
        exec(guard, {"vars": state, "config": {"body": {"callback_day": "2031-04-26"}}})
        with self.assertRaisesRegex(ValueError, "not a business day"):
            exec(guard, {"vars": state, "config": {"body": {"callback_day": "2031-04-27"}}})


if __name__ == "__main__":
    unittest.main()

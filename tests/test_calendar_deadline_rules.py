"""Source-grounded calendar and overdue-band extraction regressions."""

from __future__ import annotations

import json
import unittest

from probe_changed_brief import _code, synthetic_api, synthetic_policy
from qa import validate_workflow
from workflow_generator import _deadline_bands, _holiday_dates, build_workflow


def _policy(raw: str, constraints: dict | None = None) -> dict:
    policy = synthetic_policy()
    policy["raw_brief"] = raw + "\nQueue codes: support=Q2, human=Q3, restructuring=Q1."
    policy["constraints"] = constraints or {}
    return policy


class CalendarDeadlineRulesTests(unittest.TestCase):
    def test_prose_rule_and_month_name_holiday_reach_account_date(self) -> None:
        policy = _policy(
            "Accounts overdue from 1 through 8 days may use two persuasion attempts. "
            "Their deadline falls five business days after the call date. "
            "April 21, 2031 is a bank holiday."
        )
        self.assertEqual([{"low": 1, "high": 8, "label": "", "business_days": 5, "attempts": 2}],
                         _deadline_bands(policy))
        self.assertEqual(["2031-04-21"], _holiday_dates(policy))
        workflow = build_workflow(policy, synthetic_api())
        self.assertEqual([], validate_workflow(workflow, synthetic_api()))
        state = {"now": "2031-04-16T10:00", "account_products": [{
            "product_id": "p1", "type": "reserve_account", "past_due": 100,
            "floor_amount": 20, "days_late": 4,
        }]}
        exec(_code(workflow, "prepare_account"), {"vars": state})
        item = json.loads(state["product_json"])[0]
        self.assertEqual(("2031-04-24", 2), (item["deadline"], item["attempts"]))

    def test_later_prose_memo_overrides_band_and_calendar(self) -> None:
        policy = _policy(
            "Accounts overdue from 1 through 8 days may use one persuasion attempt. "
            "Their deadline falls three business days after the call date. "
            "April twenty-first, 2031 is a bank holiday.\n\n"
            "Later memo: For 1 through 8 days overdue, use two persuasion attempts. "
            "The deadline falls five business days after the call date. "
            "April twenty-first, 2031 is no longer a bank holiday; "
            "the twenty-second of April 2031 is now a bank holiday."
        )
        self.assertEqual(["2031-04-22"], _holiday_dates(policy))
        self.assertEqual((5, 2), tuple(_deadline_bands(policy)[-1][key]
                                       for key in ("business_days", "attempts")))
        workflow = build_workflow(policy, synthetic_api())
        state = {"now": "2031-04-16T10:00", "account_products": [{
            "product_id": "p1", "type": "reserve_account", "past_due": 100,
            "floor_amount": 20, "days_late": 4,
        }]}
        exec(_code(workflow, "prepare_account"), {"vars": state})
        item = json.loads(state["product_json"])[0]
        self.assertEqual(("2031-04-24", 2), (item["deadline"], item["attempts"]))

    def test_structured_fallback_requires_source_rule(self) -> None:
        raw = ("For accounts 1 to 8 days late, two persuasion attempts apply; "
               "their deadline is five working days after the call date.")
        band = {"low": 1, "high": 8, "business_days": 5, "attempts": 2}
        self.assertEqual([{**band, "label": ""}],
                         _deadline_bands(_policy(raw, {"deadline_bands": [band]})))
        self.assertEqual([], _deadline_bands(_policy("No date or attempt rule.",
                                                      {"deadline_bands": [band]})))

    def test_unparseable_mandatory_deadline_fails_before_workflow(self) -> None:
        policy = _policy("Accounts overdue from 1 through 8 days may use two persuasion "
                         "attempts. Their deadline falls after the bank's unstated review window.")
        with self.assertRaisesRegex(ValueError, "Cannot establish source deadline"):
            build_workflow(policy, synthetic_api())
        policy = _policy(
            "For 1 to 8 days late, the deadline is the third business day after today "
            "with one persuasion attempt. Later memo: For 1 to 8 days late, "
            "two persuasion attempts apply and the deadline follows an unstated review window."
        )
        with self.assertRaisesRegex(ValueError, "Cannot establish source deadline"):
            build_workflow(policy, synthetic_api())


if __name__ == "__main__":
    unittest.main()

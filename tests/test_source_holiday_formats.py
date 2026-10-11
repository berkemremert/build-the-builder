"""Raw holiday lists and slash dates follow source order without date guessing."""

from __future__ import annotations

import json
import unittest

from probe_changed_brief import _code, synthetic_api, synthetic_policy
from workflow_generator import _holiday_dates, build_workflow


class SourceHolidayFormatsTests(unittest.TestCase):
    def test_heading_lists_and_tables(self) -> None:
        for source in (
            "Bank holidays:\n- 2031-04-21\n- 2031-05-01",
            "| Holiday date | Name |\n|---|---|\n| 2031-04-21 | Spring closure |\n| 2031-05-01 | May closure |",
            "Holiday calendar:\n- 2031-04-21 Spring closure\n- 2031-05-01 May closure",
        ):
            with self.subTest(source=source):
                self.assertEqual(["2031-04-21", "2031-05-01"], _holiday_dates({"raw_brief": source}))

    def test_slash_dates_order_validation_and_revocation(self) -> None:
        source = ("Bank holidays: 21/04/2031. Later memo: 21/04/2031 is no longer a bank holiday; "
                  "2031/05/01 is now a bank holiday.")
        self.assertEqual(["2031-05-01"], _holiday_dates({"raw_brief": source}))
        for source, expected in (
            ("Dates use DD/MM/YYYY. Bank holidays: 04/05/2031.", "2031-05-04"),
            ("Dates use MM/DD/YYYY. Bank holidays: 04/05/2031.", "2031-04-05"),
        ):
            with self.subTest(source=source):
                self.assertEqual([expected], _holiday_dates({"raw_brief": source}))
        with self.assertRaisesRegex(ValueError, "Ambiguous source slash date"):
            _holiday_dates({"raw_brief": "Bank holidays: 04/05/2031."})
        with self.assertRaisesRegex(ValueError, "Invalid source slash date"):
            _holiday_dates({"raw_brief": "Bank holidays: 21/04/2031 and 31/02/2031."})
        self.assertEqual(["2031-04-21"], _holiday_dates({"raw_brief":
            "Example account opened 04/05/2031. Bank holidays: 21/04/2031."}))
        with self.assertRaisesRegex(ValueError, "Cannot establish bank holiday dates"):
            _holiday_dates({"raw_brief": "Example account opened 2031-04-21. Bank holidays: TBD."})

    def test_holiday_list_changes_generated_deadline(self) -> None:
        policy = synthetic_policy()
        policy["raw_brief"] = policy["raw_brief"].replace(
            "2031-04-21 is now a bank holiday.",
            "2031-04-21 is no longer a bank holiday.\nBank holidays:\n- 21/04/2031")
        workflow = build_workflow(policy, synthetic_api())
        state = {"now": "2031-04-16T10:00", "account_products": [{
            "product_id": "a7", "type": "reserve_account", "past_due": 87,
            "days_late": 4, "floor_amount": 21,
        }]}
        exec(_code(workflow, "prepare_account"), {"vars": state})
        self.assertEqual("2031-04-24", json.loads(state["product_json"])[0]["deadline"])


if __name__ == "__main__":
    unittest.main()

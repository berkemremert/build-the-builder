"""Offline compatibility checks for the staged Bank B long-call personas."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import unittest

from tools import run_cases
from workflow_generator import _deadline_bands, _holiday_dates


ROOT = Path(__file__).resolve().parents[1]


class LongCallStressTests(unittest.TestCase):
    def test_cases_match_published_fixtures_and_brief_limits(self) -> None:
        public = {case["id"]: case for case in json.loads((ROOT / "public-test-set.json").read_text())}
        cases = json.loads((ROOT / "long-call-stress.json").read_text())
        self.assertEqual(["LONG-01", "LONG-02", "LONG-03", "LONG-04", "LONG-05"],
                         [case["id"] for case in cases])
        self.assertEqual(["targeted"] * 3 + ["soak"] * 2,
                         [case["stress_tier"] for case in cases])

        brief = (ROOT / "briefs" / "brief-a.md").read_text()
        holidays = set(_holiday_dates({"raw_brief": brief}))
        bands = _deadline_bands({"raw_brief": brief})
        for case in cases:
            with self.subTest(case=case["id"]):
                seed = public[case["source_case"]]
                self.assertEqual("B", seed["brief"])
                self.assertEqual("B", case["brief"])
                for field in ("variables", "identity", "products"):
                    self.assertEqual(seed[field], case[field])
                self.assertEqual("pieces", case["style"])
                turns = case["planned_caller_turns"]
                self.assertEqual(len(turns), case["target_caller_turns"])
                self.assertTrue(12 <= len(turns) <= 15 if case["stress_tier"] == "targeted"
                                else len(turns) == 25)
                self.assertTrue(all(isinstance(turn, str) and turn.strip() for turn in turns))
                for index, turn in enumerate(turns, 1):
                    self.assertIn(f"{index}. {turn}", case["script"])
                persona = run_cases.persona_for(case, {"identity": case["identity"]})
                self.assertIn(case["script"], persona["prompt"])
                self.assertIn(run_cases.STYLES["pieces"], persona["prompt"])
                self.assertTrue(case["checks"] and case["rules"])

                expected = case["expected"]
                self.assertEqual({"outcome", "reason", "promises", "notes", "callbacks", "transfers"},
                                 set(expected))
                self.assertEqual(("promise", None), (expected["outcome"], expected["reason"]))
                self.assertEqual(([], []), (expected["callbacks"], expected["transfers"]))
                products = {f"p{index}": item for index, item in enumerate(case["products"], 1)}
                promised = [item["product_id"] for item in expected["promises"]]
                noted = [item["product_id"] for item in expected["notes"]]
                self.assertEqual(len(promised), len(set(promised)))
                self.assertEqual(len(noted), len(set(noted)))
                self.assertFalse(set(promised) & set(noted))
                self.assertTrue(set(promised + noted) <= set(products))
                self.assertEqual(set(products), set(promised + noted))
                self.assertTrue(all(note["reason"] in {"refused", "hardship", "dispute", "paid", "service_error"}
                                    for note in expected["notes"]))

                today = dt.date.fromisoformat(case["variables"]["now"][:10])
                for promise in expected["promises"]:
                    product = products[promise["product_id"]]
                    floor = product.get("minimum_payment", product.get("instalment_amount"))
                    self.assertLessEqual(float(floor), promise["amount"])
                    self.assertLessEqual(promise["amount"], product["past_due"])
                    day = dt.date.fromisoformat(promise["date"])
                    self.assertGreaterEqual(day, today)
                    self.assertLess(day.weekday(), 5)
                    self.assertNotIn(day.isoformat(), holidays)
                    band = next(rule for rule in bands
                                if rule["low"] <= product["days_late"] <= rule["high"])
                    deadline = today
                    business_days = 0
                    while business_days < band["business_days"]:
                        deadline += dt.timedelta(days=1)
                        if deadline.weekday() < 5 and deadline.isoformat() not in holidays:
                            business_days += 1
                    self.assertLessEqual(day, deadline)


if __name__ == "__main__":
    unittest.main()

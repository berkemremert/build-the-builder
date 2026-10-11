"""Focused offline checks for caller-relative payment-date interpretation."""

from __future__ import annotations

import datetime
import inspect
import json
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from payment_dates import parse_payment_date  # noqa: E402
from policy_compiler import compile_policy  # noqa: E402
from workflow_generator import build_workflow  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


class PaymentDateTests(unittest.TestCase):
    def test_month_day_rolls_into_next_year(self) -> None:
        today = datetime.date(2026, 12, 28)
        for phrase in ("the second of January", "January second", "January 2nd", "2 January"):
            with self.subTest(phrase=phrase):
                self.assertEqual(datetime.date(2027, 1, 2), parse_payment_date(phrase, today))

    def test_explicit_years_in_digits_and_words(self) -> None:
        today = datetime.date(2026, 12, 28)
        for phrase in ("the second of January 2027", "the second of January twenty twenty seven",
                       "January 2, two thousand and twenty seven", "2 January twenty twenty-seven"):
            with self.subTest(phrase=phrase):
                self.assertEqual(datetime.date(2027, 1, 2), parse_payment_date(phrase, today))
        self.assertEqual(datetime.date(2026, 1, 2),
                         parse_payment_date("the second of January twenty twenty six", today))

    def test_ordinary_spoken_dates_and_weekdays(self) -> None:
        today = datetime.date(2026, 10, 14)  # Wednesday
        cases = {
            "Monday the twenty sixth": datetime.date(2026, 10, 26),
            "the fifteenth of October": datetime.date(2026, 10, 15),
            "October 15th": datetime.date(2026, 10, 15),
            "2026-10-15": datetime.date(2026, 10, 15),
            "this Friday": datetime.date(2026, 10, 16),
            "next Friday": datetime.date(2026, 10, 23),
            "Friday": datetime.date(2026, 10, 16),
            "tomorrow": datetime.date(2026, 10, 15),
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(expected, parse_payment_date(phrase, today))

    def test_explicit_correction_uses_last_complete_date(self) -> None:
        today = datetime.date(2026, 10, 14)
        for phrase in ("21 October, sorry, I mean 20 October",
                       "the twenty-first of October, sorry, I mean the twentieth of October",
                       "October 21, actually October 20",
                       "next Friday, no, this Friday, no, Tuesday the twentieth of October"):
            with self.subTest(phrase=phrase):
                self.assertEqual(datetime.date(2026, 10, 20), parse_payment_date(phrase, today))
        for phrase in ("21 October, sorry, I mean October", "October 21, wait, no",
                       "21 October or 20 October", "21 October, 20 October",
                       "2026-10-21, 2026-10-20"):
            with self.subTest(phrase=phrase), self.assertRaises(ValueError):
                parse_payment_date(phrase, today)

    def test_ambiguous_or_inconsistent_dates_fail(self) -> None:
        today = datetime.date(2026, 12, 28)
        for phrase in ("Tuesday the second of January 2027", "January 2 2027 or 2028",
                       "the second of January or February",
                       "the thirty second of January", "sometime in January"):
            with self.subTest(phrase=phrase), self.assertRaises(ValueError):
                parse_payment_date(phrase, today)

    def test_parser_source_runs_inside_generated_code_context(self) -> None:
        namespace: dict = {}
        exec("import datetime\nimport re\n" + inspect.getsource(parse_payment_date), namespace)
        self.assertEqual(datetime.date(2027, 1, 2),
                         namespace["parse_payment_date"]("the second of January", datetime.date(2026, 12, 28)))


class GeneratedConfirmationDateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        api = json.loads((ROOT / "tools" / "openapi.json").read_text())
        policy = compile_policy([str(ROOT / "briefs" / "brief-a.md")],
                                str(ROOT / "tools" / "openapi.json"), None)
        workflow = build_workflow(policy, api)
        cls.code = (workflow["nodes"]["prepare_confirmation"]["pre_actions"][0]["function"]
                    ["parameters"]["properties"]["code"]["default"])

    def _prepare(self, phrase: str, *, now: str = "2026-12-28T11:00",
                 deadline: str = "2027-01-14") -> dict:
        state = {"now": now, "product_json": json.dumps([{
            "product_id": "p1", "label": "Loan", "floor": 100,
            "past_due": 400, "deadline": deadline,
        }]), "proposed_terms_json": json.dumps([{
            "product_id": "p1", "date": phrase,
        }])}
        exec(self.code, {"vars": state})
        return state

    def test_rolled_spoken_january_dates_reach_business_day_validation(self) -> None:
        for phrase in ("the second of January", "the second of January twenty twenty seven",
                       "January 2, 2027"):
            with self.subTest(phrase=phrase):
                state = self._prepare(phrase)
                self.assertEqual("ready", state["proposal_validation_state"], state["proposal_error"])
                self.assertEqual("2027-01-04", json.loads(state["readback_terms_json"])[0]["date"])
                self.assertIn("next business day", state["confirmation_readback"].lower())

    def test_explicit_past_year_still_fails(self) -> None:
        state = self._prepare("the second of January twenty twenty six")
        self.assertEqual("invalid", state["proposal_validation_state"])
        self.assertIn("Date outside approved range", state["proposal_error"])

    def test_weekday_and_deadline_behavior_stays_bounded(self) -> None:
        state = self._prepare("next Friday")
        self.assertEqual("ready", state["proposal_validation_state"], state["proposal_error"])
        self.assertEqual("2027-01-08", json.loads(state["readback_terms_json"])[0]["date"])
        state = self._prepare("the sixteenth of January")
        self.assertEqual("ready", state["proposal_validation_state"], state["proposal_error"])
        self.assertEqual("2027-01-14", json.loads(state["readback_terms_json"])[0]["date"])

    def test_generated_validation_uses_corrected_date(self) -> None:
        for phrase in ("21 October, sorry, I mean 20 October",
                       "the twenty-first of October, sorry, I mean the twentieth of October"):
            with self.subTest(phrase=phrase):
                state = self._prepare(phrase, now="2026-10-14T11:00", deadline="2026-10-30")
                self.assertEqual("ready", state["proposal_validation_state"], state["proposal_error"])
                self.assertEqual("2026-10-20", json.loads(state["readback_terms_json"])[0]["date"])
        unclear = self._prepare("21 October, sorry, I mean October",
                                now="2026-10-14T11:00", deadline="2026-10-30")
        self.assertEqual("invalid", unclear["proposal_validation_state"])


if __name__ == "__main__":
    unittest.main()

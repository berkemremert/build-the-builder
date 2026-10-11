"""Focused offline checks for caller-relative payment-date interpretation."""

from __future__ import annotations

import datetime
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from payment_dates import parse_payment_date  # noqa: E402


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

    def test_ambiguous_or_inconsistent_dates_fail(self) -> None:
        today = datetime.date(2026, 12, 28)
        for phrase in ("Tuesday the second of January 2027", "January 2 2027 or 2028",
                       "the thirty second of January", "sometime in January"):
            with self.subTest(phrase=phrase), self.assertRaises(ValueError):
                parse_payment_date(phrase, today)


if __name__ == "__main__":
    unittest.main()

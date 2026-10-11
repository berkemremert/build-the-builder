"""Source-order extraction for security questions."""

from __future__ import annotations

import unittest

from identity_order import latest_explicit_identity_order


class IdentityOrderTests(unittest.TestCase):
    def test_later_memo_wins(self) -> None:
        source = ("Ask birth year first, then passcode.\n"
                  "Later memo: replace identity order. Ask passcode first, then birth year.")
        self.assertEqual(["passcode", "birth_year"],
                         latest_explicit_identity_order(source, ["birth_year", "passcode"]))

    def test_real_brief_shape(self) -> None:
        source = "Ask two security questions, one at a time: first the date of birth, then the last four digits of the phone number on file."
        self.assertEqual(["date_of_birth", "phone_last4"],
                         latest_explicit_identity_order(source, ["phone_last4", "date_of_birth"]))

    def test_after_reverses_textual_order(self) -> None:
        self.assertEqual(["birth_year", "passcode"],
                         latest_explicit_identity_order("Ask passcode after birth year.",
                                                        ["passcode", "birth_year"]))

    def test_ambiguous_three_selectors_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Ambiguous identity question order"):
            latest_explicit_identity_order("Ask birth year first, then passcode, then date of birth.",
                                           ["birth_year", "passcode", "date_of_birth"])


if __name__ == "__main__":
    unittest.main()

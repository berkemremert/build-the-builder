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
        with self.assertRaisesRegex(ValueError, "more than two identity checks"):
            latest_explicit_identity_order("Ask birth year first, then passcode, then date of birth.",
                                           ["birth_year", "passcode", "date_of_birth"])

    def test_enumerated_three_checks_without_count_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "more than two identity checks"):
            latest_explicit_identity_order("Ask birth year, passcode, and postcode in that order.",
                                           ["birth_year", "passcode", "postcode"])

    def test_split_three_question_requirement_fails_closed(self) -> None:
        source = ("Ask birth year first. Then ask passcode. Then ask postcode. "
                  "All three must pass before get_account.")
        with self.assertRaisesRegex(ValueError, "more than two identity checks"):
            latest_explicit_identity_order(source, ["birth_year", "passcode", "postcode"])

    def test_three_wrong_answers_does_not_mean_three_questions(self) -> None:
        source = ("Ask two security questions: first birth year, then passcode. "
                  "Three wrong answers end the check.")
        self.assertEqual(["birth_year", "passcode"],
                         latest_explicit_identity_order(source, ["birth_year", "passcode", "postcode"]))


if __name__ == "__main__":
    unittest.main()

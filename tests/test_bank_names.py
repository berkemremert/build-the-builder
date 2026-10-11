"""Bank identity follows later source amendments."""

from __future__ import annotations

import unittest

from bank_names import source_bank_name
from workflow_generator import _bank_name


class BankNameTests(unittest.TestCase):
    def test_later_rename_and_exact_opening(self) -> None:
        source = ("Meridian Bank records calls.\n\n"
                  "Later memo: the bank has been renamed Harbor Bank. "
                  'First sentence must say exactly: "Hello, this is Harbor Bank\'s digital assistant."')
        self.assertEqual("Harbor Bank", source_bank_name(source))

    def test_same_revision_conflicting_names_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Conflicting bank names"):
            source_bank_name("The agent represents Meridian Bank.\n\n"
                             "A later paragraph calls it Harbor Bank without an amendment.")

    def test_conflicting_directives_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Conflicting bank names"):
            source_bank_name("The bank is now called Harbor Bank. "
                             'First sentence must say exactly: "Hello from Meridian Bank."')

    def test_finance_institution_from_source_title_and_call_rule(self) -> None:
        source = ("# Brief A: Meridian Finance, collections\n\n"
                  "Meridian Finance records calls. The caller is Daniel Reyes.\n"
                  "First sentence: say you are a digital assistant calling from Meridian Finance.")
        self.assertEqual("Meridian Finance", source_bank_name(source))

    def test_finance_name_overrides_stale_extracted_institution(self) -> None:
        policy = {"raw_brief": "Meridian Finance records calls.",
                  "constraints": {"bank_name": "the bank"}}
        self.assertEqual("Meridian Finance", _bank_name(policy))

    def test_credit_union_name_still_resolves(self) -> None:
        self.assertEqual("Harbor Credit Union", source_bank_name(
            "Harbor Credit Union records calls for its members."))

    def test_no_unrelated_person_or_date_as_institution(self) -> None:
        self.assertIsNone(source_bank_name(
            "The caller is Daniel Reyes. Next Monday, ask about the account."))
        self.assertIsNone(source_bank_name(
            'First sentence must say exactly: "Thank You for calling."'))


if __name__ == "__main__":
    unittest.main()

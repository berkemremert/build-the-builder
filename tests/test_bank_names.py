"""Bank identity follows later source amendments."""

from __future__ import annotations

import unittest

from bank_names import source_bank_name


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


if __name__ == "__main__":
    unittest.main()

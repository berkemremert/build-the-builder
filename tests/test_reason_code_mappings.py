"""Source reason code tables are scoped, ordered, and enum-checked."""

from __future__ import annotations

import unittest

from reason_code_mappings import explicit_reason_codes


class ReasonCodeMappingsTests(unittest.TestCase):
    def test_close_reason_table(self) -> None:
        source = ("| Reason | Close code |\n|---|---|\n| failed | R1 |\n"
                  "| declined | R2 |\n| wrong number | R3 |\n"
                  "| service error | R4 |\n| refused | R5 |")
        note, close = explicit_reason_codes(source, ["N1", "N2"], [f"R{i}" for i in range(1, 6)])
        self.assertEqual({}, note)
        self.assertEqual({"failed": "R1", "declined": "R2", "wrong_number": "R3",
                          "service_error": "R4", "refused": "R5"}, close)

    def test_later_amendment_replaces_earlier_code(self) -> None:
        source = ("Close reason code list: failed=R1.\nLater memo overrides the close code:\n"
                  "| Reason | Close code |\n|---|---|\n| failed | R2 |")
        self.assertEqual("R2", explicit_reason_codes(source, [], ["R1", "R2"])[1]["failed"])

    def test_note_table_and_shared_code_list_validate_each_api_enum(self) -> None:
        source = ("Reason code list: paid=R1, failed=R2.\n"
                  "| Reason | Note code |\n|---|---|\n| dispute | N1 |")
        note, close = explicit_reason_codes(source, ["R1", "N1"], ["R2"])
        self.assertEqual({"paid": "R1", "dispute": "N1"}, note)
        self.assertEqual({"failed": "R2"}, close)

    def test_conflict_and_wrong_enum_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Ambiguous close reason"):
            explicit_reason_codes("| Reason | Close code |\n|---|---|\n| failed | R1 |\n| failed | R2 |",
                                  [], ["R1", "R2"])
        with self.assertRaisesRegex(ValueError, "absent from close enum"):
            explicit_reason_codes("| Reason | Close code |\n|---|---|\n| failed | N1 |",
                                  ["N1"], ["R1"])

    def test_public_outcome_reason_table_does_not_supply_codes(self) -> None:
        self.assertEqual(({}, {}), explicit_reason_codes(
            "| Outcome | Reason | When |\n|---|---|---|\n| `promise` | none | after write |",
            ["paid"], ["failed"]))


if __name__ == "__main__":
    unittest.main()

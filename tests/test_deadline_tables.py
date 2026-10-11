"""Source-grounded overdue table parsing across ordinary Markdown shapes."""

from __future__ import annotations

import unittest

from deadline_tables import parse_deadline_tables


class DeadlineTableTests(unittest.TestCase):
    def test_compact_business_day_column(self) -> None:
        source = "| Overdue days | Deadline (business days) | Attempts |\n|---|---:|---:|\n| 1–8 | 5 | 2 |"
        self.assertEqual([(len(source.splitlines(keepends=True)[0]) +
                           len(source.splitlines(keepends=True)[1]),
                           {"low": 1, "high": 8, "label": "", "business_days": 5, "attempts": 2})],
                         parse_deadline_tables(source))

    def test_prose_business_day_cell(self) -> None:
        source = ("| Days late | Deadline | Persuasion attempts |\n"
                  "|---|---|---|\n| 1 to 8 | 5 business days after today | 2 |")
        self.assertEqual((5, 2), tuple(parse_deadline_tables(source)[0][1][key]
                                       for key in ("business_days", "attempts")))

    def test_later_table_row_keeps_source_order(self) -> None:
        source = ("| Days late | Deadline | Attempts |\n|---|---|---|\n"
                  "| 1 to 8 | 3 business days after today | 1 |\n\n"
                  "Later memo:\n| Overdue days | Deadline (business days) | Attempts |\n"
                  "|---|---:|---:|\n| 1–8 | 5 | 2 |")
        rows = parse_deadline_tables(source)
        self.assertEqual([(3, 1), (5, 2)],
                         [(band["business_days"], band["attempts"]) for _, band in rows])
        self.assertLess(rows[0][0], rows[1][0])

    def test_missing_unit_or_ambiguous_value_fails_closed(self) -> None:
        invalid = (
            "| Days late | Deadline | Attempts |\n|---|---|---|\n| 1–8 | 5 | 2 |",
            "| Days late | Deadline (business days) | Attempts |\n|---|---|---|\n| 1–8 | 5 | TBD |",
            "| Days late | Deadline (business days) | Attempts |\n|---|---|---|\n| 1–8 | 5 | 2 |\n| 9–15 | 4 | |",
        )
        for source in invalid:
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "Cannot establish source deadline"):
                parse_deadline_tables(source)


if __name__ == "__main__":
    unittest.main()

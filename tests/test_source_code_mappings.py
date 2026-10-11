"""Offline tests for source-labeled opaque API codes."""

from __future__ import annotations

import unittest

from source_code_mappings import explicit_code_mappings


class SourceCodeMappingsTests(unittest.TestCase):
    def test_markdown_tables_map_opaque_queues_and_outcomes(self) -> None:
        source = ("| Purpose | Queue |\n|---|---|\n| support | Q2 |\n"
                  "| human | Q3 |\n| restructuring | Q1 |\n\n"
                  "| Role | Outcome code |\n|---|---|\n| promise | C2 |\n"
                  "| paid | C3 |\n| transfer | C4 |\n| no promise | C5 |\n"
                  "| identity failure | C6 |\n| not reached | C1 |")
        self.assertEqual({"support": "Q2", "human": "Q3", "restructuring": "Q1"},
                         explicit_code_mappings(source, "queue", ["Q1", "Q2", "Q3"]))
        self.assertEqual({"promise": "C2", "paid": "C3", "transferred": "C4",
                          "no_promise": "C5", "not_verified": "C6", "not_reached": "C1"},
                         explicit_code_mappings(source, "outcome", [f"C{i}" for i in range(1, 7)]))

    def test_later_memo_overrides_earlier_code_list(self) -> None:
        source = ("Queue list: support=Q1, human=Q3. Outcome list: promise=C1.\n"
                  "Later memo overrides the earlier codes:\n"
                  "| Purpose | Queue |\n|---|---|\n| support | Q2 |\n"
                  "| Role | Outcome code |\n|---|---|\n| promise | C2 |")
        self.assertEqual("Q2", explicit_code_mappings(source, "queue", ["Q1", "Q2", "Q3"])["support"])
        self.assertEqual("C2", explicit_code_mappings(source, "outcome", ["C1", "C2"])["promise"])

    def test_code_list_accepts_backticked_labels_and_values(self) -> None:
        source = "Queue codes: `support`=`Q2`, `human`:`Q3`. Outcome codes: `identity_failure`=`C6`."
        self.assertEqual({"support": "Q2", "human": "Q3"},
                         explicit_code_mappings(source, "queue", ["Q2", "Q3"]))
        self.assertEqual({"not_verified": "C6"},
                         explicit_code_mappings(source, "outcome", ["C6"]))

    def test_conflicting_or_invalid_source_codes_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Ambiguous queue code"):
            explicit_code_mappings("support=Q1; support=Q2", "queue", ["Q1", "Q2"])
        with self.assertRaisesRegex(ValueError, "absent from OpenAPI enum"):
            explicit_code_mappings("| Role | Outcome code |\n|---|---|\n| paid | C9 |",
                                   "outcome", ["C1", "C2"])

    def test_public_outcome_reason_table_is_not_a_code_mapping(self) -> None:
        source = "| Outcome | Reason | When |\n|---|---|---|\n| `promise` | none | after write |"
        self.assertEqual({}, explicit_code_mappings(source, "outcome", ["promise", "paid"]))


if __name__ == "__main__":
    unittest.main()

"""Offline checks for strict extracted payment amounts."""

from __future__ import annotations

from pathlib import Path
import inspect
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from payment_amounts import parse_payment_amount  # noqa: E402


class PaymentAmountTests(unittest.TestCase):
    def test_spoken_whole_and_fractional_amounts(self) -> None:
        cases = {
            "four hundred and fifty-five": 455.0,
            "five hundred and ten": 510.0,
            "one thousand two hundred and thirty four": 1234.0,
            "four hundred and fifty-five pounds and twenty pence": 455.2,
            "one dollar and five cents": 1.05,
            "fifty pence": 0.5,
            "a hundred pounds": 100.0,
            "a thousand pounds": 1000.0,
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(expected, parse_payment_amount(phrase))

    def test_explicit_complete_correction_uses_final_amount(self) -> None:
        self.assertEqual(455.0, parse_payment_amount(
            "four hundred, sorry, I mean four hundred and fifty-five"))
        self.assertEqual(500.0, parse_payment_amount("four hundred, I mean five hundred"))

    def test_numeric_and_currency_inputs(self) -> None:
        for raw, expected in ((455, 455.0), (455.5, 455.5), ("455", 455.0),
                              ("455.50", 455.5), ("£1,234.56", 1234.56),
                              ("$45.05", 45.05), ("455 pounds", 455.0)):
            with self.subTest(raw=raw):
                self.assertEqual(expected, parse_payment_amount(raw))

    def test_ambiguous_incomplete_and_malformed_inputs_fail(self) -> None:
        for raw in ("four hundred or five hundred", "four hundred and",
                    "one thousand and", "a",
                    "four hundred, sorry, I mean", "four hundred, sorry, I mean five hundred and",
                    "four hundred, sorry, maybe five hundred", "one twenty", "forty fifty",
                    "one hundred and twenty five and ten", "pounds", "zero pounds and one hundred pence",
                    "£1,23", "455.555", "455 extra", "-20", "NaN", float("inf"), True, None):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_payment_amount(raw)

    def test_source_is_self_contained_for_generated_code_nodes(self) -> None:
        namespace: dict = {}
        exec(inspect.getsource(parse_payment_amount), namespace)
        self.assertEqual(455.0, namespace["parse_payment_amount"]("four hundred and fifty-five"))


if __name__ == "__main__":
    unittest.main()

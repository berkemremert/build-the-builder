"""Required close reasons need a valid code even for reasonless outcomes."""

from __future__ import annotations

import unittest

from close_reason_contract import required_no_reason_value


def _schema(*, required: bool, values: list[str]) -> dict:
    return {"type": "object", "properties": {
        "customer_id": {"type": "string"},
        "outcome": {"type": "string", "enum": ["promise", "paid", "no_promise"]},
        "reason": {"type": "string", "enum": values},
    }, "required": ["customer_id", "outcome", *(["reason"] if required else [])]}


class CloseReasonContractTests(unittest.TestCase):
    def test_optional_reason_needs_no_value(self) -> None:
        self.assertIsNone(required_no_reason_value(_schema(required=False, values=["R1"]), ""))

    def test_semantic_none_enum_is_usable(self) -> None:
        self.assertEqual("none", required_no_reason_value(
            _schema(required=True, values=["none", "refused"]), ""))

    def test_opaque_no_reason_requires_source_mapping(self) -> None:
        source = "| Reason | Close code |\n|---|---|\n| none | R0 |"
        self.assertEqual("R0", required_no_reason_value(
            _schema(required=True, values=["R0", "R1"]), source))

    def test_missing_or_ambiguous_no_reason_fails_closed(self) -> None:
        for values in (["R1", "R2"], ["none", "not_applicable"]):
            with self.subTest(values=values), self.assertRaisesRegex(
                    ValueError, "no unique no-reason value"):
                required_no_reason_value(_schema(required=True, values=values), "")


if __name__ == "__main__":
    unittest.main()

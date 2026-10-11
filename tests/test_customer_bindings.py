"""Request customer identifiers follow each supplied API schema."""

from __future__ import annotations

import unittest

from customer_bindings import customer_identifier_field


class CustomerBindingsTests(unittest.TestCase):
    def test_renamed_required_client_id(self) -> None:
        schema = {"type": "object", "properties": {
            "client_id": {"type": "string"}, "account_ref": {"type": "string"},
        }, "required": ["client_id", "account_ref"]}
        self.assertEqual("client_id", customer_identifier_field(schema))

    def test_description_can_identify_opaque_field(self) -> None:
        schema = {"type": "object", "properties": {
            "party_key": {"type": "string", "description": "Customer identifier for this call"},
            "product_ref": {"type": "string"},
        }, "required": ["party_key", "product_ref"]}
        self.assertEqual("party_key", customer_identifier_field(schema))

    def test_two_required_customer_ids_are_ambiguous(self) -> None:
        schema = {"type": "object", "properties": {
            "customer_id": {"type": "string"}, "client_id": {"type": "string"},
        }, "required": ["customer_id", "client_id"]}
        with self.assertRaisesRegex(ValueError, "one required customer identifier"):
            customer_identifier_field(schema)

    def test_customer_id_must_be_required(self) -> None:
        schema = {"type": "object", "properties": {"client_id": {"type": "string"}},
                  "required": []}
        with self.assertRaisesRegex(ValueError, "one required customer identifier"):
            customer_identifier_field(schema)


if __name__ == "__main__":
    unittest.main()

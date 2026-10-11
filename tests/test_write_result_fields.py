"""Focused response-contract inference checks."""

import unittest

from write_result_fields import write_success_field


def operation(*fields: str, description: str = "") -> dict:
    return {"responses": {"200": {"description": description,
            "content": {"application/json": {"schema": {"type": "object", "properties": {
                field: {"type": "boolean"} for field in fields}}}}}}}


class WriteResultFieldTests(unittest.TestCase):
    def test_operation_specific_names(self):
        for name, field in (("add_note", "saved"), ("transfer_to_agent", "accepted"),
                            ("schedule_callback", "booked")):
            with self.subTest(name=name):
                self.assertEqual(field, write_success_field(operation(field, "error_flag"), name))

    def test_description_can_identify_opaque_boolean(self):
        item = operation("write_flag", description="`write_flag` is true when the action was carried out.")
        self.assertEqual("write_flag", write_success_field(item, "add_note"))

    def test_ambiguous_or_unexplained_fields_fail(self):
        for fields in (("saved", "ok"), ("write_flag",), ()):
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, "unambiguous"):
                write_success_field(operation(*fields), "add_note")


if __name__ == "__main__":
    unittest.main()

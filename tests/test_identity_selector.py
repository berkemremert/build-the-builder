"""Identity request selector fields come from the supplied OpenAPI schema."""

from __future__ import annotations

import unittest

from api_bindings import identity_selector_field
from probe_changed_brief import synthetic_api


class IdentitySelectorTests(unittest.TestCase):
    def test_renamed_selector_uses_its_enum(self) -> None:
        schema = synthetic_api()["paths"]["/verify_identity"]["post"]
        schema = schema["requestBody"]["content"]["application/json"]["schema"]
        schema["properties"]["question_code"] = schema["properties"].pop("field")
        self.assertEqual(("question_code", ["birth_year", "passcode"]),
                         identity_selector_field(schema))

    def test_ambiguous_selector_fails_closed(self) -> None:
        schema = {"type": "object", "properties": {
            "choice_a": {"type": "string", "enum": ["A", "B"]},
            "choice_b": {"type": "string", "enum": ["X", "Y"]},
        }}
        with self.assertRaisesRegex(ValueError, "unambiguous security selector"):
            identity_selector_field(schema)


if __name__ == "__main__":
    unittest.main()

"""Offline checks for required OpenAPI parameter binding."""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from api_parameters import required_header_bindings  # noqa: E402
from probe_changed_brief import synthetic_api  # noqa: E402


def _parameter(name: str, location: str, *, required: bool = True,
               enum: list[str] | None = None, default: str | None = None) -> dict:
    schema: dict = {"type": "string"}
    if enum is not None:
        schema["enum"] = enum
    if default is not None:
        schema["default"] = default
    return {"name": name, "in": location, "required": required, "schema": schema}


class RequiredParameterTests(unittest.TestCase):
    def test_required_operation_header_uses_single_enum(self) -> None:
        api = synthetic_api()
        api["paths"]["/get_account"]["post"]["parameters"] = [
            _parameter("X-Channel", "header", enum=["voice"])]
        self.assertEqual({"X-Channel": {"type": "string", "default": "voice", "enum": ["voice"]}},
                         required_header_bindings(api, "/get_account", "POST"))

    def test_path_header_is_inherited_and_operation_can_override(self) -> None:
        api = synthetic_api()
        path = api["paths"]["/get_account"]
        path["parameters"] = [_parameter("X-Channel", "header", enum=["legacy"]),
                              _parameter("X-Region", "header", default="north")]
        path["post"]["parameters"] = [_parameter("x-channel", "header", enum=["voice"])]
        self.assertEqual({"x-channel": {"type": "string", "default": "voice", "enum": ["voice"]},
                          "X-Region": {"type": "string", "default": "north"}},
                         required_header_bindings(api, "/get_account", "post"))

    def test_local_parameter_and_schema_refs_are_resolved(self) -> None:
        api = synthetic_api()
        api["components"] = {"parameters": {"Channel": {
            "name": "X-Channel", "in": "header", "required": True,
            "schema": {"$ref": "#/components/schemas/Channel"}}},
            "schemas": {"Channel": {"type": "string", "enum": ["voice"]}}}
        api["paths"]["/get_account"]["parameters"] = [{"$ref": "#/components/parameters/Channel"}]
        self.assertEqual("voice", required_header_bindings(api, "/get_account", "post")
                         ["X-Channel"]["default"])

    def test_unsupported_required_path_and_query_fail_closed(self) -> None:
        api = synthetic_api()
        operation = api["paths"]["/get_account"]["post"]
        operation["parameters"] = [_parameter("locale", "query", default="en")]
        with self.assertRaisesRegex(ValueError, "unsupported query parameter"):
            required_header_bindings(api, "/get_account", "post")
        api = copy.deepcopy(api)
        api["paths"]["/get_account/{account}"] = api["paths"].pop("/get_account")
        with self.assertRaisesRegex(ValueError, "unsupported path parameter"):
            required_header_bindings(api, "/get_account/{account}", "post")

    def test_ambiguous_or_sensitive_required_headers_fail_closed(self) -> None:
        api = synthetic_api()
        operation = api["paths"]["/get_account"]["post"]
        operation["parameters"] = [_parameter("X-Channel", "header", enum=["voice", "web"])]
        with self.assertRaisesRegex(ValueError, "needs one fixed"):
            required_header_bindings(api, "/get_account", "post")
        operation["parameters"] = [_parameter("Authorization", "header", default="secret")]
        with self.assertRaisesRegex(ValueError, "credential header"):
            required_header_bindings(api, "/get_account", "post")


if __name__ == "__main__":
    unittest.main()

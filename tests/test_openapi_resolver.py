"""Local OpenAPI component resolution and builder/QA contract tests."""

from __future__ import annotations

import copy
import unittest

from openapi_resolver import normalize_openapi, resolve_schema
from probe_changed_brief import synthetic_api, synthetic_policy
from qa import validate_workflow
from workflow_generator import build_workflow


def referenced_api() -> dict:
    api = synthetic_api()
    components: dict = {"schemas": {}, "requestBodies": {}, "responses": {}}
    api["components"] = components
    for name in ("verify_identity", "create_promise"):
        operation = api["paths"]["/" + name]["post"]
        original = operation["requestBody"]
        components["requestBodies"][name] = copy.deepcopy(original)
        operation["requestBody"] = {"$ref": "#/components/requestBodies/" + name}
        body = components["requestBodies"][name]["content"]["application/json"]
        schema = body["schema"]
        components["schemas"][name] = copy.deepcopy(schema)
        body["schema"] = {"$ref": "#/components/schemas/" + name}
    for name in ("verify_identity", "get_account", "create_promise"):
        operation = api["paths"]["/" + name]["post"]
        original = operation["responses"]["200"]
        components["responses"][name] = copy.deepcopy(original)
        operation["responses"]["200"] = {"$ref": "#/components/responses/" + name}
        content = components["responses"][name]["content"]["application/json"]
        components["schemas"][name + "Result"] = copy.deepcopy(content["schema"])
        content["schema"] = {"$ref": "#/components/schemas/" + name + "Result"}
    products = components["schemas"]["get_accountResult"]["properties"]["products"]
    components["schemas"]["Product"] = copy.deepcopy(products["items"])
    products["items"] = {"$ref": "#/components/schemas/Product"}
    return api


class OpenApiResolverTests(unittest.TestCase):
    def test_component_request_response_and_nested_item_refs(self) -> None:
        api = referenced_api()
        original = copy.deepcopy(api)
        normalized = normalize_openapi(api)
        self.assertEqual(original, api, "resolver must not rewrite caller's source OpenAPI")
        verify = normalized["paths"]["/verify_identity"]["post"]
        request = verify["requestBody"]["content"]["application/json"]["schema"]
        self.assertIn("field", request["required"])
        self.assertEqual(["birth_year", "passcode"], request["properties"]["field"]["enum"])
        account = normalized["paths"]["/get_account"]["post"]
        products = account["responses"]["200"]["content"]["application/json"]["schema"]["properties"]["products"]
        self.assertEqual("string", products["items"]["properties"]["product_id"]["type"])
        workflow = build_workflow(synthetic_policy(), normalized)
        self.assertEqual([], validate_workflow(workflow, api))

    def test_allof_merges_required_fields_and_enums(self) -> None:
        api = synthetic_api()
        components = api["components"] = {"schemas": {}}
        schema = api["paths"]["/verify_identity"]["post"]["requestBody"]["content"]["application/json"]
        original = schema["schema"]
        components["schemas"]["IdentityBase"] = {
            "type": "object", "properties": {"customer_id": original["properties"]["customer_id"]},
            "required": ["customer_id"],
        }
        components["schemas"]["IdentityFields"] = {
            "type": "object", "properties": {key: original["properties"][key]
                                            for key in ("field", "answer")},
            "required": ["field", "answer"],
        }
        schema["schema"] = {"allOf": [
            {"$ref": "#/components/schemas/IdentityBase"},
            {"$ref": "#/components/schemas/IdentityFields"},
        ]}
        normalized = normalize_openapi(api)
        resolved = normalized["paths"]["/verify_identity"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(["customer_id", "field", "answer"], resolved["required"])
        self.assertEqual(["birth_year", "passcode"], resolved["properties"]["field"]["enum"])
        workflow = build_workflow(synthetic_policy(), normalized)
        self.assertEqual([], validate_workflow(workflow, api))

    def test_unsupported_external_missing_and_cycle_refs_fail_explicitly(self) -> None:
        for reference, expected in (("https://example.invalid/schema.json", "only local JSON pointers"),
                                    ("#/components/schemas/Missing", "does not resolve")):
            with self.subTest(reference=reference), self.assertRaisesRegex(ValueError, expected):
                resolve_schema({"$ref": reference}, synthetic_api())
        api = synthetic_api()
        api["components"] = {"schemas": {"A": {"$ref": "#/components/schemas/B"},
                                           "B": {"$ref": "#/components/schemas/A"}}}
        api["paths"]["/verify_identity"]["post"]["requestBody"]["content"]["application/json"]["schema"] = {
            "$ref": "#/components/schemas/A"}
        with self.assertRaisesRegex(ValueError, "Cyclic OpenAPI reference"):
            normalize_openapi(api)
        workflow = build_workflow(synthetic_policy(), synthetic_api())
        self.assertTrue(any("Cyclic OpenAPI reference" in error
                            for error in validate_workflow(workflow, api)))


if __name__ == "__main__":
    unittest.main()

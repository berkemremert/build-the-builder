"""Contract checks for nested generic API request bindings."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generic_workflow_fallback import build_generic_workflow
from qa import validate_generic_workflow


class GenericRequestValidationTests(unittest.TestCase):
    def setUp(self):
        self.spec = {"openapi": "3.0.3", "servers": [{"url": "https://example.test"}],
                     "paths": {"/read": {"post": {"operationId": "readAccount",
                         "summary": "Read account details",
                         "requestBody": {"content": {"application/json": {"schema": {
                             "type": "object", "properties": {"data": {"type": "object",
                                 "properties": {"customerNo": {"type": "string"},
                                                "channel": {"type": "string", "enum": ["IVR"]}},
                                 "required": ["customerNo", "channel"]}},
                             "required": ["data"]}}}},
                         "responses": {"200": {"description": "Success", "content": {
                             "application/json": {"schema": {"type": "object", "properties": {
                                 "ok": {"type": "boolean"}}}}}}}}}}}
        self.policy = {"raw_brief": "The IVR authenticated the caller and supplied customerNumber.",
                       "tool_policy": {"readAccount": "Read only for the authenticated caller."}}
        self.workflow = build_generic_workflow(self.policy, self.spec)

    def test_nested_required_request_field_is_checked(self):
        workflow = copy.deepcopy(self.workflow)
        body = workflow["nodes"]["service"]["tools"][0]["function"]["parameters"]["properties"]["body"]
        del body["properties"]["data"]["properties"]["customerNo"]
        self.assertTrue(any("required request field 'customerNo'" in error
                            for error in validate_generic_workflow(workflow, self.spec)))

    def test_nested_unknown_field_and_invalid_fixed_enum_are_checked(self):
        workflow = copy.deepcopy(self.workflow)
        fields = workflow["nodes"]["service"]["tools"][0]["function"]["parameters"]["properties"]["body"]["properties"]["data"]["properties"]
        fields["channel"]["default"] = "WEB"
        fields["invented"] = {"type": "string", "default": "x"}
        errors = validate_generic_workflow(workflow, self.spec)
        self.assertTrue(any("outside OpenAPI enum" in error for error in errors))
        self.assertTrue(any("invented" in error and "absent from OpenAPI" in error for error in errors))


if __name__ == "__main__":
    unittest.main()

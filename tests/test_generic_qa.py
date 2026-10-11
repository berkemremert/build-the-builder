"""Contract checks for nested generic API request bindings."""

from __future__ import annotations

import copy
import json
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

    def test_flat_tool_arguments_are_wrapped_in_documented_data_envelope(self):
        read = self.workflow["nodes"]["service"]["tools"][0]
        config = {"body": {"customerNo": "42"}}
        exec(read["pre_process_code"], {"config": config, "vars": {"customerNumber": "42"}})
        self.assertEqual({"data": {"customerNo": "42", "channel": "IVR"}}, config["body"])
        invalid = {"body": {"customerNo": "42", "invented": "x"}}
        with self.assertRaisesRegex(ValueError, "absent from the bank contract"):
            exec(read["pre_process_code"], {"config": invalid, "vars": {"customerNumber": "42"}})
        self.assertEqual({}, invalid["body"])

    def test_operation_scoped_model_arguments_fill_missing_config_body(self):
        read = self.workflow["nodes"]["service"]["tools"][0]
        config = {}
        state = {"customerNumber": "42", "readAccount_args.body.customerNo": "42"}
        exec(read["pre_process_code"], {"config": config, "vars": state})
        self.assertEqual({"data": {"customerNo": "42", "channel": "IVR"}}, config["body"])
        missing = {}
        with self.assertRaisesRegex(ValueError, "body is unavailable"):
            exec(read["pre_process_code"], {"config": missing, "vars": {"customerNumber": "42"}})

    def test_explicit_post_literal_is_bound_and_enforced(self):
        self.spec["paths"]["/read"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]["data"]["properties"]["channel"]["enum"] = ["IVR", "WEB"]
        self.policy["tool_policy"]["readAccount"] = 'POST with data.channel = "IVR" for this flow.'
        read = build_generic_workflow(self.policy, self.spec)["nodes"]["service"]["tools"][0]
        field = read["function"]["parameters"]["properties"]["body"]["properties"]["data"]["properties"]["channel"]
        self.assertEqual("IVR", field["default"])
        config = {"body": {"customerNo": "42"}}
        exec(read["pre_process_code"], {"config": config, "vars": {"customerNumber": "42"}})
        self.assertEqual("IVR", config["body"]["data"]["channel"])
        wrong = {"body": {"customerNo": "42", "channel": "WEB"}}
        with self.assertRaisesRegex(ValueError, "Fixed bank request value"):
            exec(read["pre_process_code"], {"config": wrong, "vars": {"customerNumber": "42"}})

    def test_preview_description_success_code_controls_read_evidence(self):
        operation = self.spec["paths"]["/read"]["post"]
        response = operation["responses"]["200"]
        response["description"] = "responseCode 0 means the preview is fine."
        response["content"]["application/json"]["schema"]["properties"]["data"] = {
            "type": "object", "properties": {"responseCode": {"type": "integer"}}}
        workflow = build_generic_workflow(self.policy, self.spec)
        read = workflow["nodes"]["service"]["tools"][0]
        for code, expected in ((7, False), (0, True)):
            state = {"generic_pending_readAccount": "{}"}
            result = {"status_code": 200, "data": {"data": {"responseCode": code}, "ok": True}}
            exec(read["post_process_code"], {"result": result, "vars": state})
            self.assertEqual(expected, "readAccount" in json.loads(state["generic_read_evidence"]))


class GenericWriteSafetyTests(unittest.TestCase):
    def test_successful_send_marker_exposes_guarded_write(self):
        read_schema = {"type": "object", "properties": {
            "data": {"type": "object", "properties": {
                "bucketX": {"type": "object"}, "bucketY": {"type": "object"}}},
            "ok": {"type": "boolean", "description": "True on success."}}}
        send_request = {"type": "object", "properties": {"data": {"type": "object",
            "properties": {"currencyType": {"type": "string", "enum": ["X", "Y"]}}}}}
        send_response = {"type": "object", "properties": {"data": {"type": "object",
            "properties": {"exitState": {"type": "string", "description": "DONE on a successful send."}}},
            "result": {"type": "object", "description": 'Success is HTTP 200 with returnCode "1".',
                       "properties": {"returnCode": {"type": "string"}}}}}
        def response(schema):
            return {"200": {"description": "Success", "content": {"application/json": {"schema": schema}}}}
        spec = {"openapi": "3.0.3", "servers": [{"url": "https://example.test"}], "paths": {
            "/read": {"post": {"operationId": "readPortfolio", "summary": "Read portfolio",
                                "responses": response(read_schema)}},
            "/send": {"post": {"operationId": "sendReport", "summary": "Send report",
                                "requestBody": {"content": {"application/json": {"schema": send_request}}},
                                "responses": response(send_response)}}}}
        policy = {"raw_brief": "The upstream IVR already authenticated the caller.",
                  "tool_policy": {"readPortfolio": "Read portfolio before sending.",
                                  "sendReport": "Use readPortfolio. Do not guess currencyType for a multi-currency send."}}
        workflow = build_generic_workflow(policy, spec)
        self.assertEqual([], validate_generic_workflow(workflow, spec))
        send = workflow["nodes"]["commit"]["tools"][0]
        self.assertEqual("sendReport", send["function"]["name"])

        for response, accepted in (({"data": {"bucketX": {}, "bucketY": None}}, True),
                                   ({"data": {"bucketX": {}, "bucketY": {}}}, False)):
            state = {"generic_read_evidence": json.dumps({"readPortfolio": {
                "request": {}, "response": response}}),
                "explicit_action_confirmation": "yes", "confirmed_write_action": "sendReport"}
            config = {"url": "https://example.test/send", "body": {"data": {"currencyType": "X"}}}
            if accepted:
                exec(send["pre_process_code"], {"config": config, "vars": state})
                self.assertEqual("https://example.test/send", config["url"])
            else:
                with self.assertRaisesRegex(ValueError, "uniquely established"):
                    exec(send["pre_process_code"], {"config": config, "vars": state})
                self.assertEqual("", config["url"])
        state = {}
        exec(send["post_process_code"], {"result": {"status_code": 200,
             "data": {"data": {"exitState": "DONE"}, "result": {"returnCode": "1"}}}, "vars": state})
        self.assertEqual("confirmed", state["generic_write_result"])
        exec(send["post_process_code"], {"result": {"status_code": 200,
             "data": {"data": {"exitState": "DONE"}, "result": {"returnCode": "0"}}}, "vars": state})
        self.assertEqual("unknown", state["generic_write_result"])


if __name__ == "__main__":
    unittest.main()

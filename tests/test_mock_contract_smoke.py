"""Local, synthetic contract checks for the deployed-mock smoke validator."""

from __future__ import annotations

import copy
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import mock_contract_smoke as smoke  # noqa: E402


CONTRACT = {
    "openapi": "3.0.3",
    "servers": [{"url": "https://example.invalid/v1"}],
    "paths": {
        "/customers/{customer_id}": {
            "get": {
                "operationId": "getCustomer",
                "responses": {"200": {"description": "found", "content": {
                    "application/json": {"schema": {"type": "object", "required": ["id", "balance"],
                                                     "properties": {"id": {"type": "string"},
                                                                    "balance": {"type": "integer"}}}}
                }}},
            }
        },
        "/promises": {
            "post": {
                "operationId": "createPromise",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "required": ["customer_id", "amount"],
                    "properties": {"customer_id": {"type": "string"}, "amount": {"type": "integer"}}
                }}}},
                "responses": {"201": {"description": "created", "content": {
                    "application/json": {"schema": {"type": "object", "required": ["promise_id", "status"],
                                                     "properties": {"promise_id": {"type": "string"},
                                                                    "status": {"type": "string", "enum": ["created"]}}}}
                }}},
            }
        },
        "/promises/{promise_id}": {
            "get": {
                "operationId": "getPromise",
                "responses": {"200": {"description": "found", "content": {
                    "application/json": {"schema": {"type": "object", "required": ["promise_id", "status"],
                                                     "properties": {"promise_id": {"type": "string"},
                                                                    "status": {"type": "string", "enum": ["created"]}}}}
                }}},
            }
        },
    },
}


CASES = {
    "variables": {"customer_id": "SYNTHETIC-CUSTOMER-01", "token": "SYNTHETIC-SECRET-01"},
    "sensitive_values": ["{{token}}"],
    "scenarios": [{"steps": [
        {"operationId": "getCustomer", "path_params": {"customer_id": "{{customer_id}}"},
         "expect": {"body.id": "{{customer_id}}", "body.balance": 42}},
        {"operationId": "createPromise", "request": {"customer_id": "{{customer_id}}", "amount": 12},
         "expect_status": 201, "expect": {"body.status": "created"}},
        {"operationId": "getPromise", "path_params": {"promise_id": "{{steps.1.body.promise_id}}"},
         "expect": {"body.promise_id": "{{steps.1.body.promise_id}}"}},
    ]}],
}


class MockContractSmokeTests(unittest.TestCase):
    def setUp(self):
        self.base_url = "http://127.0.0.1:8800/v1"
        self.mode = "valid"
        self.calls = []
        self.patcher = patch.object(smoke, "_send", side_effect=self._send)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def _send(self, url, method, body, headers, timeout, max_bytes):
        path = urlsplit(url).path
        self.calls.append((method, path, body))
        if (method, path) == ("get", "/v1/customers/SYNTHETIC-CUSTOMER-01"):
            response = {"id": "SYNTHETIC-CUSTOMER-01", "balance": 42}
            if self.mode == "schema":
                response.pop("balance")
            if self.mode == "secret":
                response["debug"] = "SYNTHETIC-SECRET-01"
            status = 200
        elif (method, path) == ("post", "/v1/promises") and body == {
                "customer_id": "SYNTHETIC-CUSTOMER-01", "amount": 12}:
            status, response = 201, {"promise_id": "P-01", "status": "created"}
        elif (method, path) == ("get", "/v1/promises/P-01"):
            status, response = 200, {"promise_id": "P-01", "status": "created"}
        else:
            status, response = 404, {"error": "not_found"}
        return status, response, json.dumps(response)

    def test_seeded_flow_covers_every_method_and_schema(self):
        self.assertEqual(smoke.run(CONTRACT, CASES, self.base_url), [
            "scenario 1 step 1: PASS", "scenario 1 step 2: PASS", "scenario 1 step 3: PASS"])
        self.assertEqual([(method, path) for method, path, _ in self.calls], [
            ("get", "/v1/customers/SYNTHETIC-CUSTOMER-01"),
            ("post", "/v1/promises"), ("get", "/v1/promises/P-01")])

    def test_contract_server_prefix_is_used_when_base_url_has_no_path(self):
        smoke.run(CONTRACT, CASES, "http://127.0.0.1:8800")
        self.assertEqual(self.calls[0][1], "/v1/customers/SYNTHETIC-CUSTOMER-01")

    def test_remote_http_url_is_rejected(self):
        result = smoke.run(CONTRACT, CASES, "http://example.invalid/v1")
        self.assertEqual(result, ["scenario 1 step 1: FAIL remote deployed URL must use HTTPS"])
        self.assertEqual(self.calls, [])

    def test_missing_operation_is_rejected_before_requests(self):
        cases = copy.deepcopy(CASES)
        cases["scenarios"][0]["steps"].pop()
        with self.assertRaisesRegex(smoke.SmokeError, "every contract operation"):
            smoke.run(CONTRACT, cases, self.base_url)
        self.assertEqual(self.calls, [])

    def test_response_schema_failure_has_no_values(self):
        self.mode = "schema"
        result = smoke.run(CONTRACT, CASES, self.base_url)
        self.assertEqual(result, ["scenario 1 step 1: FAIL response schema mismatch"])
        self.assertNotIn("SYNTHETIC", str(result))

    def test_secret_echo_is_rejected_without_printing_secret(self):
        self.mode = "secret"
        result = smoke.run(CONTRACT, CASES, self.base_url)
        self.assertEqual(result, ["scenario 1 step 1: FAIL response echoed a protected value"])
        self.assertNotIn("SYNTHETIC", str(result))

    def test_request_schema_failure_does_not_send_request(self):
        cases = copy.deepcopy(CASES)
        cases["scenarios"][0]["steps"][1]["request"].pop("amount")
        self.assertEqual(smoke.run(CONTRACT, cases, self.base_url)[-1],
                         "scenario 1 step 2: FAIL request schema mismatch")

    def test_cli_output_excludes_case_values_and_url(self):
        with TemporaryDirectory() as directory:
            contract = Path(directory) / "contract.json"
            cases = Path(directory) / "cases.json"
            contract.write_text(json.dumps(CONTRACT))
            cases.write_text(json.dumps(CASES))
            output = io.StringIO()
            with redirect_stdout(output):
                status = smoke.main(["--contract", str(contract), "--cases", str(cases),
                                     "--base-url", self.base_url])
        self.assertEqual(status, 0)
        self.assertIn("PASS", output.getvalue())
        self.assertNotIn("SYNTHETIC", output.getvalue())
        self.assertNotIn(self.base_url, output.getvalue())


if __name__ == "__main__":
    unittest.main()

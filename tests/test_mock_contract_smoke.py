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
from unittest.mock import MagicMock, patch
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
        self.contract = copy.deepcopy(CONTRACT)
        self.contract["servers"][0]["url"] = self.base_url
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
            if self.mode == "date":
                response["as_of"] = "2026-99-99"
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
        self.assertEqual(smoke.run(self.contract, CASES, self.base_url), [
            "scenario 1 step 1: PASS", "scenario 1 step 2: PASS", "scenario 1 step 3: PASS"])
        self.assertEqual([(method, path) for method, path, _ in self.calls], [
            ("get", "/v1/customers/SYNTHETIC-CUSTOMER-01"),
            ("post", "/v1/promises"), ("get", "/v1/promises/P-01")])

    def test_contract_server_prefix_mismatch_is_rejected(self):
        with self.assertRaisesRegex(smoke.SmokeError, "contract server URL does not match"):
            smoke.run(self.contract, CASES, "http://127.0.0.1:8800")
        self.assertEqual(self.calls, [])

    def test_origin_url_uses_contract_paths_verbatim(self):
        self.contract["servers"][0]["url"] = "http://127.0.0.1:8800"
        self.contract["paths"] = {"/v1" + path: spec for path, spec in self.contract["paths"].items()}
        smoke.run(self.contract, CASES, "http://127.0.0.1:8800")
        self.assertEqual(self.calls[0][1], "/v1/customers/SYNTHETIC-CUSTOMER-01")

    def test_remote_http_url_is_rejected(self):
        with self.assertRaisesRegex(smoke.SmokeError, "remote deployed URL must use HTTPS"):
            smoke.run(self.contract, CASES, "http://example.invalid/v1")
        self.assertEqual(self.calls, [])

    def test_later_operation_server_mismatch_is_rejected_before_any_write(self):
        operation = self.contract["paths"]["/promises"]["post"]
        operation["servers"] = [{"url": self.base_url + "/wrong"}]
        with self.assertRaisesRegex(smoke.SmokeError, "contract server URL does not match"):
            smoke.run(self.contract, CASES, self.base_url)
        self.assertEqual(self.calls, [])

    def test_missing_operation_is_rejected_before_requests(self):
        cases = copy.deepcopy(CASES)
        cases["scenarios"][0]["steps"].pop()
        with self.assertRaisesRegex(smoke.SmokeError, "every contract operation"):
            smoke.run(self.contract, cases, self.base_url)
        self.assertEqual(self.calls, [])

    def test_mixed_success_and_error_status_does_not_count_as_coverage(self):
        cases = copy.deepcopy(CASES)
        cases["scenarios"][0]["steps"][1]["expect_status"] = [201, 500]
        with self.assertRaisesRegex(smoke.SmokeError, "every contract operation"):
            smoke.run(self.contract, cases, self.base_url)
        self.assertEqual(self.calls, [])

    def test_response_schema_failure_has_no_values(self):
        self.mode = "schema"
        result = smoke.run(self.contract, CASES, self.base_url)
        self.assertEqual(result, ["scenario 1 step 1: FAIL response schema mismatch"])
        self.assertNotIn("SYNTHETIC", str(result))

    def test_date_format_is_checked(self):
        operation = self.contract["paths"]["/customers/{customer_id}"]["get"]
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        schema["required"].append("as_of")
        schema["properties"]["as_of"] = {"type": "string", "format": "date"}
        self.mode = "date"
        self.assertEqual(smoke.run(self.contract, CASES, self.base_url),
                         ["scenario 1 step 1: FAIL response schema mismatch"])

    def test_response_status_range_is_supported(self):
        responses = self.contract["paths"]["/promises"]["post"]["responses"]
        responses["2XX"] = responses.pop("201")
        self.assertEqual(smoke.run(self.contract, CASES, self.base_url)[-1],
                         "scenario 1 step 3: PASS")

    def test_secret_echo_is_rejected_without_printing_secret(self):
        self.mode = "secret"
        result = smoke.run(self.contract, CASES, self.base_url)
        self.assertEqual(result, ["scenario 1 step 1: FAIL response echoed a protected value"])
        self.assertNotIn("SYNTHETIC", str(result))

    def test_request_schema_failure_does_not_send_request(self):
        cases = copy.deepcopy(CASES)
        cases["scenarios"][0]["steps"][1]["request"].pop("amount")
        self.assertEqual(smoke.run(self.contract, cases, self.base_url)[-1],
                         "scenario 1 step 2: FAIL request schema mismatch")

    def test_cli_output_excludes_case_values_and_url(self):
        with TemporaryDirectory() as directory:
            contract = Path(directory) / "contract.json"
            cases = Path(directory) / "cases.json"
            contract.write_text(json.dumps(self.contract))
            cases.write_text(json.dumps(CASES))
            output = io.StringIO()
            with redirect_stdout(output):
                status = smoke.main(["--contract", str(contract), "--cases", str(cases),
                                     "--base-url", self.base_url])
        self.assertEqual(status, 0)
        self.assertIn("PASS", output.getvalue())
        self.assertNotIn("SYNTHETIC", output.getvalue())
        self.assertNotIn(self.base_url, output.getvalue())


class TransportTests(unittest.TestCase):
    def test_requests_use_browser_user_agent(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"status":"alive"}'
        response.status = 200
        response.headers = {"Content-Type": "application/json"}
        with patch.object(smoke, "build_opener") as build_opener:
            build_opener.return_value.open.return_value = response
            status, body, _ = smoke._send(
                "https://mock.example/healthz", "get", None, {}, 5, 1024)
            request = build_opener.return_value.open.call_args.args[0]
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "alive"})
        self.assertEqual(request.get_header("User-agent"), smoke.USER_AGENT)


if __name__ == "__main__":
    unittest.main()

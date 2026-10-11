"""Regression tests for source-described bank success and read provenance."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generic_workflow_fallback import _Operation, _result_checks, build_generic_workflow


def _response(extra: dict | None = None, description: str = '') -> dict:
    return {"description": description or 'HTTP 200 with result.returnCode equal to "1" is success.',
            "content": {"application/json": {"schema": {"type": "object", "properties": {
                "result": {"type": "object", "properties": {"returnCode": {"type": "string"}}},
                "data": {"type": "object", "properties": extra or {}},
            }}}}}


def _operation(name: str, response: dict, request: dict | None = None) -> _Operation:
    source = {"operationId": name, "responses": {"200": response}}
    if request is not None:
        source["requestBody"] = {"content": {"application/json": {"schema": request}}}
    return _Operation(name, "/" + name, "POST", source,
                      "write" if name.endswith(("change", "send")) else "read")


class FinalStyleSuccessContractTests(unittest.TestCase):
    def test_all_seven_services_require_conjunctive_source_success_markers(self):
        response_code = {"type": "integer", "description": "0 on success."}
        operations = {
            "cards.list": (_response(), {("result", "returnCode"): "1"}),
            "cards.balance": (_response(), {("result", "returnCode"): "1"}),
            "statement.week.get": (_response(), {("result", "returnCode"): "1"}),
            "statement.week.decide": (
                _response({"responseCode": response_code}),
                {("result", "returnCode"): "1", ("data", "responseCode"): 0}),
            "statement.week.change": (
                _response({"responseCode": response_code,
                           "exitState": {"type": "string", "description": "OK means success."}}),
                {("result", "returnCode"): "1", ("data", "responseCode"): 0,
                 ("data", "exitState"): "OK"}),
            "contact.email.get": (_response(), {("result", "returnCode"): "1"}),
            "statement.pdf.send": (
                _response({"exitState": {"type": "string", "description": "DONE on a successful send."}}),
                {("result", "returnCode"): "1", ("data", "exitState"): "DONE"}),
        }
        for name, (response, expected) in operations.items():
            with self.subTest(name=name):
                checks = dict(_result_checks(_operation(name, response)))
                self.assertEqual(expected, checks)

    def test_response_description_alone_binds_return_code_and_preview_code(self):
        response = _response(
            {"responseCode": {"type": "integer"}},
            'HTTP 200 with result.returnCode equal to "1" is success; '
            'data.responseCode 0 means the preview is fine.')
        checks = dict(_result_checks(_operation("statement.week.decide", response)))
        self.assertEqual({("result", "returnCode"): "1", ("data", "responseCode"): 0}, checks)

    def test_failed_repeated_list_clears_evidence_and_success_restores_it(self):
        request = {"type": "object", "properties": {"data": {"type": "object", "properties": {
            "customerNo": {"type": "string"}}}}}
        response = _response({"cards": {"type": "array", "items": {"type": "object",
            "properties": {"cardNumber": {"type": "string"}}}}})
        spec = {"openapi": "3.0.3", "servers": [{"url": "https://example.test"}],
                "paths": {"/cards.list": {"post": _operation("cards.list", response, request).source}}}
        policy = {"raw_brief": "The IVR authenticated the caller. Each call gives variables:\n"
                               "- `customerNumber`: caller number.\n",
                  "tool_policy": {"cards.list": "Read cards for supplied customerNumber; "
                                                 'HTTP 200 and result.returnCode "1" means success.'}}
        workflow = build_generic_workflow(policy, spec)
        tool = workflow["nodes"]["service"]["tools"][0]
        state = {"customerNumber": "42", "generic_read_evidence": json.dumps({
            "cards.list": {"request": {"data": {"customerNo": "42"}},
                           "response": {"data": {"cards": [{"cardNumber": "old"}]}}}})}

        def run(status: int, code: str, card: str):
            config = {"url": "https://example.test/cards.list",
                      "body": {"data": {"customerNo": "42"}}}
            exec(tool["pre_process_code"], {"config": config, "vars": state})
            self.assertNotIn("cards.list", json.loads(state["generic_read_evidence"]))
            result = {"status_code": status, "data": {"result": {"returnCode": code},
                        "data": {"cards": [{"cardNumber": card}]}}}
            exec(tool["post_process_code"], {"result": result, "vars": state})
            return json.loads(state["generic_read_evidence"])

        self.assertNotIn("cards.list", run(404, "0", "old"))
        self.assertNotIn("cards.list", run(200, "0", "old"))
        evidence = run(200, "1", "new")
        self.assertEqual("new", evidence["cards.list"]["response"]["data"]["cards"][0]["cardNumber"])
        self.assertEqual("42", evidence["cards.list"]["request"]["data"]["customerNo"])


if __name__ == "__main__":
    unittest.main()

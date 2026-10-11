"""Synthetic changed-contract tests for the emergency workflow fallback."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generic_workflow_fallback import build_generic_workflow, describe_contract  # noqa: E402


def _operation(name: str, summary: str, *, request: dict | None = None,
               result: str = "ok") -> dict:
    operation = {"operationId": name, "summary": summary,
                 "responses": {"200": {"description": "Completed",
                                        "content": {"application/json": {"schema": {
                                            "type": "object", "properties": {
                                                result: {"type": "boolean"}}}}}}}}
    if request is not None:
        operation["requestBody"] = {"content": {"application/json": {"schema": request}}}
    return operation


def _spec() -> dict:
    return {"openapi": "3.0.3", "servers": [{"url": "https://bank.example.test"}],
            "paths": {
                "/check-person": {"post": _operation(
                    "checkPerson", "Verify identity", request={"type": "object", "properties": {
                        "customer_id": {"type": "string"}, "answer": {"type": "string"}},
                        "required": ["customer_id", "answer"]}, result="verified")},
                "/portfolio": {"get": _operation("fetchPortfolio", "Get account details")},
                "/arrangements": {"post": _operation(
                    "saveArrangement", "Record confirmed arrangement", request={"type": "object",
                    "properties": {"customer_id": {"type": "string"}, "amount": {"type": "number"}},
                    "required": ["customer_id", "amount"]}, result="recorded")},
                "/interaction-end": {"post": _operation(
                    "finishInteraction", "Close call", request={"type": "object", "properties": {
                        "outcome": {"type": "string", "enum": ["promise", "no_promise", "not_verified", "not_reached"]},
                        "reason": {"type": "string", "description": "Required for every outcome except promise."}},
                        "required": ["outcome"]}, result="closed")},
            }}


def _policy() -> dict:
    return {"raw_brief": "Synthetic source: verify the customer before discussing the account. "
                         "Record an arrangement only after exact customer confirmation.\n"
                         "| Outcome | Reason | When |\n| --- | --- | --- |\n"
                         "| `no_promise` | `refused`, `service_error` | no confirmed arrangement |\n"
                         "| `not_verified` | `failed`, `declined` | security did not pass |\n"
                         "| `not_reached` | `wrong_number`, `unclear` | customer not reached |",
            "tool_policy": {"checkPerson": "Check the customer's own answer.",
                            "fetchPortfolio": "Read only after identity passes.",
                            "saveArrangement": "Record only after exact confirmation.",
                            "finishInteraction": "Record the truthful call result."}}


class GenericWorkflowFallbackTests(unittest.TestCase):
    def test_describes_exact_operation_ids_and_urls(self):
        operations = describe_contract(_spec(), _policy())
        self.assertEqual(
            [("checkPerson", "https://bank.example.test/check-person"),
             ("fetchPortfolio", "https://bank.example.test/portfolio"),
             ("saveArrangement", "https://bank.example.test/arrangements"),
             ("finishInteraction", "https://bank.example.test/interaction-end")],
            [(item["operation_id"], item["url"]) for item in operations])

    def test_renamed_four_operation_contract_has_gated_read_and_write(self):
        workflow = build_generic_workflow(_policy(), _spec())
        nodes = workflow["nodes"]
        self.assertEqual("opening", workflow["initial_node"])
        self.assertIn(_policy()["raw_brief"], workflow["system_prompt"])
        self.assertEqual(["checkPerson"], [tool["function"]["name"] for tool in nodes["verification"]["tools"]])
        self.assertEqual(["fetchPortfolio"], [tool["function"]["name"] for tool in nodes["service"]["tools"]])
        self.assertEqual(["saveArrangement"], [tool["function"]["name"] for tool in nodes["commit"]["tools"]])
        self.assertEqual(["finishInteraction"], [tool["function"]["name"] for tool in nodes["closing"]["pre_actions"]])
        self.assertEqual("api_request", nodes["closing"]["node_type"])
        self.assertEqual("ended", nodes["closing"]["passthrough_default_transition"])
        verification_edges = nodes["verification"]["transitions"]
        self.assertTrue(all("checkPerson_response.data.verified" in str(edge) for edge in verification_edges))
        self.assertIn("explicit_action_confirmation", str(nodes["service"]["transitions"]))
        self.assertEqual("https://bank.example.test/arrangements",
                         nodes["commit"]["tools"][0]["function"]["parameters"]["properties"]["url"]["default"])

    def test_full_identity_result_takes_precedence_over_question_result(self):
        spec = _spec()
        identity_schema = spec["paths"]["/check-person"]["post"]["responses"]["200"]["content"]
        identity_schema["application/json"]["schema"]["properties"]["all_verified"] = {"type": "boolean"}
        workflow = build_generic_workflow(_policy(), spec)
        self.assertIn("service", workflow["nodes"])
        self.assertTrue(all("checkPerson_response.data.all_verified" in str(edge)
                            for edge in workflow["nodes"]["verification"]["transitions"]))

    def test_ambiguous_identity_result_fails_closed_with_diagnostic(self):
        spec = _spec()
        identity_schema = spec["paths"]["/check-person"]["post"]["responses"]["200"]["content"]
        identity_schema["application/json"]["schema"]["properties"]["identity_passed"] = {"type": "boolean"}
        with self.assertRaisesRegex(ValueError, "full-verification boolean"):
            build_generic_workflow(_policy(), spec)

    def test_missing_identity_operation_fails_closed_with_diagnostic(self):
        spec = _spec()
        del spec["paths"]["/check-person"]
        with self.assertRaisesRegex(ValueError, "callable identity operation"):
            build_generic_workflow(_policy(), spec)

    def test_close_request_uses_confirmed_state_and_rejects_second_attempt(self):
        workflow = build_generic_workflow(_policy(), _spec())
        source = workflow["nodes"]["closing"]["pre_actions"][0]["pre_process_code"]
        body = {}
        state = {"caller_role": "customer", "generic_identity_verified": "true",
                 "generic_write_result": "confirmed", "generic_recorded_action": "saveArrangement",
                 "generic_close_reason": "refused"}
        exec(source, {"config": {"body": body}, "vars": state})
        self.assertEqual({"outcome": "promise"}, body)
        self.assertEqual("true", state["generic_close_attempted"])
        duplicate_config = {"url": "https://bank.example.test/interaction-end", "body": {}}
        with self.assertRaisesRegex(ValueError, "already attempted"):
            exec(source, {"config": duplicate_config, "vars": state})
        self.assertEqual("", duplicate_config["url"])

    def test_close_request_rejects_unknown_reason(self):
        workflow = build_generic_workflow(_policy(), _spec())
        source = workflow["nodes"]["closing"]["pre_actions"][0]["pre_process_code"]
        state = {"caller_role": "customer", "generic_identity_verified": "true"}
        config = {"url": "https://bank.example.test/interaction-end", "body": {}}
        with self.assertRaisesRegex(ValueError, "source-approved close reason"):
            exec(source, {"config": config, "vars": state})
        self.assertEqual("", config["url"])

    def test_unsupported_required_query_operation_is_omitted(self):
        spec = copy.deepcopy(_spec())
        spec["paths"]["/portfolio"]["get"]["parameters"] = [
            {"name": "customer", "in": "query", "required": True,
             "schema": {"type": "string"}}]
        workflow = build_generic_workflow(_policy(), spec)
        self.assertNotIn("tools", workflow["nodes"]["service"])
        self.assertIn("commit", workflow["nodes"])


if __name__ == "__main__":
    unittest.main()

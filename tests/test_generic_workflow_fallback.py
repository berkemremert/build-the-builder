"""Synthetic changed-contract tests for the emergency workflow fallback."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generic_workflow_fallback import build_generic_workflow, describe_contract  # noqa: E402
from qa import validate_generic_workflow  # noqa: E402


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
    def test_source_derived_write_provenance_rejects_mismatched_inputs(self):
        def operation(name, request, response):
            return {"operationId": name, "summary": name,
                    "requestBody": {"content": {"application/json": {"schema": {
                        "type": "object", "properties": {"data": {"type": "object",
                        "properties": request}}}}}},
                    "responses": {"200": {"content": {"application/json": {"schema": {
                        "type": "object", "properties": {"ok": {"type": "boolean"},
                        "data": {"type": "object", "properties": response}}}}}}}}

        text = lambda: {"type": "string"}
        specs = {
            "listItems": ({"customerNo": text()}, {"items": {"type": "array", "items": {
                "type": "object", "properties": {"itemKey": text()}}}}),
            "getCycle": ({"itemKey": text()}, {"itemKey": text(), "day": {"type": "integer"},
                                               "week": {"type": "integer"}}),
            "previewCycle": ({"itemKey": text(), "newWeek": {"type": "integer"},
                              "day": {"type": "integer"}},
                             {"cutDate": text(), "dueDate": text()}),
            "changeCycle": ({"itemKey": text(), "newWeek": {"type": "integer"},
                             "day": {"type": "integer"}}, {}),
            "readBalance": ({"itemKey": text()}, {"issuedDate": text(),
                            "amountUSD": {"type": "object", "properties": {"amount": {"type": "number"}}},
                            "amountEUR": {"type": "object", "properties": {"amount": {"type": "number"}}}}),
            "getContact": ({"customerNo": text()}, {"registeredMail": text()}),
            "sendCopy": ({"customerNo": text(), "itemKey": text(), "issuedDate": text(),
                          "destination": text(), "currencyCode": {"type": "string",
                                                             "enum": ["USD", "EUR"]}}, {}),
        }
        spec = {"openapi": "3.0.3", "servers": [{"url": "https://example.test"}],
                "paths": {"/" + name: {"post": operation(name, *schemas)}
                          for name, schemas in specs.items()}}
        policy = {"raw_brief": "The IVR verified identity. Each call gives variables:\n"
                               "- `customerNumber`: caller identity.\n",
                  "tool_policy": {
                      "listItems": "List eligible items.",
                      "getCycle": "Read cycle for selected listed itemKey.",
                      "previewCycle": "Only after a different newWeek is chosen. POST data.day=the "
                                      "CURRENT day from getCycle. Use exact listed itemKey.",
                      "changeCycle": "Change only after successful preview; confirm previewed dates "
                                     "and use the same exact listed itemKey, newWeek and day.",
                      "readBalance": "Read for exact listed itemKey.",
                      "getContact": "Read registered contact for supplied customerNo.",
                      "sendCopy": "Send only after confirmation. POST data.customerNo=the supplied "
                                  "customerNumber, data.itemKey=the exact listItems value, "
                                  "data.issuedDate=the issued readBalance issuedDate, "
                                  "data.destination=the same registeredMail, and data.currencyCode "
                                  "only when its choice is established; the multi-currency choice is unresolved."}}
        workflow = build_generic_workflow(policy, spec)
        tools = {tool["function"]["name"]: tool for node in workflow["nodes"].values()
                 for tool in node.get("tools", [])}

        def invoke(name, body, state, data=None):
            tool = tools[name]
            config = {"url": "https://example.test/" + name, "body": {"data": body}}
            exec(tool["pre_process_code"], {"vars": state, "config": config})
            if data is not None:
                result = {"status_code": 200, "data": {"ok": True, "data": data}}
                exec(tool["post_process_code"], {"vars": state, "result": result})
            return config

        state = {"customerNumber": "77"}
        with self.assertRaisesRegex(ValueError, "bank read"):
            invoke("changeCycle", {"itemKey": "A", "newWeek": 2, "day": 5}, state)
        invoke("listItems", {"customerNo": "77"}, state, {"items": [{"itemKey": "A"}]})
        invoke("getCycle", {"itemKey": "A"}, state, {"itemKey": "A", "day": 5, "week": 1})
        with self.assertRaisesRegex(ValueError, "bank result"):
            invoke("previewCycle", {"itemKey": "A", "newWeek": 2, "day": 6}, state)
        with self.assertRaisesRegex(ValueError, "current bank value"):
            invoke("previewCycle", {"itemKey": "A", "newWeek": 1, "day": 5}, state)
        invoke("previewCycle", {"itemKey": "A", "newWeek": 2, "day": 5}, state,
               {"cutDate": "2026-11-01", "dueDate": "2026-11-11"})
        state.update(explicit_action_confirmation="yes", confirmed_write_action="changeCycle")
        with self.assertRaisesRegex(ValueError, "preview request"):
            invoke("changeCycle", {"itemKey": "A", "newWeek": 3, "day": 5}, state)
        with self.assertRaisesRegex(ValueError, "bank list"):
            invoke("changeCycle", {"itemKey": "B", "newWeek": 2, "day": 5}, state)
        invoke("changeCycle", {"itemKey": "A", "newWeek": 2, "day": 5}, state)
        with self.assertRaisesRegex(ValueError, "already attempted"):
            invoke("changeCycle", {"itemKey": "A", "newWeek": 2, "day": 5}, state)

        state = {"customerNumber": "77"}
        invoke("listItems", {"customerNo": "77"}, state, {"items": [{"itemKey": "A"}]})
        invoke("readBalance", {"itemKey": "A"}, state,
               {"issuedDate": "2026-10-01", "amountUSD": {"amount": 10}})
        invoke("getContact", {"customerNo": "77"}, state,
               {"registeredMail": "registered@example.test"})
        state.update(explicit_action_confirmation="yes", confirmed_write_action="sendCopy")
        send = {"customerNo": "77", "itemKey": "A", "issuedDate": "2026-10-01",
                "destination": "registered@example.test", "currencyCode": "USD"}
        with self.assertRaisesRegex(ValueError, "bank result"):
            invoke("sendCopy", {**send, "destination": "other@example.test"}, state)
        with self.assertRaisesRegex(ValueError, "bank result"):
            invoke("sendCopy", {**send, "issuedDate": "2026-09-01"}, state)
        with self.assertRaisesRegex(ValueError, "uniquely established"):
            invoke("sendCopy", {**send, "currencyCode": "EUR"}, state)
        invoke("sendCopy", send, state)

    def test_nested_request_and_string_code_success_are_source_bound(self):
        result_schema = {"type": "object", "description": 'HTTP 200 with returnCode "S" indicates success.',
                         "properties": {"returnCode": {"type": "string"}}}
        spec = {"openapi": "3.0.3", "servers": [{"url": "https://example.test"}], "paths": {}}
        for path, name, summary, fields, extra in (
            ("/account.lookup", "lookupAccount", "Read account balance", {"customerNo": {"type": "string"}}, {}),
            ("/letter.dispatch", "dispatchLetter", "Send requested letter", {
                "customerNo": {"type": "string"}, "channel": {"type": "string", "enum": ["mail"]}},
             {"responseCode": {"type": "integer", "description": "0 on success."},
              "exitState": {"type": "string", "enum": ["FINISHED"]}}),
        ):
            spec["paths"][path] = {"post": {"operationId": name, "summary": summary,
                "requestBody": {"content": {"application/json": {"schema": {"type": "object",
                    "properties": {"data": {"type": "object", "properties": fields,
                                            "required": list(fields)}}}}}},
                "responses": {"200": {"description": "success", "content": {"application/json": {
                    "schema": {"type": "object", "properties": {
                        "result": result_schema, "data": {"type": "object", "properties": extra}}}}}}}}}
        policy = {"raw_brief": "Every call gives variables:\n- `customerNumber`: customer number.\n"
                               "The IVR has authenticated the caller before handoff. "
                               "Send only after explicit customer confirmation."}
        workflow = build_generic_workflow(policy, spec)
        self.assertEqual("service", workflow["initial_node"])
        self.assertEqual(["lookupAccount"], [tool["function"]["name"]
                                               for tool in workflow["nodes"]["service"]["tools"]])
        send = workflow["nodes"]["commit"]["tools"][0]
        self.assertGreater(send["config"]["timeoutMs"], 12000)
        data_fields = send["function"]["parameters"]["properties"]["body"]["properties"]
        self.assertEqual("{{customerNumber}}", data_fields["customerNo"]["default"])
        self.assertEqual("mail", data_fields["channel"]["default"])
        state = {}
        result = {"status_code": 200, "data": {"result": {"returnCode": "S"},
                                                "data": {"responseCode": 0, "exitState": "FINISHED"}}}
        exec(send["post_process_code"], {"result": result, "vars": state})
        self.assertEqual("confirmed", state["generic_write_result"])
        rejected = {"status_code": 200, "data": {"result": {"returnCode": "S"},
                                                  "data": {"responseCode": 7, "exitState": "FINISHED"}}}
        exec(send["post_process_code"], {"result": rejected, "vars": state})
        self.assertEqual("unknown", state["generic_write_result"])

    def test_auth_token_endpoint_is_not_exposed_to_caller(self):
        spec = _spec()
        spec["paths"]["/oauth2/token"] = {"post": _operation("token", "Issue access token")}
        workflow = build_generic_workflow(_policy(), spec)
        exposed = [tool["function"]["name"] for node in workflow["nodes"].values()
                   for tool in node.get("tools", []) + node.get("pre_actions", [])]
        self.assertNotIn("token", exposed)

    def test_write_requires_confirmation_of_its_own_action_and_single_attempt(self):
        workflow = build_generic_workflow(_policy(), _spec())
        write = workflow["nodes"]["commit"]["tools"][0]
        source = write["pre_process_code"]
        self.assertNotIn("confirmed_write_action", str(workflow["nodes"]["service"]["extraction_schema"]))
        self.assertIn("confirmed_write_action", str(workflow["nodes"]["proposal_saveArrangement"]["extraction_schema"]))

        for state in ({}, {"explicit_action_confirmation": "yes",
                           "confirmed_write_action": "anotherWrite"}):
            config = {"url": "https://bank.example.test/arrangements", "body": {"amount": 10}}
            with self.assertRaisesRegex(ValueError, "this exact action"):
                exec(source, {"config": config, "vars": state})
            self.assertEqual("", config["url"])
            self.assertEqual({}, config["body"])
            self.assertNotIn("generic_write_attempted_action", state)

        state = {"explicit_action_confirmation": "yes", "confirmed_write_action": "saveArrangement"}
        config = {"url": "https://bank.example.test/arrangements", "body": {"amount": 10}}
        exec(source, {"config": config, "vars": state})
        self.assertEqual("saveArrangement", state["generic_write_attempted_action"])
        with self.assertRaisesRegex(ValueError, "already attempted"):
            exec(source, {"config": config, "vars": state})
        self.assertEqual("", config["url"])

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
        self.assertIn("never expose internal reasoning, role labels, tool traces, or draft markers",
                      workflow["system_prompt"])
        self.assertEqual(["checkPerson"], [tool["function"]["name"] for tool in nodes["verification"]["tools"]])
        self.assertEqual(["fetchPortfolio"], [tool["function"]["name"] for tool in nodes["service"]["tools"]])
        self.assertEqual(["saveArrangement"], [tool["function"]["name"] for tool in nodes["commit"]["tools"]])
        self.assertEqual(["finishInteraction"], [tool["function"]["name"] for tool in nodes["closing"]["pre_actions"]])
        self.assertEqual("api_request", nodes["closing"]["node_type"])
        self.assertEqual("ended", nodes["closing"]["passthrough_default_transition"])
        verification_edges = nodes["verification"]["transitions"]
        self.assertTrue(all("checkPerson_response.data.verified" in str(edge) for edge in verification_edges))
        self.assertIn("requested_write_action", str(nodes["service"]["transitions"]))
        self.assertNotIn("commit", [edge["target_node"] for edge in nodes["service"]["transitions"]])
        self.assertIn("explicit_action_confirmation", str(nodes["proposal_saveArrangement"]["transitions"]))
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

    def test_source_confirmed_upstream_identity_uses_declared_call_inputs(self):
        spec = {"openapi": "3.0.3", "servers": [{"url": "https://bank.example.test"}],
                "paths": {"/summary": {"post": _operation(
                    "fetchSummary", "Read account summary", request={"type": "object",
                    "properties": {"subscriberNumber": {"type": "string"},
                                   "callTime": {"type": "string"}},
                    "required": ["subscriberNumber", "callTime"]})}}}
        policy = {"raw_brief": "The caller passed through the IVR. The IVR verified their identity.\n"
                               "Each call gives two variables:\n"
                               "- `subscriberNumber`: number supplied by the IVR.\n"
                               "- `callTime`: time supplied with this call.\n",
                  "tool_policy": {"fetchSummary": "Read only for the IVR-verified caller."}}
        workflow = build_generic_workflow(policy, spec)
        self.assertEqual("service", workflow["initial_node"])
        self.assertNotIn("opening", workflow["nodes"])
        body = workflow["nodes"]["service"]["tools"][0]["function"]["parameters"]["properties"]["body"]
        self.assertEqual("{{subscriberNumber}}", body["properties"]["subscriberNumber"]["default"])
        self.assertEqual("{{callTime}}", body["properties"]["callTime"]["default"])
        self.assertIn("callTime={{callTime}}", workflow["system_prompt"])
        self.assertEqual([], validate_generic_workflow(workflow, spec))

    def test_upstream_handoff_without_identity_confirmation_stays_gated(self):
        spec = copy.deepcopy(_spec())
        del spec["paths"]["/check-person"]
        policy = {"raw_brief": "The IVR detects intent and hands the call to the assistant."}
        with self.assertRaisesRegex(ValueError, "callable identity operation"):
            build_generic_workflow(policy, spec)
        policy["raw_brief"] = "The IVR has not verified the caller."
        with self.assertRaisesRegex(ValueError, "callable identity operation"):
            build_generic_workflow(policy, spec)

    def test_post_read_and_write_roles_follow_operation_meaning(self):
        spec = {"openapi": "3.0.3", "servers": [{"url": "https://bank.example.test"}],
                "paths": {
                    "/lookup": {"post": _operation("kartSorgula", "Query cards")},
                    "/send": {"post": _operation("ekstreGonder", "Send statement", result="saved")}}}
        policy = {"raw_brief": "The IVR verified the caller's identity.",
                  "tool_policy": {"kartSorgula": "Query account cards.",
                                  "ekstreGonder": "Send after confirmation."}}
        workflow = build_generic_workflow(policy, spec)
        self.assertEqual(["kartSorgula"], [tool["function"]["name"]
                                           for tool in workflow["nodes"]["service"]["tools"]])
        self.assertEqual(["ekstreGonder"], [tool["function"]["name"]
                                            for tool in workflow["nodes"]["commit"]["tools"]])


if __name__ == "__main__":
    unittest.main()

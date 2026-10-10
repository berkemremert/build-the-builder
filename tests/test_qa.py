"""Offline regression tests for workflow preflight and scorer invocation."""

import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import qa  # noqa: E402
from policy_compiler import compile_policy  # noqa: E402
from workflow_generator import build_workflow  # noqa: E402


OPENAPI = json.loads((ROOT / "tools" / "openapi.json").read_text())
BASE_URL = OPENAPI["servers"][0]["url"]


def bank_tool(name):
    path = f"/{name}"
    schema = OPENAPI["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
    fields = {}
    for field, api_spec in schema["properties"].items():
        spec = {"type": api_spec["type"]}
        if "enum" in api_spec:
            spec["enum"] = copy.deepcopy(api_spec["enum"])
        spec["default"] = "{{" + field + "}}"
        fields[field] = spec
    return {
        "type": "function",
        "function": {
            "name": name, "handler": "__function__:api_call",
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "default": BASE_URL + path},
                "method": {"type": "string", "default": "POST"},
                "body": {"type": "object", "properties": fields,
                         "required": copy.deepcopy(schema["required"])},
            }},
        },
    }


def transition(target, condition=None):
    return {"target_node": target, "description": "continue",
            "condition": condition or {"operator": "always"}}


def valid_workflow():
    close = bank_tool("close_call")
    close["config"] = {"alwaysRunsAt": ["end_of_call"]}
    return {
        "initial_node": "identity",
        "system_prompt": "Freya Bank digital assistant.",
        "nodes": {
            "identity": {
                "name": "identity", "node_type": "conversation",
                "task_message": "Verify both answers before giving account information.",
                "tools": [bank_tool("verify_identity")],
                "transitions": [
                    transition("account", {"logic": "or", "conditions": [
                        {"variable": "verify_identity_response.data.all_verified",
                         "operator": "equals", "operand": "true"},
                        {"variable": "verify_identity_response.data.all_verified",
                         "operator": "equals", "operand": "True"},
                    ]}),
                    transition("finish"),
                ],
            },
            "account": {
                "name": "account", "node_type": "conversation",
                "task_message": "Get the account.",
                "tools": [bank_tool("get_account")],
                "transitions": [transition("finish")],
            },
            "finish": {
                "name": "finish", "node_type": "end_call",
                "on_enter": {"type": "silent"}, "transitions": [],
                "terminal": {"type": "function", "function": {
                    "name": "closing_end", "handler": "__function__:end_call",
                    "parameters": {"type": "object", "properties": {
                        "message_mode": {"type": "string", "default": "none"},
                    }},
                }},
            },
        },
        "tools": [close],
    }


def explicit_close_workflow():
    workflow = valid_workflow()
    workflow["tools"] = []
    workflow["nodes"]["identity"]["transitions"][1]["target_node"] = "close"
    workflow["nodes"]["account"]["transitions"][0]["target_node"] = "close"
    workflow["nodes"]["close"] = {
        "name": "close", "node_type": "api_request", "on_enter": {"type": "silent"},
        "passthrough": True, "passthrough_default_transition": "finish",
        "pre_actions": [bank_tool("close_call")],
        "transitions": [transition("finish")],
    }
    return workflow


def confirmed_promise_workflow():
    workflow = explicit_close_workflow()
    workflow["nodes"]["account"]["task_message"] = "Read back the proposed promise and ask for confirmation."
    workflow["nodes"]["account"]["extraction_schema"] = [
        {"name": "promise_confirmation", "type": "string",
         "enum_values": ["confirmed", "pending", "changed"],
         "description": "Confirmed only after the full read-back is accepted."}]
    workflow["nodes"]["account"]["transitions"] = [
        transition("commit", {"logic": "or", "conditions": [
            {"variable": "promise_confirmation", "operator": "equals", "operand": "confirmed"}]}),
        transition("close", {"operator": "matches_intent", "description": "No promise"}),
    ]
    workflow["nodes"]["commit"] = {
        "name": "commit", "node_type": "conversation",
        "task_message": "Record the confirmed promise.",
        "tools": [bank_tool("create_promise")],
        "transitions": [transition("close")],
    }
    return workflow


class ValidatorTests(unittest.TestCase):
    def check_has(self, workflow, phrase):
        errors = qa.validate_workflow(workflow, OPENAPI)
        self.assertTrue(any(phrase in error for error in errors), errors)

    def test_valid_workflow_passes(self):
        self.assertEqual(qa.validate_workflow(valid_workflow(), OPENAPI), [])

    def test_explicit_close_before_end_call_passes(self):
        self.assertEqual(qa.validate_workflow(explicit_close_workflow(), OPENAPI), [])

    def test_bypass_of_explicit_close_is_reported(self):
        workflow = explicit_close_workflow()
        workflow["nodes"]["identity"]["transitions"][1]["target_node"] = "finish"
        self.check_has(workflow, "path reaches end_call without a preceding close_call")

    def test_promise_requires_positive_confirmation_on_every_path(self):
        workflow = confirmed_promise_workflow()
        self.assertEqual(qa.validate_workflow(workflow, OPENAPI), [])
        workflow["nodes"]["account"]["max_turn_transition"] = "commit"
        self.check_has(workflow, "create_promise is reachable without a positive confirmation condition")

        workflow["nodes"]["account"].pop("max_turn_transition")
        workflow["nodes"]["account"]["transitions"][0]["condition"]["conditions"].append(
            {"operator": "always"})
        self.check_has(workflow, "create_promise is reachable without a positive confirmation condition")

    def test_tool_call_transition_is_not_a_confirmation_gate(self):
        workflow = confirmed_promise_workflow()
        transition_to_commit = workflow["nodes"]["account"]["transitions"][0]
        transition_to_commit["is_tool_call"] = True
        transition_to_commit["condition"] = {
            "operator": "matches_intent", "description": "Caller confirmed"}
        self.check_has(workflow, "create_promise is reachable without a positive confirmation condition")

    def test_no_tool_call_prompt_cannot_offer_transition_tool(self):
        workflow = confirmed_promise_workflow()
        workflow["nodes"]["account"]["task_message"] = (
            "No tool call is allowed in this node. Use transition_to_commit when ready.")
        workflow["nodes"]["account"]["transitions"][0]["is_tool_call"] = True
        self.check_has(workflow, "contradicts callable tools or is_tool_call transitions")

    def test_null_and_secret_are_found_at_nested_paths(self):
        workflow = valid_workflow()
        workflow["nodes"]["identity"]["max_turn_transition"] = None
        workflow["tools"][0]["function"]["parameters"]["properties"]["headers"] = {
            "properties": {"Authorization": {"default": "Bearer fak_1234567890abcdef"}}}
        self.check_has(workflow, "max_turn_transition: remove null")
        self.check_has(workflow, "remove the embedded secret")

    def test_stripped_actions_and_bad_passthrough_are_rejected(self):
        workflow = valid_workflow()
        workflow["nodes"]["account"]["pre_actions"] = [bank_tool("get_account")]
        workflow["nodes"]["account"]["passthrough"] = True
        workflow["nodes"]["account"]["transitions"] = [transition("finish", {
            "operator": "matches_intent", "description": "customer is done"})]
        self.check_has(workflow, "pre_actions are stripped")
        self.check_has(workflow, "finish a passthrough node")

    def test_wrong_url_required_field_type_and_enum_are_rejected(self):
        workflow = valid_workflow()
        tool = workflow["nodes"]["identity"]["tools"][0]
        props = tool["function"]["parameters"]["properties"]
        props["url"]["default"] = "https://evil.example/v1/verify_identity"
        self.check_has(workflow, "exact URL from openapi")
        props["url"]["default"] = BASE_URL + "/verify_identity"
        body = props["body"]
        body["required"].remove("answer")
        body["properties"]["answer"].pop("default")
        body["properties"]["field"]["type"] = "number"
        body["properties"]["field"]["enum"] = ["invented"]
        errors = qa.validate_workflow(workflow, OPENAPI)
        self.assertTrue(any("include unbaked API field 'answer'" in e for e in errors), errors)
        self.assertTrue(any("field.type: use 'string'" in e for e in errors), errors)
        self.assertTrue(any("nonempty subset" in e for e in errors), errors)

    def test_api_tool_identity_uses_operation_id_for_renamed_path(self):
        changed = copy.deepcopy(OPENAPI)
        operation = changed["paths"].pop("/verify_identity")
        operation["post"]["operationId"] = "verify_identity"
        changed["paths"]["/v3/security/check"] = operation
        workflow = valid_workflow()
        tool = workflow["nodes"]["identity"]["tools"][0]
        tool["function"]["parameters"]["properties"]["url"]["default"] = (
            BASE_URL + "/v3/security/check")
        self.assertEqual(qa.validate_workflow(workflow, changed), [])

        operation["post"]["operationId"] = "another_operation"
        errors = qa.validate_workflow(workflow, changed)
        self.assertTrue(any("must match OpenAPI operation 'another_operation'" in e
                            for e in errors), errors)

    def test_account_requires_positive_identity_route(self):
        workflow = valid_workflow()
        workflow["nodes"]["identity"]["transitions"][0]["condition"] = {
            "operator": "matches_intent", "description": "customer says yes"}
        self.check_has(workflow, "get_account may run before a verified identity gate")

    def test_source_forbidden_notes_cannot_follow_transfer(self):
        workflow = explicit_close_workflow()
        workflow["system_prompt"] = "On any transfer, add no notes at all."
        workflow["nodes"]["account"]["transitions"] = [transition("transfer")]
        workflow["nodes"]["transfer"] = {
            "name": "transfer", "node_type": "api_request",
            "passthrough": True, "passthrough_default_transition": "close",
            "pre_actions": [bank_tool("transfer_to_agent")],
            "transitions": [transition("close")],
        }
        self.assertEqual(qa.validate_workflow(workflow, OPENAPI), [])
        workflow["nodes"]["transfer"]["passthrough_default_transition"] = "note"
        workflow["nodes"]["transfer"]["transitions"] = [transition("note")]
        workflow["nodes"]["note"] = {
            "name": "note", "node_type": "api_request",
            "passthrough": True, "passthrough_default_transition": "close",
            "pre_actions": [bank_tool("add_note")],
            "transitions": [transition("close")],
        }
        self.check_has(workflow, "add_note is reachable after transfer_to_agent")

    def test_combined_confirmation_needs_product_disposition_gate(self):
        workflow = explicit_close_workflow()
        workflow["system_prompt"] = (
            "Read all promises back together and ask for one confirmation.")
        workflow["nodes"]["account"]["transitions"] = [transition("reducer")]
        workflow["nodes"]["reducer"] = {
            "name": "reducer", "node_type": "conversation",
            "task_message": "Resolve products in service order.",
            "transitions": [
                transition("confirmation", {"logic": "or", "conditions": [
                    {"variable": "multi_next", "operator": "equals", "operand": "confirm"}]}),
                transition("close"),
            ],
        }
        workflow["nodes"]["confirmation"] = {
            "name": "confirmation", "node_type": "conversation",
            "on_enter": {"type": "tts", "text": "{{confirmation_readback}} Do you confirm?"},
            "extraction_schema": [{"name": "confirmation_status", "type": "string"}],
            "transitions": [transition("close")],
        }
        self.assertEqual(qa.validate_workflow(workflow, OPENAPI), [])
        workflow["nodes"]["reducer"]["transitions"][0] = transition("confirmation")
        self.check_has(workflow, "combined confirmation is reachable before multi_next=confirm")

    def test_unreachable_or_dead_end_node_is_reported(self):
        workflow = valid_workflow()
        workflow["nodes"]["orphan"] = {"name": "orphan", "node_type": "conversation",
                                        "task_message": "Unused", "transitions": []}
        self.check_has(workflow, "orphan: unreachable")
        workflow["nodes"]["identity"]["transitions"] = [transition("identity")]
        self.check_has(workflow, "identity: no route to an end_call")

    def test_missing_close_call_and_model_override_are_reported(self):
        workflow = valid_workflow()
        workflow["tools"] = []
        workflow["agent_config"] = {"llmConfig": {"provider": "other", "model": "other"}}
        self.check_has(workflow, "add close_call")
        self.check_has(workflow, "omit it; Freya and freya.py set the required event model")

    def test_code_node_and_node_level_response_mapping_are_accepted(self):
        workflow = valid_workflow()
        workflow["nodes"]["prepare"] = {
            "name": "prepare", "node_type": "code", "on_enter": {"type": "silent"},
            "passthrough": True, "passthrough_default_transition": "identity",
            "pre_actions": [{"type": "function", "function": {
                "name": "setup", "handler": "__function__:code_exec",
                "parameters": {"type": "object", "properties": {"code": {
                    "type": "string", "default": "vars['ready'] = True"}}}}}],
            "transitions": [transition("identity")],
        }
        workflow["initial_node"] = "prepare"
        self.assertEqual(qa.validate_workflow(workflow, OPENAPI), [])

        workflow["nodes"]["prepare"]["response_mapping"] = [
            {"path": "data.ready", "variable": "ready"}]
        self.check_has(workflow, "cannot read code_exec output data")
        workflow["nodes"]["prepare"].pop("response_mapping")

        # A generated source string may be present in a valid JSON workflow
        # while still failing as soon as Freya enters the code node.
        code = workflow["nodes"]["prepare"]["pre_actions"][0]
        code["function"]["parameters"]["properties"]["code"]["default"] = (
            "vars['date_facts'] = 'today: ' + today.isoformat() +\n")
        self.check_has(workflow, "Python syntax error on line 1")

    def test_failed_confirmation_code_cannot_resume_ungated_discussion(self):
        policy = compile_policy([str(ROOT / "briefs" / "brief-a.md")],
                                str(ROOT / "tools" / "openapi.json"), None)
        workflow = build_workflow(policy, OPENAPI)
        node = workflow["nodes"]["prepare_confirmation"]
        node["transitions"] = [t for t in node["transitions"] if
                               "prepare_confirmation_failure_count" not in
                               qa._condition_variables(t["condition"])]
        node["transitions"][-1] = transition("account_discussion")
        node["passthrough_default_transition"] = "account_discussion"
        errors = qa.validate_workflow(workflow, OPENAPI)
        self.assertTrue(any("code_exec confirmation/promise gate needs a first" in e
                            and "prepare_confirmation" in e for e in errors), errors)
        self.assertTrue(any("prepare_confirmation.passthrough_default_transition" in e
                            and "ungated conversation" in e for e in errors), errors)

    def test_promise_success_speech_requires_successful_api_response(self):
        policy = compile_policy([str(ROOT / "briefs" / "brief-a.md")],
                                str(ROOT / "tools" / "openapi.json"), None)
        workflow = build_workflow(policy, OPENAPI)
        promise_call = workflow["nodes"]["create_promise_call"]
        promise_call["transitions"][-1] = transition("promise_success_notice")
        promise_call["passthrough_default_transition"] = "promise_success_notice"
        self.check_has(workflow, "recorded-promise success speech is reachable without an explicit successful create_promise response")

        promise_call["passthrough_default_transition"] = "promise_write_uncertain"
        promise_call["transitions"][-1] = transition("promise_write_uncertain")
        promise_call["transitions"][0]["condition"] = {"logic": "or", "conditions": [
            {"variable": "create_promise_call_response.data.ok",
             "operator": "equals", "operand": "true"}]}
        self.check_has(workflow, "recorded-promise notice requires an explicit successful bank response")

    def test_promise_retry_rejects_ambiguous_and_undocumented_results(self):
        policy = compile_policy([str(ROOT / "briefs" / "brief-a.md")],
                                str(ROOT / "tools" / "openapi.json"), None)
        workflow = build_workflow(policy, OPENAPI)
        self.assertEqual(qa.validate_workflow(workflow, OPENAPI), [])
        first = workflow["nodes"]["create_promise_call"]
        retry_route = next(t for t in first["transitions"]
                           if t["target_node"] == "prepare_promise_retry")
        retry_route["condition"] = {"operator": "always"}
        self.check_has(workflow, "create_promise retry requires an explicit bank nonrecording response")
        retry_route["condition"] = {"logic": "or", "conditions": [
            {"variable": "create_promise_call_failure_count",
             "operator": "greater_than", "operand": 0}]}
        self.check_has(workflow, "ambiguous create_promise result must close without retry")

        retry_route["condition"] = {"logic": "or", "conditions": [
            {"variable": "create_promise_call_response.status_code",
             "operator": "equals", "operand": "200"}]}
        self.check_has(workflow, "create_promise retry requires an explicit bank nonrecording response")

        retry_route["condition"] = copy.deepcopy(build_workflow(policy, OPENAPI)["nodes"]
                                                  ["create_promise_call"]["transitions"][1]["condition"])
        first["passthrough_default_transition"] = "prepare_promise_retry"
        self.check_has(workflow, "passthrough_default_transition: ambiguous create_promise result must close")

    def test_promise_retry_status_codes_follow_openapi(self):
        policy = compile_policy([str(ROOT / "briefs" / "brief-a.md")],
                                str(ROOT / "tools" / "openapi.json"), None)
        workflow = build_workflow(policy, OPENAPI)
        changed = copy.deepcopy(OPENAPI)
        responses = changed["paths"]["/create_promise"]["post"]["responses"]
        responses["503"] = responses.pop("500")
        properties = responses["200"]["content"]["application/json"]["schema"]["properties"]
        properties["recorded"] = properties.pop("ok")
        responses["200"]["description"] = (
            "`recorded` is false when the action was not carried out.")
        errors = qa.validate_workflow(workflow, changed)
        self.assertTrue(any("create_promise retry requires an explicit bank nonrecording response"
                            in e for e in errors), errors)
        updated = build_workflow(policy, changed)
        self.assertEqual(qa.validate_workflow(updated, changed), [])

    def test_uncertain_promise_speech_is_not_a_success_notice(self):
        uncertain = {"on_enter": {"type": "tts", "text":
                     "I cannot confirm whether your payment promise was recorded."}}
        self.assertFalse(qa._promise_success_notice(uncertain))

    def test_hooks_compile_without_executing_them(self):
        workflow = valid_workflow()
        tool = workflow["nodes"]["account"]["tools"][0]
        tool["pre_process_code"] = "raise RuntimeError('must not run during validation')"
        tool["post_process_code"] = "result['data'] = ("
        errors = qa.validate_workflow(workflow, OPENAPI)
        self.assertTrue(any("post_process_code: Python syntax error" in e for e in errors), errors)
        self.assertFalse(any("pre_process_code: Python syntax error" in e for e in errors), errors)

    def test_response_mapping_must_be_at_effective_level_with_data_path(self):
        workflow = explicit_close_workflow()
        action = workflow["nodes"]["close"]["pre_actions"][0]
        action["response_mapping"] = [{"path": "ok", "variable": "closed"}]
        workflow["nodes"]["account"]["response_mapping"] = [
            {"path": "data.ok", "variable": "account_ok"}]
        errors = qa.validate_workflow(workflow, OPENAPI)
        self.assertTrue(any("response_mapping is ignored; move it to the node" in e for e in errors), errors)
        self.assertTrue(any("path must start with 'data.'" in e for e in errors), errors)
        self.assertTrue(any("ignored on conversation nodes" in e for e in errors), errors)

        action.pop("response_mapping")
        workflow["nodes"]["account"].pop("response_mapping")
        workflow["nodes"]["close"]["response_mapping"] = [
            {"path": "data.ok", "variable": "closed"}]
        self.assertEqual(qa.validate_workflow(workflow, OPENAPI), [])


class PracticeWrapperTests(unittest.TestCase):
    def test_invokes_official_runner_and_returns_written_result(self):
        def fake_run(command, **kwargs):
            self.assertEqual(Path(command[1]).name, "run_cases.py")
            self.assertEqual(command[command.index("--brief") + 1], "B")
            self.assertEqual(command[command.index("--only") + 1], "A1,B2")
            self.assertEqual(command[command.index("--reps") + 1], "2")
            self.assertEqual(command[command.index("--style") + 1], "pieces")
            Path(command[command.index("--out") + 1]).write_text('{"passed": 2, "total": 2}')
            return SimpleNamespace(returncode=0, stdout="2 / 2 passed", stderr="")
        with patch.object(qa.subprocess, "run", side_effect=fake_run):
            result = qa.run_practice("agent-123", "B", only=["A1", "B2"], reps=2, style="pieces")
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["passed"], 2)


if __name__ == "__main__":
    unittest.main()

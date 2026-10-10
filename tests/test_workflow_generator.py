"""Offline acceptance checks for workflows built from bank source files.

These checks cover properties the Freya simulator cannot exercise offline. The
official `tools/run_cases.py` remains the authority for conversational scoring.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from policy_compiler import compile_policy  # noqa: E402
from qa import validate_workflow  # noqa: E402
from workflow_generator import build_workflow  # noqa: E402


TOOL_NAMES = {
    "verify_identity", "get_account", "create_promise", "add_note",
    "schedule_callback", "transfer_to_agent", "close_call",
}


def _source_policy(*briefs: str, openapi_path: Path | None = None) -> dict:
    paths = [str(ROOT / "briefs" / name) for name in briefs]
    return compile_policy(paths, str(openapi_path or ROOT / "tools" / "openapi.json"), None)


def _all_tools(workflow: dict) -> list[dict]:
    tools = list(workflow.get("tools", []))
    for node in (*workflow.get("nodes", {}).values(), *workflow.get("global_nodes", {}).values()):
        tools.extend(node.get("tools", []))
        tools.extend(node.get("pre_actions", []))
    return tools


def _named_tool(workflow: dict, name: str) -> list[dict]:
    return [tool for tool in _all_tools(workflow) if tool.get("function", {}).get("name") == name]


def _nodes_using_tool(workflow: dict, name: str) -> list[tuple[str, dict]]:
    return [(key, node) for key, node in workflow.get("nodes", {}).items()
            if any(tool.get("function", {}).get("name") == name
                   for tool in (*node.get("tools", []), *node.get("pre_actions", [])))]


def _text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _prepared_products(workflow: dict, products: list[dict], now: str) -> tuple[list[dict], dict]:
    """Execute only the generator's local account-preparation code, without Freya."""
    node = workflow["nodes"]["prepare_account"]
    code = node["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"]
    state = {"now": now, "account_products": products}
    exec(code, {"vars": state})
    return json.loads(state["product_json"]), state


def _run_code_node(workflow: dict, node_name: str, state: dict) -> dict:
    node = workflow["nodes"][node_name]
    code = node["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"]
    exec(code, {"vars": state})
    return state


def _code_node_writing(workflow: dict, variable: str) -> str:
    needle = f"vars['{variable}']"
    matches = [name for name, node in workflow["nodes"].items()
               if node.get("node_type") == "code" and any(
                   needle in str(tool.get("function", {}).get("parameters", {})
                                 .get("properties", {}).get("code", {}).get("default", ""))
                   for tool in node.get("pre_actions", []))]
    if not matches:
        raise AssertionError(f"No code node writes {variable}")
    return matches[0]


def _node_extracting(workflow: dict, variable: str) -> str:
    matches = [name for name, node in workflow["nodes"].items()
               if any(item.get("name") == variable for item in node.get("extraction_schema", []))]
    if len(matches) != 1:
        raise AssertionError(f"Expected one node extracting {variable}, found {matches}")
    return matches[0]


def _matches_deterministic_condition(condition: dict, state: dict) -> bool:
    """Evaluate the small equals/group subset used by generated result routes."""
    if "conditions" in condition:
        matches = [_matches_deterministic_condition(child, state)
                   for child in condition["conditions"]]
        return all(matches) if condition.get("logic") == "and" else any(matches)
    if condition.get("operator") == "equals":
        return str(state.get(condition.get("variable"), "")) == str(condition.get("operand"))
    return False


def _first_engine_route(node: dict, state: dict) -> str | None:
    """Select the first route using the condition subset emitted for API nodes."""
    def matches(condition: dict) -> bool:
        if "conditions" in condition:
            values = [matches(child) for child in condition["conditions"]]
            return all(values) if condition.get("logic") == "and" else any(values)
        operator = condition.get("operator")
        if operator == "always":
            return True
        variable = condition.get("variable")
        if operator == "equals":
            return str(state.get(variable, "")) == str(condition.get("operand"))
        if operator == "greater_than":
            try:
                return float(state.get(variable, 0)) > float(condition.get("operand"))
            except (TypeError, ValueError):
                return False
        return False

    return next((item["target_node"] for item in node.get("transitions", [])
                 if matches(item["condition"])), None)


def _embedded_python(workflow: dict):
    for tool in _all_tools(workflow):
        name = tool.get("function", {}).get("name", "unknown")
        params = tool.get("function", {}).get("parameters", {})
        if tool.get("function", {}).get("handler") == "__function__:code_exec":
            code = params.get("properties", {}).get("code", {}).get("default")
            if isinstance(code, str):
                yield f"{name}.code", code
        for field in ("pre_process_code", "post_process_code"):
            code = tool.get(field)
            if isinstance(code, str):
                yield f"{name}.{field}", code


class WorkflowGeneratorAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.openapi = json.loads((ROOT / "tools" / "openapi.json").read_text(encoding="utf-8"))

    def _build(self, *briefs: str, openapi: dict | None = None, openapi_path: Path | None = None) -> dict:
        source = _source_policy(*briefs, openapi_path=openapi_path)
        workflow = build_workflow(source, openapi or self.openapi)
        self.assertIsInstance(workflow, dict)
        return workflow

    def test_public_a_has_safe_identity_gate_and_all_seven_tools(self) -> None:
        workflow = self._build("brief-a.md")
        self.assertEqual([], validate_workflow(workflow, self.openapi))
        self.assertIn(workflow["initial_node"], workflow["nodes"])
        self.assertEqual(TOOL_NAMES, {tool["function"]["name"] for tool in _all_tools(workflow)
                                      if tool.get("function", {}).get("name") in TOOL_NAMES})

        first = workflow["nodes"][workflow["initial_node"]]
        spoken = first.get("on_enter", {}).get("text", "")
        self.assertTrue(spoken, "The opening disclosure must be spoken on entry")
        first_sentence = re.split(r"(?<=[.!?])\s+", spoken, maxsplit=1)[0].lower()
        self.assertIn("freya bank", first_sentence)
        self.assertIn("digital assistant", first_sentence)
        self.assertRegex(first_sentence, r"\b(?:recorded|recording|taped|taping|captured|saving|saved)\b")
        self.assertNotRegex(spoken.lower(), r"collections|debt|payment|credit card|loan|past.due")
        self.assertFalse(_named_tool({"tools": first.get("tools", []), "nodes": {}, "global_nodes": {}}, "get_account"))
        self.assertTrue(_named_tool(workflow, "verify_identity"))
        self.assertTrue(_named_tool(workflow, "get_account"))

    def test_exact_quoted_recording_opening_without_recorded_token_is_preserved(self) -> None:
        mandatory = "Hello, this is Acme Bank's digital assistant and this call is being taped."
        source = (
            f'First sentence must be exactly: "{mandatory}" '
            'Then ask: "Am I speaking with {title} {last_name}?" '
            "Before identity verification do not reveal collections, debt, payment, card, or loan details."
        )
        self.assertNotIn("recorded", source.lower())
        policy = {"global_rules": source, "opening": source, "identity": "Verify identity before account lookup.",
                  "verified_call": "", "outcomes": {}, "tool_policy": {}, "raw_brief": source,
                  "constraints": {"bank_name": "Acme Bank", "extraction_status": "source_only"}}
        workflow = build_workflow(policy, self.openapi)
        first = workflow["nodes"][workflow["initial_node"]]
        spoken = first["on_enter"]["text"]
        first_sentence = re.split(r"(?<=[.!?])\s+", spoken, maxsplit=1)[0]
        self.assertEqual(mandatory, first_sentence)
        self.assertNotRegex(spoken.lower(), r"collections|debt|payment|credit card|loan|past.due")

    def test_all_embedded_python_parses_before_freya_save(self) -> None:
        workflow = self._build("brief-a.md", "brief-b.md")
        sources = list(_embedded_python(workflow))
        self.assertTrue(sources)
        for label, code in sources:
            with self.subTest(label=label):
                compile(code, f"<workflow:{label}>", "exec")

    def test_promise_and_note_hooks_only_mark_successful_bank_writes(self) -> None:
        workflow = self._build("brief-a.md", "brief-b.md")
        promise_hook = _named_tool(workflow, "create_promise")[0]["post_process_code"]
        note_hook = _named_tool(workflow, "add_note")[0]["post_process_code"]
        state = {"recorded_ids": "[]", "promise_attempts": "{}", "note_reasons": "{}",
                 "pending_promise_product_id": "p1", "pending_note_product_id": "p2",
                 "pending_note_reason": "refused"}
        failed = {"status_code": 200, "data": {"ok": False}}
        exec(promise_hook, {"vars": state, "result": failed})
        self.assertEqual([], json.loads(state["recorded_ids"]))
        self.assertEqual(1, json.loads(state["promise_attempts"])["p1"])
        self.assertIn("not recorded", failed["data"].get("what_to_do_next", "").lower())

        succeeded = {"status_code": 200, "data": {"ok": True}}
        exec(promise_hook, {"vars": state, "result": succeeded})
        self.assertEqual(["p1"], json.loads(state["recorded_ids"]))
        self.assertIn("recorded", succeeded["data"].get("what_to_do_next", "").lower())

        noted = {"data": {"ok": True}}
        exec(note_hook, {"vars": state, "result": noted})
        self.assertEqual("refused", json.loads(state["note_reasons"])["p2"])

    def test_final_outcome_waits_for_each_product_result(self) -> None:
        workflow = self._build("brief-a.md", "brief-b.md")
        state = {"product_json": json.dumps([{"product_id": "p1"}, {"product_id": "p2"}]),
                 "recorded_ids": '["p1"]', "note_reasons": "{}"}
        _run_code_node(workflow, "finalize", state)
        self.assertEqual("no", state["processing_complete"])
        self.assertEqual("p2", state["missing_products"])

        state.update({"pending_note_product_id": "p2", "pending_note_reason": "refused"})
        note_hook = _named_tool(workflow, "add_note")[0]["post_process_code"]
        exec(note_hook, {"vars": state, "result": {"data": {"ok": True}}})
        _run_code_node(workflow, "finalize", state)
        self.assertEqual("yes", state["processing_complete"])
        self.assertEqual("promise", state["close_outcome"])
        self.assertEqual(["p1"], json.loads(state["recorded_ids"]))
        self.assertEqual({"p2": "refused"}, json.loads(state["note_reasons"]))

    def test_bank_b_common_date_uses_each_deadline_and_one_shared_confirmation(self) -> None:
        workflow = self._build("brief-a.md", "brief-b.md")
        now = "2026-10-14T11:00"
        products, _ = _prepared_products(workflow, [
            {"product_id": "p1", "type": "credit_card", "past_due": 420,
             "minimum_payment": 126, "days_late": 6},
            {"product_id": "p2", "type": "personal_loan", "past_due": 250,
             "instalment_amount": 75, "days_late": 20},
        ], now)
        self.assertEqual(["2026-11-02", "2026-10-23"],
                         [product["deadline"] for product in products])
        state = {"now": now, "product_json": json.dumps(products), "recorded_ids": "[]",
                 "write_index": "0", "proposed_terms_json": json.dumps([
                     {"product_id": "p1", "amount": 420, "date": "2026-10-30"},
                     {"product_id": "p2", "amount": 250, "date": "2026-10-30"},
                 ])}
        preparer = _code_node_writing(workflow, "proposal_validation_state")
        _run_code_node(workflow, preparer, state)
        self.assertEqual("ready", state["proposal_validation_state"])
        self.assertEqual([("p1", "2026-10-30"), ("p2", "2026-10-23")],
                         [(item["product_id"], item["date"])
                          for item in json.loads(state["readback_terms_json"])])
        self.assertIn("credit card: 420.00 on 2026-10-30", state["confirmation_readback"])
        self.assertIn("personal loan: 250.00 on 2026-10-23", state["confirmation_readback"])
        self.assertIn("latest date", state["confirmation_readback"].lower())

        confirmation_name = _node_extracting(workflow, "confirmation_status")
        confirmation = workflow["nodes"][confirmation_name]
        self.assertEqual([confirmation_name], [item["target_node"]
                         for item in workflow["nodes"][preparer]["transitions"]
                         if _matches_deterministic_condition(item["condition"], state)])
        validator = next(item["target_node"] for item in confirmation["transitions"]
                         if _matches_deterministic_condition(item["condition"],
                                                             {"confirmation_status": "confirmed"}))
        _run_code_node(workflow, validator, state)
        self.assertEqual("invalid", state["validation_state"])
        self.assertNotEqual("promise", state["write_state"])
        self.assertEqual([], json.loads(state["recorded_ids"]))

        state["confirmation_status"] = "confirmed"
        _run_code_node(workflow, validator, state)
        self.assertEqual("ready", state["validation_state"])
        self.assertEqual([("p1", "2026-10-30"), ("p2", "2026-10-23")],
                         [(item["product_id"], item["date"])
                          for item in json.loads(state["pending_promises_json"])])
        notice = workflow["nodes"]["promise_success_notice"]
        for index, expected_pid in enumerate(("p1", "p2")):
            self.assertEqual("promise", state["write_state"])
            self.assertEqual(expected_pid, state["pending_product_id"])
            route_node = workflow["nodes"][validator] if index == 0 else notice
            self.assertEqual(["create_promise_call"], [item["target_node"]
                             for item in route_node["transitions"]
                             if _matches_deterministic_condition(item["condition"], state)])
            _run_code_node(workflow, "mark_promise_success", state)
        self.assertEqual("done", state["write_state"])
        self.assertEqual(["prepare_notes"], [item["target_node"] for item in notice["transitions"]
                         if _matches_deterministic_condition(item["condition"], state)])
        self.assertEqual(["p1", "p2"], json.loads(state["recorded_ids"]))

    def test_bank_b_phone_retry_keeps_passed_dob(self) -> None:
        workflow = self._build("brief-a.md", "brief-b.md")
        dob = workflow["nodes"]["identity_dob"]
        phone = workflow["nodes"]["identity_phone"]
        verified_dob = {"verify_identity_response.data.verified": "true",
                        "verify_identity_response.data.all_verified": "false",
                        "verify_identity_response.data.wrong_answers_left": "3"}
        self.assertEqual(["identity_phone"], [item["target_node"] for item in dob["transitions"]
                         if _matches_deterministic_condition(item["condition"], verified_dob)])
        phone_tool = next(tool for tool in phone["tools"]
                          if tool["function"]["name"] == "verify_identity")
        body = phone_tool["function"]["parameters"]["properties"]["body"]["properties"]
        self.assertEqual("phone_last4", body["field"]["default"])
        first_wrong_phone = {"verify_identity_response.data.verified": "false",
                             "verify_identity_response.data.all_verified": "false",
                             "verify_identity_response.data.wrong_answers_left": "2",
                             "identity_intent": "answered"}
        self.assertEqual([], [item["target_node"] for item in phone["transitions"]
                              if _matches_deterministic_condition(item["condition"], first_wrong_phone)])
        self.assertNotIn("identity_dob", [item["target_node"] for item in phone["transitions"]])
        corrected_phone = dict(first_wrong_phone,
                               **{"verify_identity_response.data.verified": "true",
                                  "verify_identity_response.data.all_verified": "true"})
        self.assertEqual(["load_account"], [item["target_node"] for item in phone["transitions"]
                         if _matches_deterministic_condition(item["condition"], corrected_phone)])

    def test_bank_b_pub12_product_order_and_individual_attempt_limits(self) -> None:
        case = next(item for item in json.loads((ROOT / "public-test-set.json").read_text(encoding="utf-8"))
                    if item["id"] == "PUB-12")
        workflow = self._build("brief-a.md", "brief-b.md")
        account_products = [dict(item, product_id=f"p{index}")
                            for index, item in enumerate(case["products"], start=1)]
        products, state = _prepared_products(workflow, account_products, case["variables"]["now"])
        self.assertEqual(["p1", "p2", "p3"], [item["product_id"] for item in products])
        self.assertEqual(["credit card", "personal loan", "car loan"],
                         [item["label"].split(" ending ")[0] for item in products])
        self.assertEqual([510.0, 1320.0, 140.0], [item["past_due"] for item in products])
        self.assertEqual([153.0, 440.0, 46.0], [item["floor"] for item in products])
        self.assertEqual([2, 2, 1], [item["attempts"] for item in products])
        self.assertEqual(["2026-10-23", "2026-10-23", "2026-11-02"],
                         [item["deadline"] for item in products])
        self.assertLess(state["account_facts"].index("product ID p1"),
                        state["account_facts"].index("product ID p2"))
        self.assertLess(state["account_facts"].index("product ID p2"),
                        state["account_facts"].index("product ID p3"))

    def test_bank_b_pub12_refusals_advance_only_the_current_product(self) -> None:
        case = next(item for item in json.loads((ROOT / "public-test-set.json").read_text(encoding="utf-8"))
                    if item["id"] == "PUB-12")
        workflow = self._build("brief-a.md", "brief-b.md")
        account_products = [dict(item, product_id=f"p{index}")
                            for index, item in enumerate(case["products"], start=1)]
        products, state = _prepared_products(workflow, account_products, case["variables"]["now"])
        state.update({"product_json": json.dumps(products), "all_today_status": "no",
                      "proposed_terms_json": '[{"product_id":"p1","amount":510,"date":"tomorrow"}]',
                      "pending_note_reasons_json": "{}", "write_index": "0"})
        advance = _code_node_writing(workflow, "multi_offer_stage")
        offer = _code_node_writing(workflow, "multi_offer_state")
        _run_code_node(workflow, advance, state)
        self.assertEqual("offer", state["multi_next"])
        self.assertEqual("p2", state["multi_focus_product_id"])
        self.assertEqual("1", state["multi_offer_stage"],
                         "Saying no to all today must not consume p2's first offer")
        self.assertEqual({"p1"}, set(json.loads(state["multi_terms_json"])))
        self.assertEqual({}, json.loads(state["multi_notes_json"]))

        for expected_pid, expected_stage, expected_amount, expected_date in (
            ("p2", "1", 1320.0, "2026-10-16"),
            ("p2", "2", 440.0, "2026-10-23"),
            ("p3", "1", 140.0, "2026-10-16"),
        ):
            with self.subTest(product=expected_pid, stage=expected_stage):
                self.assertEqual("offer", state["multi_next"])
                self.assertEqual("1" if expected_pid == "p3" else expected_stage,
                                 state["multi_offer_stage"])
                _run_code_node(workflow, offer, state)
                self.assertEqual("ready", state["multi_offer_state"])
                self.assertEqual((expected_pid, expected_amount, expected_date),
                                 (state["multi_focus_product_id"],
                                  float(state["multi_offer_amount"]), state["multi_offer_date"]))
                self.assertIn("can you pay", state["multi_offer_text"].lower())
                state["multi_offer_action"] = "refused"
                _run_code_node(workflow, advance, state)
                if expected_pid == "p2" and expected_stage == "1":
                    self.assertEqual("2", state["multi_offer_stage"])
                    self.assertEqual({}, json.loads(state["multi_notes_json"]))
                elif expected_pid == "p2":
                    self.assertEqual("p3", state["multi_focus_product_id"])
                    self.assertEqual({"p2": "refused"}, json.loads(state["multi_notes_json"]))

        self.assertEqual("confirm", state["multi_next"])
        self.assertEqual({"p2": "refused", "p3": "refused"},
                         json.loads(state["multi_notes_json"]))
        self.assertEqual(["p1"], [item["product_id"] for item in json.loads(state["proposed_terms_json"])])
        preparer = _code_node_writing(workflow, "proposal_validation_state")
        _run_code_node(workflow, preparer, state)
        self.assertEqual("ready", state["proposal_validation_state"])
        self.assertEqual([{"product_id": "p1", "amount": 510.0, "date": "2026-10-15"}],
                         json.loads(state["readback_terms_json"]))
        self.assertNotIn("personal loan", state["confirmation_readback"].lower())
        self.assertNotIn("car loan", state["confirmation_readback"].lower())
        self.assertEqual([], json.loads(state["recorded_ids"]))

        confirmation = workflow["nodes"][_node_extracting(workflow, "confirmation_status")]
        validator = next(item["target_node"] for item in confirmation["transitions"]
                         if _matches_deterministic_condition(item["condition"],
                                                             {"confirmation_status": "confirmed"}))
        _run_code_node(workflow, validator, state)
        self.assertNotEqual("promise", state["write_state"])
        state["confirmation_status"] = "confirmed"
        _run_code_node(workflow, validator, state)
        self.assertEqual(("p1", 510.0, "2026-10-15"),
                         (state["pending_product_id"], state["pending_amount"], state["pending_date"]))
        self.assertEqual("promise", state["write_state"])
        _run_code_node(workflow, "mark_promise_success", state)
        self.assertEqual(["p1"], json.loads(state["recorded_ids"]))
        self.assertEqual("done", state["write_state"])
        _run_code_node(workflow, "prepare_notes", state)
        self.assertEqual([("p2", "refused"), ("p3", "refused")],
                         [(item["product_id"], item["reason"])
                          for item in json.loads(state["pending_notes_json"])])

    def test_bank_b_one_promise_two_refusal_notes_close_promise_once(self) -> None:
        workflow = self._build("brief-a.md", "brief-b.md")
        state = {"product_json": json.dumps([{"product_id": pid}
                                             for pid in ("p1", "p2", "p3")]),
                 "recorded_ids": '["p1"]', "note_reasons": "{}",
                 "pending_note_reasons_json": json.dumps({"p2": "refused", "p3": "refused"})}
        _run_code_node(workflow, "prepare_notes", state)
        self.assertEqual([("p2", "refused"), ("p3", "refused")],
                         [(item["product_id"], item["reason"])
                          for item in json.loads(state["pending_notes_json"])])
        note_tool = _named_tool(workflow, "add_note")[0]
        for expected_pid in ("p2", "p3"):
            _run_code_node(workflow, "select_note", state)
            self.assertEqual(expected_pid, state["pending_note_product_id"])
            exec(note_tool["post_process_code"], {"vars": state, "result": {"data": {"ok": True}}})
            _run_code_node(workflow, "advance_note", state)
        _run_code_node(workflow, "select_note", state)
        self.assertEqual("done", state["note_write_state"])
        _run_code_node(workflow, "finalize", state)
        self.assertEqual("yes", state["processing_complete"])
        self.assertEqual("promise", state["close_outcome"])
        self.assertEqual("", state["close_reason"])
        self.assertEqual({"p2": "refused", "p3": "refused"}, json.loads(state["note_reasons"]))
        self.assertEqual(1, len(_nodes_using_tool(workflow, "close_call")))

    def test_bank_b_failed_first_write_retries_same_confirmed_terms_without_success_speech(self) -> None:
        workflow = self._build("brief-a.md", "brief-b.md")
        now = "2026-10-14T11:00"
        products, _ = _prepared_products(workflow, [
            {"product_id": "p1", "type": "credit_card", "past_due": 420,
             "minimum_payment": 126, "days_late": 6},
            {"product_id": "p2", "type": "personal_loan", "past_due": 250,
             "instalment_amount": 75, "days_late": 20},
        ], now)
        state = {"now": now, "customer_id": "c_test", "product_json": json.dumps(products),
                 "recorded_ids": "[]", "promise_attempts": "{}", "write_index": "0",
                 "proposed_terms_json": json.dumps([
                     {"product_id": "p1", "amount": 420, "date": "2026-10-30"},
                     {"product_id": "p2", "amount": 250, "date": "2026-10-30"},
                 ])}
        _run_code_node(workflow, _code_node_writing(workflow, "proposal_validation_state"), state)
        self.assertEqual("ready", state["proposal_validation_state"])
        state["confirmation_status"] = "confirmed"
        confirmation = workflow["nodes"][_node_extracting(workflow, "confirmation_status")]
        validator = next(item["target_node"] for item in confirmation["transitions"]
                         if _matches_deterministic_condition(item["condition"], state))
        _run_code_node(workflow, validator, state)
        self.assertEqual("ready", state["validation_state"])
        self.assertEqual(("p1", 420.0, "2026-10-30"),
                         (state["pending_product_id"], state["pending_amount"], state["pending_date"]))

        first = workflow["nodes"]["create_promise_call"]
        retry_preparer = _first_engine_route(first, {
            "create_promise_call_response.status_code": "200",
            "create_promise_call_response.data.ok": "false",
        })
        self.assertEqual("prepare_promise_retry", retry_preparer)
        retry_name = _first_engine_route(workflow["nodes"][retry_preparer], {})
        self.assertEqual("retry_promise_call", retry_name)
        retry = workflow["nodes"][retry_name]
        first_tool = next(tool for tool in first["pre_actions"]
                          if tool["function"]["name"] == "create_promise")
        retry_tool = next(tool for tool in retry["pre_actions"]
                          if tool["function"]["name"] == "create_promise")
        first_body = first_tool["function"]["parameters"]["properties"]["body"]
        retry_body = retry_tool["function"]["parameters"]["properties"]["body"]
        self.assertEqual(first_body, retry_body, "Retry must reuse the exact confirmed body")
        frozen_terms = (state["pending_product_id"], state["pending_amount"], state["pending_date"])
        exec(first_tool["pre_process_code"], {"vars": state,
                                              "config": {"body": {"product_id": frozen_terms[0]}}})
        failed_result = {"status_code": 200, "data": {"ok": False}}
        exec(first_tool["post_process_code"], {"vars": state, "result": failed_result})
        self.assertEqual([], json.loads(state["recorded_ids"]))
        self.assertEqual(1, json.loads(state["promise_attempts"])["p1"])
        self.assertIn("not recorded", failed_result["data"]["what_to_do_next"].lower())
        self.assertEqual(frozen_terms,
                         (state["pending_product_id"], state["pending_amount"], state["pending_date"]))
        success_route = first["transitions"][0]
        self.assertFalse(_matches_deterministic_condition(success_route["condition"],
                                                            {"create_promise_call_response.status_code": "200",
                                                             "create_promise_call_response.data.ok": "false"}))
        self.assertEqual("mark_promise_success", success_route["target_node"])
        self.assertEqual("promise_write_uncertain", first["passthrough_default_transition"])
        self.assertNotIn("text message", str(retry.get("on_enter", {})).lower())

        success_result = {"status_code": 200, "data": {"ok": True}}
        exec(retry_tool["post_process_code"], {"vars": state, "result": success_result})
        self.assertEqual(["p1"], json.loads(state["recorded_ids"]))
        self.assertEqual(frozen_terms,
                         (state["pending_product_id"], state["pending_amount"], state["pending_date"]))
        _run_code_node(workflow, retry["transitions"][0]["target_node"], state)
        self.assertEqual(("p2", 250.0, "2026-10-23"),
                         (state["pending_product_id"], state["pending_amount"], state["pending_date"]))
        self.assertEqual(["create_promise_call"], [item["target_node"]
                         for item in workflow["nodes"]["promise_success_notice"]["transitions"]
                         if _matches_deterministic_condition(item["condition"], state)])

    def test_clear_confirmation_deterministically_unlocks_promise_write(self) -> None:
        workflow = self._build("brief-a.md")
        confirmation_name = _node_extracting(workflow, "confirmation_status")
        confirmation = workflow["nodes"][confirmation_name]
        state = {"now": "2026-10-14T11:00",
                 "proposed_terms_json": '[{"product_id":"p1","amount":420,"date":"2026-10-14"}]'}
        products, _ = _prepared_products(workflow, [{"product_id": "p1", "type": "credit_card",
                                                     "past_due": 420, "minimum_payment": 126,
                                                     "days_late": 6}], state["now"])
        state["product_json"] = json.dumps(products)

        discussion_name = _node_extracting(workflow, "discussion_action")
        state["discussion_action"] = "proposal_ready"
        proposal_route = [item for item in workflow["nodes"][discussion_name]["transitions"]
                          if _matches_deterministic_condition(item["condition"], state)]
        self.assertEqual(1, len(proposal_route))
        self.assertFalse(proposal_route[0].get("is_tool_call", False))
        preparer_name = proposal_route[0]["target_node"]
        _run_code_node(workflow, preparer_name, state)
        self.assertEqual("ready", state["proposal_validation_state"])
        self.assertIn("420.00 on 2026-10-14", state["confirmation_readback"])
        confirm_route = [item for item in workflow["nodes"][preparer_name]["transitions"]
                         if _matches_deterministic_condition(item["condition"], state)]
        self.assertEqual([confirmation_name], [item["target_node"] for item in confirm_route])
        self.assertNotEqual(preparer_name, workflow["nodes"][discussion_name].get("max_turn_transition"))

        state["confirmation_status"] = "confirmed"
        route = [item for item in confirmation["transitions"]
                 if _matches_deterministic_condition(item["condition"], state)]
        self.assertEqual(1, len(route))
        self.assertFalse(route[0].get("is_tool_call", False))
        validator_name = route[0]["target_node"]
        _run_code_node(workflow, validator_name, state)
        self.assertEqual("ready", state["validation_state"])
        self.assertEqual([{"product_id": "p1", "amount": 420.0, "date": "2026-10-14"}],
                         json.loads(state["pending_promises_json"]))
        self.assertEqual("promise", state["write_state"])
        self.assertEqual("p1", state["pending_product_id"])
        self.assertEqual(420.0, state["pending_amount"])
        write_route = [item for item in workflow["nodes"][validator_name]["transitions"]
                       if _matches_deterministic_condition(item["condition"], state)]
        self.assertTrue(write_route)
        writer = workflow["nodes"][write_route[0]["target_node"]]
        self.assertIn("create_promise", [tool["function"]["name"]
                                         for tool in writer.get("pre_actions", [])])

    def test_unconfirmed_or_invalid_terms_cannot_reach_promise_write(self) -> None:
        workflow = self._build("brief-a.md")
        confirmation_name = _node_extracting(workflow, "confirmation_status")
        validator_name = next(item["target_node"] for item in workflow["nodes"][confirmation_name]["transitions"]
                              if _matches_deterministic_condition(item["condition"], {"confirmation_status": "confirmed"}))
        preparer_name = _code_node_writing(workflow, "proposal_validation_state")
        products, _ = _prepared_products(workflow, [{"product_id": "p1", "type": "credit_card",
                                                     "past_due": 420, "minimum_payment": 126,
                                                     "days_late": 6}], "2026-10-14T11:00")
        for status, amount, date in (
            ("unclear", 420, "2026-10-14"),
            ("changed", 420, "2026-10-14"),
            ("confirmed", 500, "2026-10-14"),  # Above past-due amount.
            ("confirmed", 420, "2026-10-13"),  # Before call's now.
            ("confirmed", 420, "next month"),  # No exact date.
        ):
            with self.subTest(status=status, amount=amount, date=date):
                state = {"now": "2026-10-14T11:00", "product_json": json.dumps(products),
                         "confirmation_status": status,
                         "proposed_terms_json": json.dumps([{"product_id": "p1", "amount": amount,
                                                              "date": date}])}
                _run_code_node(workflow, preparer_name, state)
                if status == "confirmed":
                    self.assertEqual("invalid", state["proposal_validation_state"])
                _run_code_node(workflow, validator_name, state)
                self.assertEqual("invalid", state["validation_state"])
                self.assertEqual([], json.loads(state["pending_promises_json"]))
                select_name = _code_node_writing(workflow, "write_state")
                _run_code_node(workflow, select_name, state)
                self.assertNotEqual("promise", state["write_state"])

    def test_this_friday_is_resolved_from_call_now_before_confirmation(self) -> None:
        """PUB-10: the caller's weekday words must reach deterministic date parsing."""
        workflow = self._build("brief-a.md")
        discussion = workflow["nodes"][_node_extracting(workflow, "discussion_action")]
        terms_field = next(item for item in discussion["extraction_schema"]
                           if item["name"] == "proposed_terms_json")
        self.assertIn("raw", terms_field["description"].lower())
        self.assertIn("{{now}}", terms_field["description"])
        self.assertNotIn("convert full amount to the numeric past due and dates to iso",
                         terms_field["description"].lower())

        now = "2026-10-14T11:00"  # Wednesday; unrelated to the host clock.
        products, _ = _prepared_products(workflow, [
            {"product_id": "p1", "type": "credit_card", "past_due": 365,
             "minimum_payment": 109.5, "days_late": 6},
        ], now)
        state = {"now": now, "product_json": json.dumps(products),
                 "discussion_action": "proposal_ready",
                 "proposed_terms_json": json.dumps([
                     {"product_id": "p1", "amount": 365, "date": "this Friday"},
                 ])}
        preparer = _code_node_writing(workflow, "proposal_validation_state")
        _run_code_node(workflow, preparer, state)
        self.assertEqual("ready", state["proposal_validation_state"])
        self.assertEqual("2026-10-16", json.loads(state["readback_terms_json"])[0]["date"])
        self.assertIn("365.00 on 2026-10-16", state["confirmation_readback"])

        # If one extractor still invents an old ISO year, the independent
        # verbatim date field retains the caller's actual words for the parser.
        date_words = next(item for item in discussion["extraction_schema"]
                          if item["name"] == "proposed_date_words")
        self.assertIn("verbatim", date_words["description"].lower())
        recovered = {"now": now, "product_json": json.dumps(products),
                     "discussion_action": "proposal_ready",
                     "proposed_date_words": "this Friday",
                     "proposed_terms_json": json.dumps([
                         {"product_id": "p1", "amount": 365, "date": "2025-05-23"},
                     ])}
        _run_code_node(workflow, preparer, recovered)
        self.assertEqual("ready", recovered["proposal_validation_state"])
        self.assertEqual("2026-10-16", json.loads(recovered["readback_terms_json"])[0]["date"])

    def test_relative_and_holiday_dates_follow_the_brief_across_year_end(self) -> None:
        workflow = self._build("brief-a.md")
        preparer = _code_node_writing(workflow, "proposal_validation_state")
        for now, spoken_date, expected_date, adjustment in (
            ("2026-12-30T11:00", "next Friday", "2027-01-08", False),
            ("2026-12-31T11:00", "this Friday", "2027-01-04", True),
            ("2026-10-23T11:00", "2026-10-26", "2026-10-27", True),
        ):
            with self.subTest(now=now, spoken_date=spoken_date):
                products, _ = _prepared_products(workflow, [
                    {"product_id": "p1", "type": "credit_card", "past_due": 365,
                     "minimum_payment": 109.5, "days_late": 6},
                ], now)
                state = {"now": now, "product_json": json.dumps(products),
                         "proposed_terms_json": json.dumps([
                             {"product_id": "p1", "amount": 365, "date": spoken_date},
                         ])}
                _run_code_node(workflow, preparer, state)
                self.assertEqual("ready", state["proposal_validation_state"])
                self.assertEqual(expected_date, json.loads(state["readback_terms_json"])[0]["date"])
                self.assertEqual(adjustment, "next business day" in state["confirmation_readback"].lower())

        # An unresolvable or past date remains a clarification, never a promise.
        products, _ = _prepared_products(workflow, [
            {"product_id": "p1", "type": "credit_card", "past_due": 365,
             "minimum_payment": 109.5, "days_late": 6},
        ], "2026-10-23T11:00")
        for bad_date in ("2026-10-22", "this Friday 2026-10-22"):
            with self.subTest(bad_date=bad_date):
                state = {"now": "2026-10-23T11:00", "product_json": json.dumps(products),
                         "proposed_terms_json": json.dumps([
                             {"product_id": "p1", "amount": 365, "date": bad_date},
                         ])}
                _run_code_node(workflow, preparer, state)
                self.assertEqual("invalid", state["proposal_validation_state"])
                self.assertEqual([], json.loads(state["readback_terms_json"]))

    def test_confirmed_relative_date_writes_exactly_one_valid_promise(self) -> None:
        workflow = self._build("brief-a.md")
        now = "2026-10-14T11:00"
        products, _ = _prepared_products(workflow, [
            {"product_id": "p1", "type": "credit_card", "past_due": 365,
             "minimum_payment": 109.5, "days_late": 6},
        ], now)
        state = {"now": now, "customer_id": "c_test", "product_json": json.dumps(products),
                 "recorded_ids": "[]", "write_index": "0",
                 "proposed_terms_json": json.dumps([
                     {"product_id": "p1", "amount": 365, "date": "this Friday"},
                 ])}
        _run_code_node(workflow, _code_node_writing(workflow, "proposal_validation_state"), state)
        self.assertEqual("ready", state["proposal_validation_state"])
        state["confirmation_status"] = "confirmed"
        confirmation = workflow["nodes"][_node_extracting(workflow, "confirmation_status")]
        validator = next(item["target_node"] for item in confirmation["transitions"]
                         if _matches_deterministic_condition(item["condition"], state))
        _run_code_node(workflow, validator, state)
        self.assertEqual("ready", state["validation_state"])
        self.assertEqual("promise", state["write_state"])
        self.assertEqual(("p1", 365.0, "2026-10-16"),
                         (state["pending_product_id"], state["pending_amount"], state["pending_date"]))
        self.assertEqual(["create_promise_call"], [item["target_node"]
                         for item in workflow["nodes"][validator]["transitions"]
                         if _matches_deterministic_condition(item["condition"], state)])

        writer = workflow["nodes"]["create_promise_call"]
        promise_tool = next(tool for tool in writer["pre_actions"]
                            if tool["function"]["name"] == "create_promise")
        body = promise_tool["function"]["parameters"]["properties"]["body"]
        self.assertEqual({"customer_id": "{{customer_id}}", "product_id": "{{pending_product_id}}",
                          "amount": "{{pending_amount}}", "date": "{{pending_date}}"},
                         {key: value["default"] for key, value in body["properties"].items()})
        exec(promise_tool["pre_process_code"], {"vars": state,
                                                "config": {"body": {"product_id": state["pending_product_id"]}}})
        exec(promise_tool["post_process_code"], {"vars": state,
                                                 "result": {"status_code": 200, "data": {"ok": True}}})
        self.assertEqual(["p1"], json.loads(state["recorded_ids"]))
        _run_code_node(workflow, "mark_promise_success", state)
        self.assertEqual("done", state["write_state"])
        self.assertEqual(["prepare_notes"], [item["target_node"]
                         for item in workflow["nodes"]["promise_success_notice"]["transitions"]
                         if _matches_deterministic_condition(item["condition"], state)])
        self.assertEqual(["p1"], json.loads(state["recorded_ids"]))
        self.assertEqual("1", state["write_index"])

    def test_invalid_proposal_clears_stale_route_before_clarification(self) -> None:
        """PUB-10: an invalid extracted date must not keep reentering preparation."""
        workflow = self._build("brief-a.md")
        discussion_name = _node_extracting(workflow, "discussion_action")
        discussion = workflow["nodes"][discussion_name]
        action_field = next(item for item in discussion["extraction_schema"]
                            if item["name"] == "discussion_action")
        self.assertIn("bare yes", action_field["description"].lower())
        now = "2026-10-14T11:00"
        products, _ = _prepared_products(workflow, [
            {"product_id": "p1", "type": "credit_card", "past_due": 365,
             "minimum_payment": 109.5, "days_late": 6},
        ], now)
        state = {"now": now, "product_json": json.dumps(products),
                 "discussion_action": "proposal_ready",
                 "proposed_terms_json": json.dumps([
                     {"product_id": "p1", "amount": 365, "date": "2025-05-23"},
                 ])}
        preparer = _code_node_writing(workflow, "proposal_validation_state")
        _run_code_node(workflow, preparer, state)
        self.assertEqual("invalid", state["proposal_validation_state"])
        self.assertNotEqual("proposal_ready", state.get("discussion_action"))
        self.assertFalse(state.get("proposed_terms_json"))
        self.assertFalse(state.get("proposed_date_words"))
        routes = [item["target_node"] for item in workflow["nodes"][preparer]["transitions"]
                  if _matches_deterministic_condition(item["condition"], state)]
        self.assertEqual([discussion_name], routes)
        # A later bare "yes" supplies no new product, amount, or date terms.
        self.assertFalse(state.get("proposed_terms_json"))
        self.assertFalse(any(_matches_deterministic_condition(item["condition"], state)
                             and item["target_node"] == preparer
                             for item in discussion["transitions"]))

    def test_failed_confirmation_code_uses_safe_fallback_without_promise(self) -> None:
        """PUB-01: a failed code_exec has no validated read-back or write state."""
        workflow = self._build("brief-a.md")
        preparer = _code_node_writing(workflow, "proposal_validation_state")
        node = workflow["nodes"][preparer]
        self.assertEqual("code", node["node_type"])
        self.assertTrue(any(tool["function"].get("handler") == "__function__:code_exec"
                            for tool in node["pre_actions"]))
        # Freya may record a failure counter while the code sandbox never runs.
        # The stale proposal must not send the call back into discussion.
        state = {"discussion_action": "proposal_ready",
                 "proposed_terms_json": '[{"product_id":"p1","amount":365,"date":"this Friday"}]',
                 preparer + "_failure_count": 1}
        self.assertNotIn("proposal_validation_state", state)
        self.assertNotIn("confirmation_readback", state)
        failure_route = next(item for item in node["transitions"]
                             if item["condition"].get("conditions") and any(
                                 child.get("variable") == preparer + "_failure_count"
                                 and child.get("operator") == "greater_than"
                                 and child.get("operand") == 0
                                 for child in item["condition"]["conditions"]))
        safe_name = failure_route["target_node"]
        self.assertNotEqual(_node_extracting(workflow, "discussion_action"), safe_name)
        self.assertNotEqual(_node_extracting(workflow, "confirmation_status"), safe_name)
        self.assertEqual(safe_name, node["passthrough_default_transition"],
                         "An execution failure must be safe even if Freya uses the default route")

        # Follow the fixed fallback until the terminal node. It may close as a
        # service error, but must never read back unvalidated terms or write a promise.
        seen = set()
        current = safe_name
        while current not in seen:
            seen.add(current)
            current_node = workflow["nodes"][current]
            self.assertFalse(any(tool["function"].get("name") == "create_promise"
                                 for tool in (*current_node.get("tools", []),
                                              *current_node.get("pre_actions", []))))
            speech = str(current_node.get("on_enter", {}).get("text", ""))
            self.assertNotIn("{{confirmation_readback}}", speech)
            self.assertNotIn("you will receive a text message", speech.lower())
            if current_node["node_type"] == "end_call":
                break
            current = current_node.get("passthrough_default_transition")
            self.assertIsNotNone(current, "Fallback must terminate deterministically")
        self.assertEqual("end_call", workflow["nodes"][current]["node_type"])

    def test_corrected_terms_clear_old_readback_and_require_new_confirmation(self) -> None:
        workflow = self._build("brief-a.md")
        confirmation_name = _node_extracting(workflow, "confirmation_status")
        confirmation = workflow["nodes"][confirmation_name]
        preparer_name = _code_node_writing(workflow, "proposal_validation_state")
        products, _ = _prepared_products(workflow, [{"product_id": "p1", "type": "credit_card",
                                                     "past_due": 420, "minimum_payment": 126,
                                                     "days_late": 6}], "2026-10-14T11:00")
        state = {"now": "2026-10-14T11:00", "product_json": json.dumps(products),
                 "proposed_terms_json": '[{"product_id":"p1","amount":420,"date":"2026-10-14"}]'}
        _run_code_node(workflow, preparer_name, state)
        self.assertIn("420.00 on 2026-10-14", state["confirmation_readback"])
        state["confirmation_status"] = "changed"
        correction = [item for item in confirmation["transitions"]
                      if _matches_deterministic_condition(item["condition"], state)]
        self.assertEqual(1, len(correction))
        clearing_node = workflow["nodes"][correction[0]["target_node"]]
        self.assertEqual("code", clearing_node["node_type"])
        _run_code_node(workflow, correction[0]["target_node"], state)
        self.assertEqual("invalid", state["validation_state"])
        self.assertEqual([], json.loads(state["pending_promises_json"]))
        self.assertFalse(state.get("confirmation_readback"))
        self.assertFalse(state.get("proposed_terms_json"))

        # The corrected amount enters a fresh proposal and fixed read-back.
        state["proposed_terms_json"] = '[{"product_id":"p1","amount":200,"date":"2026-10-16"}]'
        _run_code_node(workflow, preparer_name, state)
        self.assertEqual("ready", state["proposal_validation_state"])
        self.assertIn("200.00 on 2026-10-16", state["confirmation_readback"])
        validator_name = next(item["target_node"] for item in confirmation["transitions"]
                              if _matches_deterministic_condition(item["condition"], {"confirmation_status": "confirmed"}))
        _run_code_node(workflow, validator_name, state)
        self.assertEqual("invalid", state["validation_state"])
        state["confirmation_status"] = "confirmed"  # Only the new read-back gets a clear yes.
        _run_code_node(workflow, validator_name, state)
        self.assertEqual("ready", state["validation_state"])
        self.assertEqual(200.0, json.loads(state["pending_promises_json"])[0]["amount"])

    def test_strict_promise_retry_requires_current_explicit_bank_result(self) -> None:
        workflow = self._build("brief-a.md")
        first = workflow["nodes"]["create_promise_call"]
        retry = workflow["nodes"]["retry_promise_call"]
        for name, node, false_target in (
            ("create_promise_call", first, "prepare_promise_retry"),
            ("retry_promise_call", retry, "mark_promise_error"),
        ):
            prefix = name + "_response."
            with self.subTest(node=name):
                self.assertEqual("mark_promise_success", _first_engine_route(node, {
                    prefix + "status_code": "200", prefix + "data.ok": "true"}))
                self.assertEqual(false_target, _first_engine_route(node, {
                    prefix + "status_code": "200", prefix + "data.ok": "false"}))
                # An explicit HTTP 500 is known not to have recorded a promise
                # when Freya exposes its status; an opaque transport error is not.
                self.assertEqual(false_target, _first_engine_route(node, {
                    prefix + "status_code": "500"}))
                for uncertain in (
                    {},
                    {prefix + "status_code": "200"},
                    {prefix + "data.ok": "false"},
                    {name + "_failure_count": "1"},
                ):
                    self.assertEqual("promise_write_uncertain", _first_engine_route(node, uncertain))
                self.assertEqual("promise_write_uncertain", node["passthrough_default_transition"])

        retry_preparer = workflow["nodes"]["prepare_promise_retry"]
        self.assertEqual("retry_promise_call", _first_engine_route(retry_preparer, {}))
        self.assertEqual("service_unavailable", _first_engine_route(retry_preparer, {
            "prepare_promise_retry_failure_count": "1"}))

        # The bank write hook must not turn a missing response into a known
        # failure, schedule a retry, or claim that the promise was recorded.
        hook = next(tool["post_process_code"] for tool in first["pre_actions"]
                    if tool["function"]["name"] == "create_promise")
        state = {"pending_promise_product_id": "p1", "recorded_ids": "[]",
                 "promise_attempts": "{}"}
        opaque = {"status": "error", "message": "transport lost"}
        exec(hook, {"vars": state, "result": opaque})
        self.assertEqual([], json.loads(state["recorded_ids"]))
        self.assertEqual({}, json.loads(state["promise_attempts"]))
        self.assertIn("do not retry", str(opaque.get("data", {})).lower())
        self.assertIn("do not", str(opaque.get("data", {})).lower())

        explicit_500 = {"status_code": 500, "data": {}}
        exec(hook, {"vars": state, "result": explicit_500})
        self.assertEqual([], json.loads(state["recorded_ids"]))
        self.assertEqual(1, json.loads(state["promise_attempts"])["p1"])

        uncertainty = workflow["nodes"]["promise_write_uncertain"]
        success_speech = workflow["nodes"]["promise_success_notice"]["on_enter"]["text"]
        self.assertNotEqual(success_speech, uncertainty["on_enter"].get("text"))
        self.assertNotEqual("retry_promise_call", uncertainty.get("passthrough_default_transition"))

    def test_next_product_clears_prior_promise_response_before_deciding_result(self) -> None:
        workflow = self._build("brief-a.md", "brief-b.md")
        first = workflow["nodes"]["create_promise_call"]
        promise_tool = next(tool for tool in first["pre_actions"]
                            if tool["function"]["name"] == "create_promise")
        state = {"confirmation_status": "confirmed", "validation_state": "ready",
                 "pending_promises_json": json.dumps([
                     {"product_id": "p1", "amount": 420, "date": "2026-10-16"},
                     {"product_id": "p2", "amount": 250, "date": "2026-10-23"},
                 ]),
                 "write_index": "1", "recorded_ids": '["p1"]', "promise_attempts": "{}",
                 "create_promise_call_response.status_code": "200",
                 "create_promise_call_response.data.ok": "true",
                 "retry_promise_call_response.status_code": "200",
                 "retry_promise_call_response.data.ok": "true"}
        _run_code_node(workflow, "record_results", state)
        self.assertEqual("p2", state["pending_product_id"])
        self.assertEqual("promise", state["write_state"])
        for name in ("create_promise_call", "retry_promise_call"):
            self.assertFalse(state.get(name + "_response.status_code"))
            self.assertFalse(state.get(name + "_response.data.ok"))
        self.assertEqual("promise_write_uncertain", _first_engine_route(first, state))

        # The API pre-hook repeats the reset immediately before the p2 call.
        state["create_promise_call_response.status_code"] = "200"
        state["create_promise_call_response.data.ok"] = "true"
        exec(promise_tool["pre_process_code"], {"vars": state,
                                                "config": {"body": {"product_id": "p2"}}})
        self.assertFalse(state.get("create_promise_call_response.data.ok"))
        self.assertEqual("p2", state["pending_promise_product_id"])
        self.assertEqual("promise_write_uncertain", _first_engine_route(first, state))

        # A fresh explicit negative result belongs to p2 even though p1 was
        # already recorded successfully; it cannot trigger p2 success speech.
        result = {"status_code": 200, "data": {"ok": False}}
        exec(promise_tool["post_process_code"], {"vars": state, "result": result})
        self.assertEqual(["p1"], json.loads(state["recorded_ids"]))
        self.assertEqual({"p2": 1}, json.loads(state["promise_attempts"]))
        current = {"create_promise_call_response.status_code": "200",
                   "create_promise_call_response.data.ok": "false"}
        self.assertEqual("prepare_promise_retry", _first_engine_route(first, current))

    def test_promise_api_result_controls_recorded_state_and_close_outcome(self) -> None:
        workflow = self._build("brief-a.md")
        writers = [(name, node) for name, node in _nodes_using_tool(workflow, "create_promise")
                   if node.get("node_type") == "api_request"]
        self.assertEqual(2, len(writers), "A failed promise should have one bounded retry")
        first_name, first = writers[0]
        first_failure = {first_name + "_response.status_code": "200",
                         first_name + "_response.data.ok": "false"}
        retry_preparer = _first_engine_route(first, first_failure)
        self.assertEqual("prepare_promise_retry", retry_preparer)
        second_name = _first_engine_route(workflow["nodes"][retry_preparer], {})
        self.assertIn(second_name, dict(writers))
        second = workflow["nodes"][second_name]
        base = {"product_json": '[{"product_id":"p1"}]', "recorded_ids": "[]",
                "note_reasons": "{}", "pending_product_id": "p1",
                "create_promise_args.product_id": "p1", "write_index": "0",
                "promise_attempts": "{}"}

        success = dict(base)
        promise_tool = next(tool for tool in first["pre_actions"]
                            if tool.get("function", {}).get("name") == "create_promise")
        exec(promise_tool["post_process_code"], {"vars": success,
                                                 "result": {"status_code": 200, "data": {"ok": True}}})
        success[first_name + "_response.status_code"] = "200"
        success[first_name + "_response.data.ok"] = "true"
        self.assertEqual("mark_promise_success", _first_engine_route(first, success))
        _run_code_node(workflow, _first_engine_route(first, success), success)
        self.assertEqual(["p1"], json.loads(success["recorded_ids"]))
        _run_code_node(workflow, "prepare_notes", success)
        _run_code_node(workflow, "finalize", success)
        self.assertEqual("promise", success["close_outcome"])
        self.assertEqual("", success["close_reason"])

        failure = dict(base)
        exec(promise_tool["post_process_code"], {"vars": failure,
                                                 "result": {"status_code": 200, "data": {"ok": False}}})
        self.assertEqual(retry_preparer, _first_engine_route(first, first_failure))
        _run_code_node(workflow, retry_preparer, failure)
        retry_tool = next(tool for tool in second["pre_actions"]
                          if tool.get("function", {}).get("name") == "create_promise")
        exec(retry_tool["post_process_code"], {"vars": failure,
                                               "result": {"status_code": 200, "data": {"ok": False}}})
        second_failure = {second_name + "_response.status_code": "200",
                          second_name + "_response.data.ok": "false"}
        error_name = _first_engine_route(second, second_failure)
        self.assertEqual("mark_promise_error", error_name)
        _run_code_node(workflow, error_name, failure)
        self.assertEqual([], json.loads(failure["recorded_ids"]))
        self.assertEqual(2, json.loads(failure["promise_attempts"])["p1"])
        _run_code_node(workflow, "prepare_notes", failure)
        _run_code_node(workflow, "finalize", failure)
        self.assertEqual("no_promise", failure["close_outcome"])
        self.assertEqual("service_error", failure["close_reason"])

    def test_successful_transfer_skips_notes_and_finishes_after_one_farewell(self) -> None:
        workflow = self._build("brief-a.md", "brief-b.md")
        transfer_nodes = _nodes_using_tool(workflow, "transfer_to_agent")
        self.assertTrue(transfer_nodes)
        transfer_tool = next(tool for _, node in transfer_nodes for tool in node.get("pre_actions", [])
                             if tool.get("function", {}).get("name") == "transfer_to_agent")
        state = {"product_json": json.dumps([{"product_id": "p1"}, {"product_id": "p2"},
                                               {"product_id": "p3"}]),
                 "recorded_ids": '["p1"]', "note_reasons": "{}",
                 "pending_note_reasons_json": '{"p2":"refused","p3":"refused"}',
                 "pending_transfer_queue": "restructuring",
                 "pending_transfer_reason": "restructuring"}
        exec(transfer_tool["post_process_code"], {"vars": state, "result": {"data": {"ok": True}}})
        self.assertEqual("restructuring", state["transfer_reason"])
        _run_code_node(workflow, "finalize", state)
        self.assertEqual("transferred", state["close_outcome"])
        self.assertEqual("restructuring", state["close_reason"])
        self.assertEqual("yes", state["processing_complete"])
        self.assertEqual({}, json.loads(state["note_reasons"]))
        self.assertEqual("finalize", workflow["nodes"]["transfer_restructuring"]["passthrough_default_transition"])

        # A successful close path has one farewell, one close_call API node,
        # and an end_call terminal. A second dialogue node would let the model
        # resume saying Goodbye repeatedly after the bank action succeeded.
        close_nodes = _nodes_using_tool(workflow, "close_call")
        self.assertEqual(1, len(close_nodes))
        close_name, close_node = close_nodes[0]
        end_name = close_node.get("passthrough_default_transition")
        self.assertEqual("end_call", workflow["nodes"][end_name]["node_type"])
        farewell_nodes = [node for node in workflow["nodes"].values()
                          if node.get("passthrough_default_transition") == close_name]
        self.assertTrue(farewell_nodes)
        self.assertTrue(all(node.get("passthrough") for node in farewell_nodes))

    def test_public_b_carries_multi_product_rules_and_call_time(self) -> None:
        workflow = self._build("brief-a.md", "brief-b.md")
        self.assertEqual([], validate_workflow(workflow, self.openapi))
        text = _text(workflow).lower()
        # These requirements must reach the agent even when source-only policy
        # compilation is used because the model provider is unavailable.
        for concept in ("product", "deadline", "read-back", "add_note", "now"):
            self.assertIn(concept, text)
        self.assertTrue(_named_tool(workflow, "add_note"))
        self.assertNotIn("system.todays_date", text)
        self.assertNotIn("system.iso_utc_now", text)
        self.assertNotRegex(text, r"datetime\.now\(|date\.today\(")

    def test_multi_product_deadlines_are_per_product_and_move_with_now(self) -> None:
        workflow = self._build("brief-a.md", "brief-b.md")
        cases = json.loads((ROOT / "public-test-set.json").read_text(encoding="utf-8"))
        case = next(item for item in cases if item["id"] == "PUB-11")
        products = [{**item, "product_id": f"p{index}"}
                    for index, item in enumerate(case["products"], start=1)]
        prepared, state = _prepared_products(workflow, products, "2026-10-14T11:00")
        self.assertEqual(["p1", "p2", "p3"], [item["product_id"] for item in prepared])
        self.assertEqual(["2026-10-23", "2026-10-23", "2026-11-02"],
                         [item["deadline"] for item in prepared])
        self.assertEqual([66.0, 213.0, 25.0], [item["floor"] for item in prepared])
        self.assertIn("2026-10-14", state["date_facts"])

        later, later_state = _prepared_products(workflow, products, "2026-11-12T11:00")
        self.assertEqual(["2026-11-23", "2026-11-23", "2026-12-01"],
                         [item["deadline"] for item in later])
        self.assertIn("2026-11-12", later_state["date_facts"])

    def test_sample_now_date_is_not_misread_as_a_bank_holiday(self) -> None:
        workflow = self._build("brief-a.md")
        prepared, state = _prepared_products(
            workflow, [{"product_id": "p1", "type": "credit_card", "past_due": 100,
                        "days_late": 9, "minimum_payment": 30}], "2026-10-13T11:00")
        # Seven business days after Tuesday 13 October are 14, 15, 16, 19,
        # 20, 21, and 22 October. The brief's sample now date is 14 October;
        # it is not in the holiday list.
        self.assertEqual("2026-10-22", prepared[0]["deadline"])
        self.assertNotIn("2026-10-14", state["date_facts"].split("bank holidays: ")[-1])

    def test_account_preparation_reads_actual_freya_flattened_result_prefix(self) -> None:
        """Freya uses the action-node name, not the bank tool name, in flattened keys."""
        workflow = self._build("brief-a.md")
        state = {
            "now": "2026-10-14T11:00",
            "load_account_response.data.products.0.product_id": "p1",
            "load_account_response.data.products.0.type": "credit_card",
            "load_account_response.data.products.0.card_last4": "4821",
            "load_account_response.data.products.0.past_due": "420.0",
            "load_account_response.data.products.0.days_late": "6",
            "load_account_response.data.products.0.minimum_payment": "126.0",
        }
        _run_code_node(workflow, "prepare_account", state)
        products = json.loads(state["product_json"])
        self.assertEqual("1", state["product_count"])
        self.assertEqual(1, len(products))
        self.assertEqual("p1", products[0]["product_id"])
        self.assertEqual(420.0, products[0]["past_due"])
        self.assertEqual(126.0, products[0]["floor"])
        self.assertIn("credit card ending 4821", state["account_facts"])

    def test_successful_callback_hook_immediately_matches_callback_route(self) -> None:
        workflow = self._build("brief-a.md")
        callback = _named_tool(workflow, "schedule_callback")[0]
        state: dict = {}
        result = {"data": {"ok": True}}
        exec(callback["post_process_code"], {"vars": state, "result": result})
        self.assertIn("callback_recorded", state)
        matching = [transition["target_node"] for transition in workflow["nodes"]["third_party"]["transitions"]
                    if _matches_deterministic_condition(transition["condition"], state)]
        self.assertTrue(matching, "A successful schedule_callback must match a deterministic route")
        self.assertEqual("finish_callback", matching[0])
        _run_code_node(workflow, matching[0], state)
        self.assertEqual("not_reached", state["close_outcome"])
        self.assertEqual("callback", state["close_reason"])

    def test_changed_openapi_fields_enums_and_host_reach_tools(self) -> None:
        changed = copy.deepcopy(self.openapi)
        changed["servers"] = [{"url": "https://changed-bank.example.test/api"}]
        identity = changed["paths"]["/verify_identity"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        identity["properties"]["field"]["enum"] = ["postal_code", "security_word"]
        transfer = changed["paths"]["/transfer_to_agent"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        transfer["properties"]["queue"]["enum"] = ["specialist", "review"]
        promise = changed["paths"]["/create_promise"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        promise["properties"]["campaign_code"] = {"type": "string", "enum": ["new_a", "new_b"]}
        promise["required"].append("campaign_code")
        custom_brief = (
            "Verify the named customer in two steps: ask postal code, then security word. "
            "The API field codes are postal_code and security_word. Never fetch the account "
            "until both pass. Transfer to the specialist queue when requested. A promise "
            "requires campaign_code new_a. All dates are based on call variable now."
        )
        policy = {"global_rules": custom_brief, "opening": "", "identity": "",
                  "verified_call": "", "outcomes": {}, "tool_policy": {},
                  "raw_brief": custom_brief, "constraints": {"extraction_status": "source_only"}}
        with self.subTest("source-only policy still follows changed schema"):
            workflow = build_workflow(policy, changed)
            self.assertEqual([], validate_workflow(workflow, changed))

        for name, field, expected_enum in (
            ("transfer_to_agent", "queue", ["specialist", "review"]),
            ("create_promise", "campaign_code", ["new_a", "new_b"]),
        ):
            found = _named_tool(workflow, name)
            self.assertTrue(found, name)
            for tool in found:
                props = tool["function"]["parameters"]["properties"]
                self.assertEqual("https://changed-bank.example.test/api/" + name, props["url"]["default"])
                body = props["body"]
                self.assertIn(field, body["properties"])
                self.assertEqual(expected_enum, body["properties"][field]["enum"])
                if name == "create_promise":
                    self.assertIn(field, body["required"])
        verify_fields = [tool["function"]["parameters"]["properties"]["body"]["properties"]["field"]
                         for tool in _named_tool(workflow, "verify_identity")]
        self.assertTrue(verify_fields)
        self.assertEqual({"postal_code", "security_word"},
                         {item.get("default") for item in verify_fields})

    def test_no_public_calendar_is_baked_into_unrelated_brief(self) -> None:
        custom = (
            "Call now is supplied as a variable. First identify the named customer and verify "
            "both security questions before get_account. Business days exclude 2031-04-17. "
            "Use the seven bank API tools and close every call."
        )
        policy = {
            "global_rules": custom, "opening": "", "identity": "",
            "verified_call": "", "outcomes": {}, "tool_policy": {},
            "raw_brief": custom, "constraints": {"extraction_status": "source_only"},
        }
        workflow = build_workflow(policy, self.openapi)
        text = _text(workflow)
        self.assertIn("2031-04-17", text)
        self.assertNotIn("2026-10-26", text)
        self.assertNotIn("2026-10-14", text)
        self.assertRegex(text, r"\bnow\b")

    def test_changed_deadline_bands_come_from_the_supplied_brief(self) -> None:
        custom = (
            "Use the call variable now. Business days are Monday through Friday and there "
            "are no bank holidays. For a product 1 to 5 days late, the deadline is the third "
            "business day after today, with one persuasion attempt. For a product 6 to 45 "
            "days late, the deadline is the ninth business day after today, with two attempts."
        )
        policy = {"global_rules": custom, "opening": "", "identity": "",
                  "verified_call": "", "outcomes": {}, "tool_policy": {},
                  "raw_brief": custom, "constraints": {"extraction_status": "source_only"}}
        workflow = build_workflow(policy, self.openapi)
        prepared, _ = _prepared_products(workflow, [
            {"product_id": "p1", "type": "credit_card", "past_due": 100,
             "days_late": 4, "minimum_payment": 30},
            {"product_id": "p2", "type": "personal_loan", "past_due": 200,
             "days_late": 8, "instalment_amount": 50},
        ], "2026-10-05T10:00")
        self.assertEqual(["2026-10-08", "2026-10-16"],
                         [item["deadline"] for item in prepared])
        self.assertEqual([1, 2], [item["attempts"] for item in prepared])

    def test_changed_close_outcome_enum_controls_recorded_promise(self) -> None:
        changed = copy.deepcopy(self.openapi)
        close_schema = changed["paths"]["/close_call"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        close_schema["properties"]["outcome"]["enum"] = [
            "pledged", "paid", "transferred", "no_promise", "not_verified", "not_reached"]
        custom = "After a recorded promise, close_call must use outcome pledged."
        policy = {"global_rules": custom, "opening": "", "identity": "",
                  "verified_call": "", "outcomes": {"pledged": {"reason": None}},
                  "tool_policy": {"close_call": custom}, "raw_brief": custom,
                  "constraints": {"extraction_status": "source_only"}}
        workflow = build_workflow(policy, changed)
        state = {"product_json": json.dumps([{"product_id": "p1"}]),
                 "recorded_ids": '["p1"]', "note_reasons": "{}"}
        _run_code_node(workflow, "finalize", state)
        self.assertEqual("pledged", state["close_outcome"])

    def test_changed_reason_codes_follow_structured_policy_and_api(self) -> None:
        """A hidden brief may rename the bank's non-payment reason codes."""
        changed = copy.deepcopy(self.openapi)
        note_schema = changed["paths"]["/add_note"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        note_schema["properties"]["reason"]["enum"] = [
            "declined", "vulnerable", "contested", "settled", "write_failed"]
        close_schema = changed["paths"]["/close_call"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        close_schema["properties"]["outcome"]["enum"] = [
            "pledged", "settled", "transferred", "unresolved", "not_verified", "not_reached"]
        close_schema["properties"]["reason"]["enum"] = [
            "declined", "vulnerable", "contested", "write_failed", "human", "support",
            "restructuring", "failed", "callback", "no_callback", "wrong_number", "deceased", "unclear"]
        source = (
            "An accepted payment promise closes as pledged. If no product has a promise, "
            "close as unresolved using the first non-settled product reason. A flat refusal "
            "uses declined; a debt dispute uses contested. A payment already made uses "
            "settled. Record one add_note reason per unresolved product."
        )
        policy = {
            "global_rules": source, "opening": "", "identity": "", "verified_call": source,
            "outcomes": {
                "pledged": {"when": "at least one promise recorded", "reasons": []},
                "settled": {"when": "all products already paid", "reasons": []},
                "unresolved": {"when": "none recorded", "reasons": ["declined", "vulnerable", "contested", "write_failed"]},
            },
            "tool_policy": {"add_note": "Use changed reason codes from the API and policy.",
                            "close_call": "Use the changed outcome and reason codes."},
            "constraints": {"outcome_precedence": "pledged wins over unresolved"},
            "raw_brief": source,
        }
        workflow = build_workflow(policy, changed)
        self.assertEqual([], validate_workflow(workflow, changed))
        note = _named_tool(workflow, "add_note")[0]
        note_codes = note["function"]["parameters"]["properties"]["body"]["properties"]["reason"]["enum"]
        self.assertEqual(note_schema["properties"]["reason"]["enum"], note_codes)

        # Simulate a multi-product caller who promises the second product but
        # disputes the first. The bank's changed outcome and reason codes must
        # appear in the computed close state, with the promise taking precedence.
        mixed = {"product_json": json.dumps([{"product_id": "p1"}, {"product_id": "p2"}]),
                 "recorded_ids": '["p2"]', "note_reasons": '{"p1":"contested"}'}
        _run_code_node(workflow, "finalize", mixed)
        self.assertEqual("pledged", mixed["close_outcome"])
        self.assertEqual("", mixed["close_reason"])

        refused = {"product_json": json.dumps([{"product_id": "p1"}, {"product_id": "p2"}]),
                   "recorded_ids": "[]", "note_reasons": '{"p1":"settled","p2":"declined"}'}
        _run_code_node(workflow, "finalize", refused)
        self.assertEqual("unresolved", refused["close_outcome"])
        self.assertEqual("declined", refused["close_reason"])

        single = {"product_json": json.dumps([{"product_id": "p1"}]),
                  "recorded_ids": "[]", "note_reasons": "{}",
                  "single_product_reason": "contested"}
        _run_code_node(workflow, "finalize", single)
        self.assertEqual("unresolved", single["close_outcome"])
        self.assertEqual("contested", single["close_reason"])

    def test_source_only_fallback_preserves_later_memo_precedence(self) -> None:
        """A raw memo later in the input must override an earlier code table."""
        base = (
            "# Bank policy extract\n"
            "Use the call variable now. Verify identity before get_account.\n"
            "For a flat refusal on a product, add_note reason refused. With no promise, "
            "close_call outcome no_promise and reason refused.\n"
        )
        memo = (
            "# Later operations memo — overrides the extract above\n"
            "Effective immediately, replace the refusal code refused with declined for "
            "both add_note and close_call. Replace outcome no_promise with unresolved. "
            "The earlier refusal codes must not be sent to the bank API.\n"
        )
        changed = copy.deepcopy(self.openapi)
        note_schema = changed["paths"]["/add_note"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        note_schema["properties"]["reason"]["enum"] = ["declined", "hardship", "dispute", "paid", "service_error"]
        close_schema = changed["paths"]["/close_call"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        close_schema["properties"]["outcome"]["enum"] = [
            "promise", "paid", "transferred", "unresolved", "not_verified", "not_reached"]
        close_schema["properties"]["reason"]["enum"] = [
            "declined", "hardship", "dispute", "service_error", "human", "support",
            "restructuring", "failed", "callback", "no_callback", "wrong_number", "deceased", "unclear"]
        with tempfile.TemporaryDirectory(prefix="btb-hidden-policy-") as directory:
            first = Path(directory) / "policy.md"
            second = Path(directory) / "memo.md"
            api = Path(directory) / "openapi.json"
            first.write_text(base, encoding="utf-8")
            second.write_text(memo, encoding="utf-8")
            api.write_text(json.dumps(changed), encoding="utf-8")
            policy = compile_policy([str(first), str(second)], str(api), None)

        self.assertEqual("source_only", policy["constraints"]["extraction_status"])
        self.assertLess(policy["raw_brief"].index("SOURCE: policy.md"),
                        policy["raw_brief"].index("SOURCE: memo.md"))
        self.assertIn("replace the refusal code refused with declined", policy["raw_brief"])
        workflow = build_workflow(policy, changed)
        self.assertEqual([], validate_workflow(workflow, changed))
        state = {"product_json": json.dumps([{"product_id": "p1"}]),
                 "recorded_ids": "[]", "note_reasons": "{}",
                 "single_product_reason": "declined"}
        _run_code_node(workflow, "finalize", state)
        self.assertEqual("unresolved", state["close_outcome"])
        self.assertEqual("declined", state["close_reason"])


if __name__ == "__main__":
    unittest.main()

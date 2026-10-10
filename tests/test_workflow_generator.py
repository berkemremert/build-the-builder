"""Offline acceptance checks for workflows built from bank source files.

These checks cover properties the Freya simulator cannot exercise offline. The
official `tools/run_cases.py` remains the authority for conversational scoring.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
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
        opening = _text(first.get("on_enter", {})) + " " + str(first.get("task_message", ""))
        self.assertRegex(opening.lower(), r"digital assistant|freya bank")
        self.assertRegex(opening.lower(), r"recorded|recording")
        spoken = first.get("on_enter", {}).get("text", "")
        if spoken:
            self.assertNotRegex(spoken.lower(), r"collections|debt|payment|credit card|loan|past.due")
        self.assertFalse(_named_tool({"tools": first.get("tools", []), "nodes": {}, "global_nodes": {}}, "get_account"))
        self.assertTrue(_named_tool(workflow, "verify_identity"))
        self.assertTrue(_named_tool(workflow, "get_account"))

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
        failed = {"data": {"ok": False}}
        exec(promise_hook, {"vars": state, "result": failed})
        self.assertEqual([], json.loads(state["recorded_ids"]))
        self.assertEqual(1, json.loads(state["promise_attempts"])["p1"])
        self.assertIn("not recorded", failed["data"].get("what_to_do_next", "").lower())

        succeeded = {"data": {"ok": True}}
        exec(promise_hook, {"vars": state, "result": succeeded})
        self.assertEqual(["p1"], json.loads(state["recorded_ids"]))
        self.assertIn("recorded", succeeded["data"].get("what_to_do_next", "").lower())

        noted = {"data": {"ok": True}}
        exec(note_hook, {"vars": state, "result": noted})
        self.assertEqual("refused", json.loads(state["note_reasons"])["p2"])

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


if __name__ == "__main__":
    unittest.main()

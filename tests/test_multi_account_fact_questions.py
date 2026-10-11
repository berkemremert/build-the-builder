"""Offline checks for verified product questions before the Bank B all-today answer."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from policy_compiler import compile_policy  # noqa: E402
from workflow_generator import build_workflow  # noqa: E402


def run_code(workflow: dict, name: str, state: dict) -> None:
    node = workflow["nodes"][name]
    code = node["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"]
    exec(code, {"vars": state})


def route(node: dict, state: dict) -> str | None:
    def matches(condition: dict) -> bool:
        if "conditions" in condition:
            values = [matches(item) for item in condition["conditions"]]
            return all(values) if condition.get("logic") == "and" else any(values)
        operator = condition.get("operator")
        if operator == "always":
            return True
        if operator == "equals":
            return str(state.get(condition.get("variable"), "")) == str(condition.get("operand"))
        if operator == "greater_than":
            return float(state.get(condition.get("variable"), 0)) > float(condition.get("operand"))
        return False

    return next((item["target_node"] for item in node.get("transitions", [])
                 if matches(item["condition"])), None)


class MultiAccountFactQuestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        api_path = ROOT / "tools" / "openapi.json"
        api = json.loads(api_path.read_text())
        policy = compile_policy([str(ROOT / "briefs" / "brief-a.md"),
                                 str(ROOT / "briefs" / "brief-b.md")], str(api_path), None)
        cls.workflow = build_workflow(policy, api)

    def prepared(self, case_id: str) -> dict:
        case = next(item for item in json.loads((ROOT / "long-call-stress.json").read_text())
                    if item["id"] == case_id)
        state = {"now": case["variables"]["now"], "account_products": [
            dict(item, product_id=f"p{index}") for index, item in enumerate(case["products"], 1)
        ]}
        run_code(self.workflow, "prepare_account", state)
        self.assertEqual("3", state["product_count"])
        return state

    def ask_fact(self, state: dict, from_node: str, kind: str, product_id: str, expected: str) -> None:
        state.update({"multi_account_fact_question": "yes", "multi_account_fact_kind": kind,
                      "multi_account_fact_product_id": product_id, "all_today_status": "other",
                      "pending_note_reasons_json": "{}"})
        self.assertEqual("multi_account_fact_answer", route(self.workflow["nodes"][from_node], state))
        run_code(self.workflow, "multi_account_fact_answer", state)
        self.assertEqual(expected, state["multi_account_fact_answer"])
        self.assertEqual("no", state["multi_account_fact_question"])
        self.assertEqual("Can you pay all of these products today?",
                         self.workflow["nodes"]["multi_account_followup"]["on_enter"]["text"].split("}} ")[1])
        self.assertEqual("{}", state["pending_note_reasons_json"])

    def test_long04_repeated_product_questions_preserve_first_offer(self) -> None:
        state = self.prepared("LONG-04")
        self.ask_fact(state, "account_discussion", "list", "",
                      "The products, in order, are credit card ending 2288, personal loan, car loan.")
        self.ask_fact(state, "multi_account_followup", "card_suffix", "p1",
                      "The product is credit card ending 2288.")
        self.ask_fact(state, "multi_account_followup", "past_due", "p2",
                      "The past-due amount on the personal loan is 640.00.")
        self.ask_fact(state, "multi_account_followup", "past_due", "p3",
                      "The past-due amount on the car loan is 75.00.")
        state.update({"multi_account_fact_question": "no", "all_today_status": "no",
                      "multi_full_too_much_ids_json": "[]", "proposed_terms_json": "[]",
                      "pending_note_reasons_json": '{"p1":"refused","p2":"refused","p3":"refused"}'})
        self.assertEqual("multi_advance", route(self.workflow["nodes"]["multi_account_followup"], state))
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual(("p1", "1", "offer"),
                         (state["multi_focus_product_id"], state["multi_offer_stage"], state["multi_next"]))
        self.assertEqual({}, json.loads(state["multi_notes_json"]))
        self.assertEqual({}, json.loads(state["multi_terms_json"]))

    def test_long05_card_amount_and_days_are_account_facts(self) -> None:
        state = self.prepared("LONG-05")
        self.ask_fact(state, "account_discussion", "list", "",
                      "The products, in order, are credit card ending 9015, personal loan, car loan.")
        self.ask_fact(state, "multi_account_followup", "card_suffix", "p1",
                      "The product is credit card ending 9015.")
        self.ask_fact(state, "multi_account_followup", "past_due", "p1",
                      "The past-due amount on the credit card ending 9015 is 510.00.")
        self.ask_fact(state, "multi_account_followup", "days_late", "p1",
                      "The credit card ending 9015 is 16 days late.")
        state.update({"multi_account_fact_question": "no", "all_today_status": "no",
                      "multi_full_too_much_ids_json": "[]", "proposed_terms_json": "[]",
                      "pending_note_reasons_json": '{"p1":"refused","p2":"refused","p3":"refused"}'})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual("offer", state["multi_next"])
        self.assertEqual("1", state["multi_offer_stage"])
        self.assertEqual({}, json.loads(state["multi_notes_json"]))

    def test_ambiguous_product_question_does_not_invent_facts(self) -> None:
        state = self.prepared("LONG-05")
        self.ask_fact(state, "account_discussion", "past_due", "", "Which product do you mean?")
        self.assertEqual([], self.workflow["nodes"]["multi_account_followup"].get("tools", []))
        self.assertGreaterEqual(self.workflow["nodes"]["account_discussion"]["max_node_turns"], 6)

    def test_product_question_route_precedes_premature_refusal_classification(self) -> None:
        state = self.prepared("LONG-04")
        state.update({"multi_account_fact_question": "yes", "multi_account_fact_kind": "list",
                      "discussion_action": "done_no_promise", "all_today_status": "no",
                      "pending_note_reasons_json": '{"p1":"refused","p2":"refused","p3":"refused"}'})
        self.assertEqual("multi_account_fact_answer",
                         route(self.workflow["nodes"]["account_discussion"], state))
        run_code(self.workflow, "multi_account_fact_answer", state)
        self.assertNotIn("multi_terms_json", state)
        self.assertNotIn("multi_notes_json", state)


if __name__ == "__main__":
    unittest.main()

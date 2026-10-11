"""Offline regressions from the Bank B HARD-07 v8 call trace."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from policy_compiler import compile_policy  # noqa: E402
from workflow_generator import build_workflow  # noqa: E402


def run_node(workflow: dict, name: str, state: dict) -> None:
    code = (workflow["nodes"][name]["pre_actions"][0]["function"]
            ["parameters"]["properties"]["code"]["default"])
    exec(code, {"vars": state})


class Hard07ConfirmationRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        api_path = ROOT / "tools" / "openapi.json"
        api = json.loads(api_path.read_text())
        policy = compile_policy([str(ROOT / "briefs" / "brief-a.md"),
                                 str(ROOT / "briefs" / "brief-b.md")], str(api_path), None)
        cls.workflow = build_workflow(policy, api)
        cls.products = [
            {"product_id": "p1", "label": "credit card ending 9015", "floor": 153,
             "past_due": 510, "deadline": "2026-10-23"},
            {"product_id": "p2", "label": "personal loan", "floor": 440,
             "past_due": 1320, "deadline": "2026-10-23"},
            {"product_id": "p3", "label": "car loan", "floor": 46,
             "past_due": 140, "deadline": "2026-10-30"},
        ]

    def _state(self) -> dict:
        return {"now": "2026-10-14T11:00", "product_json": json.dumps(self.products),
                "multi_terms_json": json.dumps({"p1": {
                    "product_id": "p1", "amount": 510,
                    "date": "tomorrow, fifteen October"}}),
                "multi_notes_json": '{"p2":"paid","p3":"dispute"}',
                "multi_correction_attempts": "0"}

    def test_concordant_spoken_date_reaches_validated_readback(self) -> None:
        state = self._state()
        state["proposed_terms_json"] = json.dumps(list(json.loads(state["multi_terms_json"]).values()))
        run_node(self.workflow, "prepare_confirmation", state)
        self.assertEqual("ready", state["proposal_validation_state"], state.get("proposal_error"))
        self.assertEqual([{"product_id": "p1", "amount": 510.0, "date": "2026-10-15"}],
                         json.loads(state["readback_terms_json"]))
        self.assertIn("credit card ending 9015: 510.00 on 2026-10-15", state["confirmation_readback"])

    def test_positional_correction_array_reaches_fresh_readback(self) -> None:
        state = self._state()
        state["multi_corrected_terms_json"] = json.dumps(
            ["credit card", "five hundred and ten", "fifteen October"])
        run_node(self.workflow, "multi_revise", state)
        self.assertEqual("confirm", state["multi_next"])
        self.assertEqual("fifteen October", json.loads(state["multi_terms_json"])["p1"]["date"])
        run_node(self.workflow, "prepare_confirmation", state)
        self.assertEqual("ready", state["proposal_validation_state"], state.get("proposal_error"))
        self.assertEqual([{"product_id": "p1", "amount": 510.0, "date": "2026-10-15"}],
                         json.loads(state["readback_terms_json"]))

    def test_incomplete_positional_correction_does_not_guess(self) -> None:
        state = self._state()
        original = state["multi_terms_json"]
        state["multi_corrected_terms_json"] = json.dumps(["credit", "510"])
        run_node(self.workflow, "multi_revise", state)
        self.assertEqual("clarify", state["multi_next"])
        self.assertEqual(original, state["multi_terms_json"])

    def test_ordinary_object_correction_still_validates(self) -> None:
        state = self._state()
        state["multi_corrected_terms_json"] = json.dumps([{
            "product_id": "p1", "amount": "five hundred and ten", "date": "fifteen October",
        }])
        run_node(self.workflow, "multi_revise", state)
        self.assertEqual("confirm", state["multi_next"])
        self.assertEqual(510.0, json.loads(state["multi_terms_json"])["p1"]["amount"])

    def test_ambiguous_or_out_of_range_positional_correction_clarifies(self) -> None:
        for raw in (["credit card", "five hundred and eleven", "fifteen October"],
                    ["credit card", "five hundred and", "fifteen October"],
                    ["credit card", "five hundred and ten", "tomorrow, sixteen October"]):
            with self.subTest(raw=raw):
                state = self._state()
                original = state["multi_terms_json"]
                state["multi_corrected_terms_json"] = json.dumps(raw)
                run_node(self.workflow, "multi_revise", state)
                self.assertEqual("clarify", state["multi_next"])
                self.assertEqual(original, state["multi_terms_json"])

    def test_complete_three_product_correction_after_fragment(self) -> None:
        state = self._state()
        state["product_json"] = json.dumps([
            {"product_id": "p1", "label": "credit card", "floor": 50,
             "past_due": 220, "deadline": "2026-10-30"},
            {"product_id": "p2", "label": "personal loan", "floor": 200,
             "past_due": 640, "deadline": "2026-10-23"},
            {"product_id": "p3", "label": "car loan", "floor": 25,
             "past_due": 75, "deadline": "2026-10-30"},
        ])
        state["multi_terms_json"] = "{}"
        state["multi_corrected_terms_json"] = json.dumps(["credit card", "220"])
        run_node(self.workflow, "multi_revise", state)
        self.assertEqual("clarify", state["multi_next"])
        self.assertEqual({}, json.loads(state["multi_terms_json"]))
        state["multi_corrected_terms_json"] = json.dumps([
            {"product_id": "p1", "amount": 220, "date": "Friday 16 October"},
            {"product_id": "p2", "amount": 640, "date": "Tuesday 20 October"},
            {"product_id": "p3", "amount": 75, "date": "Thursday 22 October"},
        ])
        run_node(self.workflow, "multi_revise", state)
        self.assertEqual("confirm", state["multi_next"])
        run_node(self.workflow, "prepare_confirmation", state)
        self.assertEqual("ready", state["proposal_validation_state"], state.get("proposal_error"))
        self.assertEqual(3, len(json.loads(state["readback_terms_json"])))


if __name__ == "__main__":
    unittest.main()

"""Offline amount validation in generated Freya code nodes."""

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


class GeneratedPaymentAmountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        api_path = ROOT / "tools" / "openapi.json"
        api = json.loads(api_path.read_text())
        policy = compile_policy([str(ROOT / "briefs" / "brief-a.md"),
                                 str(ROOT / "briefs" / "brief-b.md")], str(api_path), None)
        cls.workflow = build_workflow(policy, api)
        cls.product = {"product_id": "p1", "label": "credit card", "floor": 50,
                       "past_due": 500, "deadline": "2026-10-30"}

    def _state(self, amount: object) -> dict:
        return {"now": "2026-10-14T11:00", "product_json": json.dumps([self.product]),
                "proposed_terms_json": json.dumps([{"product_id": "p1", "amount": amount,
                                                     "date": "2026-10-15"}])}

    def test_prepare_confirmation_accepts_spoken_and_corrected_amounts(self) -> None:
        for amount in ("four hundred and fifty-five",
                       "four hundred, sorry, I mean four hundred and fifty-five", 455, "£455.00"):
            with self.subTest(amount=amount):
                state = self._state(amount)
                run_node(self.workflow, "prepare_confirmation", state)
                self.assertEqual("ready", state["proposal_validation_state"], state.get("proposal_error"))
                self.assertEqual(455.0, json.loads(state["readback_terms_json"])[0]["amount"])

    def test_prepare_confirmation_rejects_unfinished_ambiguous_and_out_of_range(self) -> None:
        for amount in ("four hundred and", "four hundred, sorry, I mean",
                       "four hundred or five hundred", "five hundred and one", 501,
                       "four hundred, sorry, I mean five hundred and one",
                       "forty-nine", "455.555"):
            with self.subTest(amount=amount):
                state = self._state(amount)
                run_node(self.workflow, "prepare_confirmation", state)
                self.assertEqual("invalid", state["proposal_validation_state"])
                self.assertEqual([], json.loads(state["readback_terms_json"]))

    def test_single_proposal_gate_parses_spoken_amount_before_floor_check(self) -> None:
        below = self._state("forty-nine")
        run_node(self.workflow, "single_proposal_gate", below)
        self.assertEqual("below_floor", below["single_proposal_gate_state"])
        self.assertIn("49.00", below["single_floor_offer_text"])
        for amount in ("four hundred and", "four hundred or forty-nine"):
            with self.subTest(amount=amount):
                state = self._state(amount)
                run_node(self.workflow, "single_proposal_gate", state)
                self.assertEqual("normal", state["single_proposal_gate_state"])

    def test_multi_advance_accepts_valid_correction_and_rejects_out_of_range(self) -> None:
        state = {"now": "2026-10-14T11:00", "product_json": json.dumps([
            self.product, {"product_id": "p2", "label": "loan", "floor": 20,
                           "past_due": 200, "deadline": "2026-10-30"}]),
                 "all_today_status": "no"}
        run_node(self.workflow, "multi_advance", state)
        self.assertEqual("p1", state["multi_focus_product_id"])
        state.update({"multi_offer_action": "proposal", "multi_reply_terms_json": json.dumps([{
            "product_id": "p1", "amount": "four hundred, sorry, I mean four hundred and fifty-five",
            "date": "2026-10-15"}])})
        run_node(self.workflow, "multi_advance", state)
        self.assertEqual(455.0, json.loads(state["multi_terms_json"])["p1"]["amount"])

        for amount in ("five hundred and one", "four hundred and", "four hundred or five hundred"):
            with self.subTest(amount=amount):
                candidate = {"now": state["now"], "product_json": state["product_json"],
                             "all_today_status": "no"}
                run_node(self.workflow, "multi_advance", candidate)
                candidate.update({"multi_offer_action": "proposal", "multi_reply_terms_json": json.dumps([{
                    "product_id": "p1", "amount": amount, "date": "2026-10-15"}])})
                run_node(self.workflow, "multi_advance", candidate)
                self.assertNotIn("p1", json.loads(candidate["multi_terms_json"]))


if __name__ == "__main__":
    unittest.main()

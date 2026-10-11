"""Offline regressions for a caller day in the first multi-product answer."""

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
    code = workflow["nodes"][name]["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"]
    exec(code, {"vars": state})


class InitialMultiDateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        api_path = ROOT / "tools" / "openapi.json"
        api = json.loads(api_path.read_text())
        policy = compile_policy([str(ROOT / "briefs" / "brief-a.md"),
                                 str(ROOT / "briefs" / "brief-b.md")], str(api_path), None)
        cls.workflow = build_workflow(policy, api)

    def prepared(self, *, renamed: bool = False) -> dict:
        case = next(item for item in json.loads((ROOT / "long-call-stress.json").read_text())
                    if item["id"] == "LONG-05")
        state = {"now": case["variables"]["now"], "account_products": [
            dict(item, product_id=f"p{index}") for index, item in enumerate(case["products"], 1)
        ]}
        run_code(self.workflow, "prepare_account", state)
        if renamed:
            products = json.loads(state["product_json"])
            for item, pid, label in zip(products, ("acct-A", "acct-B", "acct-C"),
                                         ("retail account", "education account", "equipment account")):
                item["product_id"] = pid
                item["label"] = label
            state["product_json"] = json.dumps(products)
        state.update({"all_today_status": "no", "pending_note_reasons_json": "{}"})
        return state

    def test_raw_initial_day_uses_only_remaining_product_and_keeps_refusals_pending(self) -> None:
        state = self.prepared(renamed=True)
        state.update({"multi_initial_date_spoken": "yes", "proposed_date_words": "tomorrow",
                      "pending_note_reasons_json": '{"acct-B":"refused","acct-C":"refused"}',
                      "proposed_terms_json": ""})
        run_code(self.workflow, "multi_all_today_gate", state)
        self.assertEqual("advance", state["multi_all_today_next"])
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual(("acct-A", "clarify"),
                         (state["multi_focus_product_id"], state["multi_next"]))
        self.assertEqual({"acct-A": {"product_id": "acct-A", "date": "tomorrow"}},
                         json.loads(state["multi_partial_terms_json"]))
        self.assertEqual({}, json.loads(state["multi_notes_json"]))
        self.assertEqual({}, json.loads(state["multi_terms_json"]))
        self.assertIn("510.00", state["multi_clarify_prompt"])
        self.assertIn("tomorrow", state["multi_clarify_prompt"])

        state.update({"multi_offer_action": "accepted", "multi_fixed_offer_accepted": "yes",
                      "multi_reply_date_spoken": "no"})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual(("acct-B", "offer"),
                         (state["multi_focus_product_id"], state["multi_next"]))
        self.assertEqual({"product_id": "acct-A", "amount": 510.0, "date": "tomorrow"},
                         json.loads(state["multi_terms_json"])["acct-A"])
        self.assertEqual({}, json.loads(state["multi_notes_json"]))

        for expected_focus, expected_stage in (("acct-B", "2"), ("acct-C", "1"), ("acct-C", "1")):
            state["multi_offer_action"] = "refused"
            run_code(self.workflow, "multi_advance", state)
            self.assertEqual(expected_focus, state["multi_focus_product_id"])
            self.assertEqual(expected_stage, state["multi_offer_stage"])
        self.assertEqual("confirm", state["multi_next"])
        self.assertEqual({"acct-B": "refused", "acct-C": "refused"},
                         json.loads(state["multi_notes_json"]))
        self.assertEqual("tomorrow", json.loads(state["proposed_terms_json"])[0]["date"])

    def test_ambiguous_day_asks_for_product_before_any_offer(self) -> None:
        state = self.prepared(renamed=True)
        state.update({"multi_initial_date_spoken": "yes", "proposed_date_words": "tomorrow"})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual("date_identify", state["multi_next"])
        self.assertEqual({}, json.loads(state["multi_terms_json"]))
        self.assertEqual({}, json.loads(state["multi_partial_terms_json"]))
        self.assertEqual("tomorrow", state["multi_initial_date_words"])
        self.assertIn("multi_initial_date_identify",
                      {item["target_node"] for item in self.workflow["nodes"]["multi_advance"]["transitions"]})

        state["multi_initial_date_product_id"] = "equipment account"
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual("offer", state["multi_next"])
        self.assertEqual("acct-A", state["multi_focus_product_id"])
        self.assertEqual({"acct-C": {"product_id": "acct-C", "date": "tomorrow"}},
                         json.loads(state["multi_partial_terms_json"]))

    def test_identified_day_keeps_captured_amount_with_multiple_candidates(self) -> None:
        state = self.prepared()
        state.update({"multi_initial_date_spoken": "yes", "proposed_date_words": "tomorrow", "proposed_terms_json": json.dumps([
            {"product_id": "p1", "amount": 300},
            {"product_id": "p2", "amount": 500},
        ])})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual("date_identify", state["multi_next"])
        self.assertEqual({"p1": {"product_id": "p1", "amount": 300.0},
                          "p2": {"product_id": "p2", "amount": 500.0}},
                         json.loads(state["multi_partial_terms_json"]))

        state["multi_initial_date_product_id"] = "p1"
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual({"p1": {"product_id": "p1", "amount": 300.0, "date": "tomorrow"}},
                         json.loads(state["multi_terms_json"]))
        self.assertEqual({"p2": {"product_id": "p2", "amount": 500.0}},
                         json.loads(state["multi_partial_terms_json"]))
        self.assertEqual(("p2", "clarify"),
                         (state["multi_focus_product_id"], state["multi_next"]))
        self.assertNotIn("510.00", state["multi_clarify_prompt"])

    def test_invalid_identified_day_keeps_captured_amount_for_correction(self) -> None:
        state = self.prepared()
        state.update({"multi_initial_date_spoken": "yes", "proposed_date_words": "Saturday", "proposed_terms_json": json.dumps([
            {"product_id": "p1", "amount": 300},
            {"product_id": "p2", "amount": 500},
        ])})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual("date_identify", state["multi_next"])

        state["multi_initial_date_product_id"] = "p1"
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual({"product_id": "p1", "amount": 300.0, "invalid_date": "Saturday"},
                         json.loads(state["multi_partial_terms_json"])["p1"])
        self.assertEqual("clarify", state["multi_next"])
        self.assertIn("exact business day", state["multi_clarify_prompt"])
        self.assertNotIn("Saturday", state["multi_clarify_prompt"])
        self.assertNotIn("510.00", state["multi_clarify_prompt"])

        state.update({"multi_offer_action": "proposal", "multi_reply_date_spoken": "yes",
                      "multi_reply_terms_json": '[{"product_id":"p1","date":"19 October"}]'})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual({"product_id": "p1", "amount": 300.0, "date": "19 October"},
                         json.loads(state["multi_terms_json"])["p1"])

    def test_explicit_product_with_invalid_day_asks_for_valid_day(self) -> None:
        state = self.prepared(renamed=True)
        state.update({"multi_initial_date_spoken": "yes", "proposed_date_words": "Saturday", "multi_initial_date_product_id": "acct-A"})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual("clarify", state["multi_next"])
        self.assertEqual({"product_id": "acct-A", "invalid_date": "Saturday"},
                         json.loads(state["multi_partial_terms_json"])["acct-A"])
        self.assertIn("exact business day", state["multi_clarify_prompt"])
        self.assertNotIn("Saturday", state["multi_clarify_prompt"])
        self.assertNotIn("510.00", state["multi_clarify_prompt"])

    def test_shared_day_still_applies_to_all_products(self) -> None:
        state = self.prepared()
        state.update({"multi_initial_date_spoken": "yes", "multi_common_date_words": "tomorrow"})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual("confirm", state["multi_next"])
        self.assertEqual({"p1", "p2", "p3"}, set(json.loads(state["multi_terms_json"])))


if __name__ == "__main__":
    unittest.main()

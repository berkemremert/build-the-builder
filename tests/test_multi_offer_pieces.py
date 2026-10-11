"""Offline state regressions for fragmented Bank B product replies."""

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


class MultiOfferPiecesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        api_path = ROOT / "tools" / "openapi.json"
        api = json.loads(api_path.read_text())
        policy = compile_policy([str(ROOT / "briefs" / "brief-a.md"),
                                 str(ROOT / "briefs" / "brief-b.md")], str(api_path), None)
        cls.workflow = build_workflow(policy, api)

    def prepared(self, now: str | None = None) -> dict:
        case = next(item for item in json.loads((ROOT / "long-call-stress.json").read_text())
                    if item["id"] == "LONG-05")
        state = {"now": now or case["variables"]["now"], "account_products": [
            dict(item, product_id=f"p{index}") for index, item in enumerate(case["products"], 1)
        ]}
        run_code(self.workflow, "prepare_account", state)
        state.update({"all_today_status": "no", "pending_note_reasons_json": "{}"})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual("p1", state["multi_focus_product_id"])
        return state

    def test_amount_then_date_stays_on_card_and_fact_questions_do_not_advance(self) -> None:
        state = self.prepared()
        run_code(self.workflow, "multi_select_offer", state)
        self.assertEqual("yes", state["multi_fixed_offer"])

        # Caller: "I can pay the full five hundred and ten for the credit card."
        # Even if accepted is extracted, the fixed offer's DATE was not accepted.
        state.update({"multi_offer_action": "accepted", "multi_fixed_offer_accepted": "no",
                      "multi_reply_date_spoken": "no",
                      "multi_reply_terms_json": '[{"product_id":"p1","amount":"five hundred and ten",'
                                                '"date":"2026-10-16"}]'})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual(("p1", "1", "clarify"),
                         (state["multi_focus_product_id"], state["multi_offer_stage"], state["multi_next"]))
        self.assertEqual({}, json.loads(state["multi_terms_json"]))
        self.assertEqual(510.0, json.loads(state["multi_partial_terms_json"])["p1"]["amount"])
        self.assertIn("exact day", state["multi_clarify_prompt"].lower())

        # Caller: "Friday 16 October." This completes the CARD proposal.
        state.update({"multi_offer_action": "accepted", "multi_reply_date_spoken": "yes",
                      "multi_reply_terms_json": '[{"product_id":"p1","date":"Friday 16 October"}]'})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual("p2", state["multi_focus_product_id"])
        self.assertEqual("1", state["multi_offer_stage"])
        self.assertEqual({"product_id": "p1", "amount": 510.0, "date": "Friday 16 October"},
                         json.loads(state["multi_terms_json"])["p1"])

        # Caller: "Is 16 October before the card deadline?" This is a question,
        # even while the personal loan is the current focus.
        state.update({"multi_offer_action": "unclear", "multi_offer_fact_question": "yes",
                      "multi_offer_fact_kind": "deadline", "multi_offer_fact_product_id": "p1",
                      "multi_offer_fact_date_words": "16 October"})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual(("p2", "1", "question"),
                         (state["multi_focus_product_id"], state["multi_offer_stage"], state["multi_next"]))
        self.assertEqual({}, json.loads(state["multi_notes_json"]))
        run_code(self.workflow, "multi_offer_fact_answer", state)
        self.assertIn("on or before", state["multi_offer_fact_answer"])
        self.assertEqual("offer", state["multi_phase"])

        # Caller reports the current personal-loan overdue amount already paid.
        state.update({"multi_offer_action": "paid", "multi_reply_product_id": "p2"})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual("p3", state["multi_focus_product_id"])
        self.assertEqual({"p2": "paid"}, json.loads(state["multi_notes_json"]))

        # Caller: "Is 16 October before the" (unfinished factual question).
        state.update({"multi_offer_action": "unclear", "multi_offer_fact_question": "yes",
                      "multi_offer_fact_kind": "deadline", "multi_offer_fact_product_id": "",
                      "multi_offer_fact_date_words": "16 October"})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual(("p3", "1", "question"),
                         (state["multi_focus_product_id"], state["multi_offer_stage"], state["multi_next"]))
        self.assertNotIn("p3", json.loads(state["multi_notes_json"]))
        run_code(self.workflow, "multi_offer_fact_answer", state)
        self.assertEqual("Which product do you mean?", state["multi_offer_fact_answer"])

        state.update({"multi_offer_action": "dispute", "multi_reply_product_id": "p3"})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual("confirm", state["multi_next"])
        self.assertEqual({"p2": "paid", "p3": "dispute"}, json.loads(state["multi_notes_json"]))
        self.assertEqual(["p1"], [item["product_id"] for item in json.loads(state["proposed_terms_json"])])

    def test_paid_and_dispute_claims_bypass_correction_amount_parser(self) -> None:
        state = self.prepared()
        state.update({"multi_terms_json": '{"p1":{"product_id":"p1","amount":510,'
                                          '"date":"Friday 16 October"}}',
                      "multi_phase": "offer", "multi_focus_product_id": "p2",
                      "multi_offer_stage": "1"})
        correction = self.workflow["nodes"]["multi_correction"]
        targets = {item["target_node"] for item in correction["transitions"]}
        self.assertIn("multi_correction_claim", targets)

        # Caller: "I paid the current 1320 past due on the personal loan."
        state.update({"multi_correction_claim_action": "paid", "multi_correction_claim_product_id": "p2",
                      "multi_correction_amount_words": "1320"})
        run_code(self.workflow, "multi_correction_claim", state)
        self.assertEqual("advance", state["multi_next"])
        self.assertEqual("", state["multi_correction_amount_words"])
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual({"p2": "paid"}, json.loads(state["multi_notes_json"]))
        self.assertEqual("p3", state["multi_focus_product_id"])

        # Caller: "I never had that car loan; it is not mine."
        state.update({"multi_correction_claim_action": "dispute", "multi_correction_claim_product_id": "p3",
                      "multi_correction_amount_words": "140"})
        run_code(self.workflow, "multi_correction_claim", state)
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual("confirm", state["multi_next"])
        self.assertEqual({"p2": "paid", "p3": "dispute"}, json.loads(state["multi_notes_json"]))
        self.assertEqual(["p1"], [item["product_id"] for item in json.loads(state["proposed_terms_json"])])

    def test_genuine_nonanswer_still_uses_a_counted_attempt(self) -> None:
        state = self.prepared()
        state.update({"multi_offer_action": "unclear", "multi_offer_fact_question": "no"})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual(("p1", "2", "offer"),
                         (state["multi_focus_product_id"], state["multi_offer_stage"], state["multi_next"]))

    def test_clear_yes_to_fixed_amount_and_date_remains_accepted(self) -> None:
        state = self.prepared()
        run_code(self.workflow, "multi_select_offer", state)
        offered_day = state["multi_offer_date"]
        state.update({"multi_offer_action": "accepted", "multi_fixed_offer_accepted": "yes",
                      "multi_reply_date_spoken": "no", "multi_reply_terms_json": ""})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual("p2", state["multi_focus_product_id"])
        self.assertEqual(offered_day, json.loads(state["multi_terms_json"])["p1"]["date"])

    def test_missing_date_spoken_does_not_accept_copied_offer_date(self) -> None:
        state = self.prepared()
        run_code(self.workflow, "multi_select_offer", state)
        offered_day = state["multi_offer_date"]
        state.update({"multi_offer_action": "proposal", "multi_reply_terms_json": json.dumps([{
            "product_id": "p1", "amount": 510, "date": offered_day,
        }])})
        state.pop("multi_reply_date_spoken", None)
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual(("p1", "1", "clarify"),
                         (state["multi_focus_product_id"], state["multi_offer_stage"], state["multi_next"]))
        self.assertEqual({}, json.loads(state["multi_terms_json"]))
        self.assertEqual(510.0, json.loads(state["multi_partial_terms_json"])["p1"]["amount"])

    def test_common_date_keeps_previously_spoken_product_amount(self) -> None:
        state = self.prepared()
        # Caller: "I can pay 300 for the credit card."
        state.update({"multi_offer_action": "proposal", "multi_reply_date_spoken": "no",
                      "multi_reply_terms_json": '[{"product_id":"p1","amount":300}]'})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual("clarify", state["multi_next"])
        self.assertEqual(300.0, json.loads(state["multi_partial_terms_json"])["p1"]["amount"])

        # A copied common date is not evidence that the caller said it.
        state.update({"multi_offer_action": "proposal", "multi_reply_common_date_words": "Friday 16 October"})
        state.pop("multi_reply_date_spoken", None)
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual("clarify", state["multi_next"])
        self.assertEqual({}, json.loads(state["multi_terms_json"]))

        # Caller: "Friday 16 October for all of them."
        state.update({"multi_offer_action": "proposal", "multi_reply_date_spoken": "yes",
                      "multi_reply_common_date_words": "Friday 16 October"})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual("confirm", state["multi_next"])
        terms = json.loads(state["multi_terms_json"])
        self.assertEqual((300.0, 1320.0, 140.0),
                         (terms["p1"]["amount"], terms["p2"]["amount"], terms["p3"]["amount"]))
        self.assertEqual({}, json.loads(state["multi_partial_terms_json"]))

    def test_invalid_extracted_amount_retains_caller_date_for_full_amount_offer(self) -> None:
        state = self.prepared()
        # Caller: "I will pay the credit card tomorrow." The extractor's
        # amount field contains the PRODUCT LABEL, not a payment amount.
        state.update({"multi_offer_action": "proposal", "multi_reply_date_spoken": "yes",
                      "multi_reply_terms_json": '[{"product_id":"p1","amount":"credit card",'
                                                '"date":"tomorrow"}]'})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual(("p1", "1", "clarify"),
                         (state["multi_focus_product_id"], state["multi_offer_stage"], state["multi_next"]))
        self.assertEqual({}, json.loads(state["multi_terms_json"]))
        self.assertEqual({"product_id": "p1", "date": "tomorrow"},
                         json.loads(state["multi_partial_terms_json"])["p1"])
        self.assertIn("510.00", state["multi_clarify_prompt"])
        self.assertIn("tomorrow", state["multi_clarify_prompt"])
        self.assertEqual("no", state["multi_reply_date_spoken"])
        self.assertEqual("no", state["multi_fixed_offer_accepted"])
        self.assertNotIn("pending_promises_json", state)

        # The assistant proposes the full account amount on the retained day.
        # Only the caller's clear acceptance advances to combined confirmation.
        state.update({"multi_offer_action": "accepted", "multi_fixed_offer_accepted": "yes",
                      "multi_reply_date_spoken": "no", "multi_reply_terms_json": ""})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual("p2", state["multi_focus_product_id"])
        self.assertEqual({"product_id": "p1", "amount": 510.0, "date": "tomorrow"},
                         json.loads(state["multi_terms_json"])["p1"])
        self.assertNotIn("pending_promises_json", state)

    def test_date_slot_can_merge_a_later_valid_customer_amount(self) -> None:
        state = self.prepared()
        state.update({"multi_offer_action": "proposal", "multi_reply_date_spoken": "yes",
                      "multi_reply_terms_json": '[{"product_id":"p1","amount":"credit card",'
                                                '"date":"tomorrow"}]'})
        run_code(self.workflow, "multi_advance", state)
        state.update({"multi_offer_action": "proposal", "multi_reply_date_spoken": "no",
                      "multi_reply_terms_json": '[{"product_id":"p1","amount":400}]'})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual({"product_id": "p1", "amount": 400.0, "date": "tomorrow"},
                         json.loads(state["multi_terms_json"])["p1"])
        self.assertEqual("p2", state["multi_focus_product_id"])

    def test_invalid_holiday_day_is_not_offered_with_full_amount(self) -> None:
        state = self.prepared("2026-10-25T11:00")
        state.update({"multi_offer_action": "proposal", "multi_reply_date_spoken": "yes",
                      "multi_reply_terms_json": '[{"product_id":"p1","amount":"credit card",'
                                                '"date":"tomorrow"}]'})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual(("p1", "1", "clarify"),
                         (state["multi_focus_product_id"], state["multi_offer_stage"], state["multi_next"]))
        self.assertEqual({}, json.loads(state["multi_terms_json"]))
        self.assertEqual({"product_id": "p1", "invalid_date": "tomorrow"},
                         json.loads(state["multi_partial_terms_json"])["p1"])
        self.assertIn("exact business day", state["multi_clarify_prompt"])
        self.assertNotIn("tomorrow", state["multi_clarify_prompt"])
        self.assertNotIn("510.00", state["multi_clarify_prompt"])

        # Caller corrects the day; only then may the assistant propose full due.
        state.update({"multi_offer_action": "proposal", "multi_reply_date_spoken": "yes",
                      "multi_reply_terms_json": '[{"product_id":"p1","amount":"credit card",'
                                                '"date":"27 October"}]'})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual({"product_id": "p1", "date": "27 October"},
                         json.loads(state["multi_partial_terms_json"])["p1"])
        self.assertIn("510.00", state["multi_clarify_prompt"])
        self.assertIn("27 October", state["multi_clarify_prompt"])

    def test_day_after_product_deadline_is_not_offered(self) -> None:
        state = self.prepared()
        products = json.loads(state["product_json"])
        products[0]["deadline"] = "2026-10-14"
        state["product_json"] = json.dumps(products)
        state.update({"multi_offer_action": "proposal", "multi_reply_date_spoken": "yes",
                      "multi_reply_terms_json": '[{"product_id":"p1","amount":"credit card",'
                                                '"date":"tomorrow"}]'})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual("clarify", state["multi_next"])
        self.assertEqual({"product_id": "p1", "invalid_date": "tomorrow"},
                         json.loads(state["multi_partial_terms_json"])["p1"])
        self.assertIn("2026-10-14", state["multi_clarify_prompt"])
        self.assertNotIn("tomorrow", state["multi_clarify_prompt"])
        self.assertEqual({}, json.loads(state["multi_terms_json"]))

    def test_paid_claim_wins_over_same_turn_question(self) -> None:
        state = self.prepared()
        state.update({"multi_offer_action": "paid", "multi_offer_fact_question": "yes",
                      "multi_reply_product_id": "p2"})
        run_code(self.workflow, "multi_advance", state)
        self.assertEqual({"p2": "paid"}, json.loads(state["multi_notes_json"]))
        self.assertNotEqual("question", state["multi_next"])


if __name__ == "__main__":
    unittest.main()

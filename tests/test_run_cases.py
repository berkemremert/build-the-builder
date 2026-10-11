"""Regression checks for preserving a finished call when bank scoring is unavailable."""

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from tools import run_cases
from tools.score import score_ledger


class LedgerFailureTests(unittest.TestCase):
    def test_hard_cases_reuse_published_account_facts(self):
        root = Path(run_cases.__file__).resolve().parents[1]
        public = {case["id"]: case for case in json.loads((root / "public-test-set.json").read_text())}
        hard = json.loads((root / "hard-test-set.json").read_text())
        self.assertEqual(30, len(hard))
        self.assertEqual(len(hard), len({case["id"] for case in hard}))
        for case in hard:
            with self.subTest(case=case["id"]):
                self.assertNotIn(case["id"], public)
                self.assertIn(case["source_case"], public)
                seed = public[case["source_case"]]
                for field in ("variables", "identity", "products"):
                    self.assertEqual(seed[field], case[field])
                known_ids = {f"p{index}" for index in range(1, len(seed["products"]) + 1)}
                for field in ("promises", "notes"):
                    self.assertTrue(all(item["product_id"] in known_ids for item in case["expected"][field]))
                self.assertTrue(case["checks"] and case["rules"])
                persona = run_cases.persona_for(case, {"identity": case["identity"]})
                self.assertIn(case["id"], persona["name"])
                self.assertIn(run_cases.private_plan(case["script"]), persona["prompt"])

    def test_private_plan_hides_step_numbers_and_preserves_fragments(self):
        root = Path(run_cases.__file__).resolve().parents[1]
        case = next(case for case in json.loads((root / "long-call-stress.json").read_text())
                    if case["id"] == "LONG-05")
        persona = run_cases.persona_for(case, {"identity": case["identity"]})
        prompt = persona["prompt"]
        self.assertIn("PRIVATE CALL PLAN (never read it aloud)", prompt)
        self.assertNotIn("19. For the car loan", prompt)
        self.assertIn("- For the car loan, say the product is not yours.", prompt)
        self.assertIn('say "19 October" only', prompt)
        self.assertIn("A deliberately split answer may be a natural fragment", prompt)

    def test_script_leakage_flags_recited_step_but_not_natural_fragment(self):
        root = Path(run_cases.__file__).resolve().parents[1]
        case = next(case for case in json.loads((root / "long-call-stress.json").read_text())
                    if case["id"] == "LONG-05")
        leaked = [{"role": "user", "content": (
            "past-due amount on the personal loan. Could that payment take one business day "
            "to appear? Short sentence. Wait. 19. For the car loan, say the product is not yours.")}]
        natural = [{"role": "user", "content": "Actually, for the card, I can only"}]
        self.assertEqual("19", run_cases.script_leakage(case, leaked))
        self.assertIsNone(run_cases.script_leakage(case, natural))

    def test_leaked_caller_plan_makes_run_unscored(self):
        case = {"id": "LONG-05", "source_case": "PUB-12", "level": "hard",
                "family": "Long-call state",
                "script": "19. For the car loan, say the product is not yours."}
        ledger = {"calls": [], "promises": [], "notes": [], "callbacks": [],
                  "transfers": [], "outcome": {}}
        judge = {"runs": [{"personaId": "persona-long", "callId": "call-long",
                           "outcome": "pass", "reason": ""}]}
        call = {"messages": [{"role": "user", "content":
                "Short sentence. Wait. 19. For the car loan, say the product is not yours."}],
                "endedReason": "assistant-ended-call"}
        with mock.patch.object(run_cases, "http", side_effect=[{"variables": {"customer_id": "c_test"}}, ledger]), \
             mock.patch.object(run_cases.freya, "run_simulation", return_value=("sim-1", "iteration-1", "completed")), \
             mock.patch.object(run_cases.freya, "get_iteration", return_value=judge), \
             mock.patch.object(run_cases.freya, "get_call", return_value=call), \
             mock.patch.object(run_cases, "score_ledger", return_value={"pass": True, "failed": []}), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            rows, *_ = run_cases.run_once("agent-1", [case], {"LONG-05": "persona-long"})
        self.assertIsNone(rows[0]["pass"])
        self.assertEqual("19", rows[0]["persona_leakage"])
        self.assertIn("UNSCORED LONG-05", output.getvalue())

    def test_source_case_seeds_bank_but_custom_case_is_scored(self):
        case = {"id": "HARD-01", "source_case": "PUB-01", "level": "hard",
                "family": "Dates", "expected": {"outcome": "promise", "reason": None,
                "promises": [{"product_id": "p1", "amount": 420, "date": "2026-10-27"}],
                "notes": [], "callbacks": [], "transfers": []}}
        ledger = {"outcome": "promise", "reason": None,
                  "promises": [{"product_id": "p1", "amount": 420, "date": "2026-10-27"}],
                  "notes": [], "callbacks": [], "transfers": [],
                  "calls": [{"tool": "close_call", "result": {"ok": True}}]}
        judge = {"runs": [{"personaId": "persona-hard", "callId": "call-hard",
                           "outcome": "pass", "reason": ""}]}
        with mock.patch.object(run_cases, "http", side_effect=[{"variables": {"customer_id": "c_seed"}}, ledger]) as bank, \
             mock.patch.object(run_cases.freya, "run_simulation", return_value=("sim-1", "iteration-1", "completed")) as simulate, \
             mock.patch.object(run_cases.freya, "get_iteration", return_value=judge), \
             mock.patch.object(run_cases.freya, "get_call", return_value={"messages": [], "endedReason": "assistant-ended-call"}), \
             mock.patch.object(run_cases, "score_ledger", wraps=score_ledger) as score, \
             contextlib.redirect_stdout(io.StringIO()):
            rows, *_ = run_cases.run_once("agent-1", [case], {"HARD-01": "persona-hard"})

        bank.assert_any_call("/v1/practice/sessions", {"case": "PUB-01"})
        simulate.assert_called_once()
        self.assertEqual({"persona-hard": {"customer_id": "c_seed"}}, simulate.call_args.args[2])
        score.assert_called_once_with(case, ledger)
        self.assertEqual((rows[0]["id"], rows[0]["pass"]), ("HARD-01", True))
        self.assertEqual(rows[0]["source_case"], "PUB-01")

    def test_failed_ledger_keeps_judge_and_call_result_unscored(self):
        case = {"id": "PUB-13", "level": "hard", "family": "Multi-product"}
        judge = {"runs": [{"personaId": "persona-1", "callId": "call-1",
                           "outcome": "fail", "reason": "wrong read-back dates"}]}
        for bad_ledger in (json.JSONDecodeError("Expecting value", "", 0),
                           {"ok": False, "error": "bank unavailable"}):
            with self.subTest(bad_ledger=type(bad_ledger).__name__), \
                 mock.patch.object(run_cases, "http", side_effect=[{"variables": {"customer_id": "c_test"}}, bad_ledger]), \
                 mock.patch.object(run_cases.freya, "run_simulation", return_value=("sim-1", "iteration-1", "completed")), \
                 mock.patch.object(run_cases.freya, "get_iteration", return_value=judge), \
                 mock.patch.object(run_cases.freya, "get_call", return_value={"messages": [], "endedReason": "customer-ended-call"}), \
                 mock.patch.object(run_cases, "score_ledger") as score, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                rows, sim_id, iteration_id, status, _, _ = run_cases.run_once("agent-1", [case], {"PUB-13": "persona-1"})

            score.assert_not_called()
            self.assertEqual((sim_id, iteration_id, status), ("sim-1", "iteration-1", "completed"))
            self.assertIsNone(rows[0]["pass"])
            self.assertEqual(rows[0]["judge_outcome"], "fail")
            self.assertEqual(rows[0]["judge_reason"], "wrong read-back dates")
            self.assertEqual(rows[0]["call_id"], "call-1")
            self.assertEqual(rows[0]["ended"], "customer-ended-call")
            self.assertFalse(rows[0]["ledger_available"])
            self.assertIsNone(rows[0]["tools"])
            self.assertIn("UNSCORED PUB-13", output.getvalue())


if __name__ == "__main__":
    unittest.main()

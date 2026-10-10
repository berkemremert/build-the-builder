"""Regression checks for preserving a finished call when bank scoring is unavailable."""

import contextlib
import io
import json
import unittest
from unittest import mock

from tools import run_cases


class LedgerFailureTests(unittest.TestCase):
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

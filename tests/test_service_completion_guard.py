"""A declined write does not finish a call that contains another request."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generic_workflow_fallback import _Operation, _post_read_evidence_code, build_generic_workflow
from tests.test_generic_workflow_fallback import _policy, _spec


def _matching_targets(node: dict, values: dict[str, str]) -> list[str]:
    def matches(condition: dict) -> bool:
        if condition.get("logic") == "or":
            return any(matches(item) for item in condition["conditions"])
        if condition.get("logic") == "and":
            return all(matches(item) for item in condition["conditions"])
        return (condition.get("operator") == "equals" and
                values.get(condition.get("variable")) == condition.get("operand"))

    targets = []
    for transition in node["transitions"]:
        if matches(transition["condition"]):
            targets.append(transition["target_node"])
    return targets


class ServiceCompletionGuardTests(unittest.TestCase):
    def setUp(self):
        self.nodes = build_generic_workflow(_policy(), _spec())["nodes"]
        self.service = self.nodes["service"]

    def test_polite_thanks_with_new_request_stays_in_service(self):
        # E4's turn thanked the agent, then requested a prior statement. Freya
        # extracted write confirmation=no; that must not route to farewell.
        utterance = "Anladım, teşekkürler. Geçen ayın ekstresini gönderebilir misiniz?"
        self.assertIn("teşekkürler", utterance)
        variables = {"caller_finished": "no"}
        self.assertEqual([], _matching_targets(self.service, variables))
        extraction = {item["name"]: item for item in self.service["extraction_schema"]}
        self.assertIn("new request", extraction["caller_finished"]["description"])
        self.assertIn("thank-you followed by a request", extraction["caller_finished"]["description"])

    def test_clear_no_more_help_routes_to_farewell(self):
        variables = {"caller_finished": "yes"}
        self.assertEqual(["farewell"], _matching_targets(self.service, variables))

    def test_completion_precedes_stale_action_request(self):
        variables = {"caller_finished": "yes", "requested_write_action": "saveArrangement"}
        matched = _matching_targets(self.service, variables)
        self.assertEqual("farewell", matched[0])

    def test_initial_demand_cannot_commit_without_proposal(self):
        variables = {"requested_write_action": "saveArrangement",
                     "explicit_action_confirmation": "yes", "confirmed_write_action": "saveArrangement"}
        self.assertEqual(["proposal_saveArrangement"], _matching_targets(self.service, variables))
        self.assertEqual("llm", self.nodes["proposal_saveArrangement"]["on_enter"]["type"])
        self.assertNotIn("tools", self.nodes["proposal_saveArrangement"])
        self.assertEqual(["confirm_saveArrangement"],
                         _matching_targets(self.nodes["proposal_saveArrangement"], variables))
        self.assertEqual(["commit"],
                         _matching_targets(self.nodes["confirm_saveArrangement"], variables))

    def test_declined_final_confirmation_exits_cleanly(self):
        confirm = self.nodes["confirm_saveArrangement"]
        self.assertEqual(["farewell"], _matching_targets(
            confirm, {"explicit_action_confirmation": "no"}))

    def test_read_prerequisites_never_create_write_consent(self):
        state = {"generic_pending_cards": "{}", "generic_pending_preview": "{}"}
        checks = [(('result', 'returnCode'), '1')]
        for name in ("cards", "preview"):
            operation = _Operation(name, "/" + name, "post", {}, "read")
            code = _post_read_evidence_code(operation, checks,
                                            {"change": ["cards", "preview"]})
            result = {"status_code": 200, "data": {"result": {"returnCode": "1"}}}
            exec(code, {"vars": state, "result": result})
            if name == "cards":
                self.assertNotIn("generic_prereq_ready_change", state)
        self.assertEqual("yes", state["generic_prereq_ready_change"])
        self.assertNotIn("confirmed_write_action", state)

    def test_failed_write_tool_exits_commit_without_retry(self):
        commit = self.nodes["commit"]
        self.assertEqual(["farewell"], _matching_targets(
            commit, {"saveArrangement_response.status": "error"}))
        self.assertNotIn("commit", [edge["target_node"] for edge in commit["transitions"]])


if __name__ == "__main__":
    unittest.main()

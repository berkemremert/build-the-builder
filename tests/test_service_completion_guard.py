"""A declined write does not finish a call that contains another request."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generic_workflow_fallback import build_generic_workflow
from tests.test_generic_workflow_fallback import _policy, _spec


def _matching_targets(node: dict, values: dict[str, str]) -> list[str]:
    targets = []
    for transition in node["transitions"]:
        condition = transition["condition"]
        parts = condition.get("conditions", [condition])
        if any(part.get("operator") == "equals" and
               values.get(part["variable"]) == part["operand"] for part in parts):
            targets.append(transition["target_node"])
    return targets


class ServiceCompletionGuardTests(unittest.TestCase):
    def setUp(self):
        self.service = build_generic_workflow(_policy(), _spec())["nodes"]["service"]

    def test_polite_thanks_with_new_request_stays_in_service(self):
        # E4's turn thanked the agent, then requested a prior statement. Freya
        # extracted write confirmation=no; that must not route to farewell.
        utterance = "Anladım, teşekkürler. Geçen ayın ekstresini gönderebilir misiniz?"
        self.assertIn("teşekkürler", utterance)
        variables = {"explicit_action_confirmation": "no", "caller_finished": "no"}
        self.assertEqual([], _matching_targets(self.service, variables))
        extraction = {item["name"]: item for item in self.service["extraction_schema"]}
        self.assertIn("new request", extraction["caller_finished"]["description"])
        self.assertIn("thank-you followed by a request", extraction["caller_finished"]["description"])

    def test_clear_no_more_help_routes_to_farewell(self):
        variables = {"explicit_action_confirmation": "no", "caller_finished": "yes"}
        self.assertEqual(["farewell"], _matching_targets(self.service, variables))

    def test_explicit_write_confirmation_still_routes_to_commit(self):
        variables = {"explicit_action_confirmation": "yes", "caller_finished": "no"}
        self.assertEqual(["commit"], _matching_targets(self.service, variables))


if __name__ == "__main__":
    unittest.main()

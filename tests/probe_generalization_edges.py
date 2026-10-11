"""Explicit local probes for changed OpenAPI contracts.

These tests use only invented data from probe_changed_brief and deliberately
sit outside default ``test_*.py`` discovery until the generator supports them.
Run with ``python3 tests/probe_generalization_edges.py``.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_changed_brief import synthetic_api, synthetic_policy  # noqa: E402
from qa import validate_workflow  # noqa: E402
from workflow_generator import build_workflow  # noqa: E402


def _body(workflow: dict, node_name: str) -> dict:
    action = workflow["nodes"][node_name]["pre_actions"][0]
    return action["function"]["parameters"]["properties"]["body"]["properties"]


class OpenApiGeneralityEdges(unittest.TestCase):
    def test_multi_product_note_uses_renamed_request_fields(self) -> None:
        api = synthetic_api()
        policy = synthetic_policy()
        policy["verified_call"] = (
            "For up to three products, state all products, then process each "
            "product one by one in account order. Note any without a promise."
        )
        workflow = build_workflow(policy, api)
        self.assertEqual([], validate_workflow(workflow, api))
        body = _body(workflow, "add_note_call")
        self.assertEqual("{{pending_note_product_id}}", body["account_ref"]["default"])
        self.assertEqual("{{pending_note_reason}}", body["note_code"]["default"])

    def test_identity_routes_use_renamed_result_fields(self) -> None:
        api = synthetic_api()
        props = (api["paths"]["/verify_identity"]["post"]["responses"]["200"]
                 ["content"]["application/json"]["schema"]["properties"])
        props["passed"] = props.pop("verified")
        props["complete"] = props.pop("all_verified")
        props["tries_left"] = props.pop("wrong_answers_left")
        workflow = build_workflow(synthetic_policy(), api)
        self.assertEqual([], validate_workflow(workflow, api))
        first_routes = str(workflow["nodes"]["identity_dob"]["transitions"])
        second_routes = str(workflow["nodes"]["identity_phone"]["transitions"])
        self.assertIn("verify_identity_response.data.passed", first_routes)
        self.assertIn("verify_identity_response.data.complete", second_routes)
        self.assertIn("verify_identity_response.data.tries_left", first_routes)

    def test_account_collection_can_come_from_another_success_status(self) -> None:
        api = synthetic_api()
        responses = api["paths"]["/get_account"]["post"]["responses"]
        success = responses.pop("200")
        success["content"]["application/json"]["schema"]["properties"]["accounts"] = (
            success["content"]["application/json"]["schema"]["properties"].pop("products")
        )
        responses["201"] = success
        workflow = build_workflow(synthetic_policy(), api)
        self.assertEqual([{"path": "data.accounts", "variable": "account_products"}],
                         workflow["nodes"]["load_account"]["response_mapping"])
        self.assertEqual([], validate_workflow(workflow, api))


if __name__ == "__main__":
    unittest.main(verbosity=2)

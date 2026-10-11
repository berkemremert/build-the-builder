"""A model paraphrase must not disable source-required multi-product routing."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from policy_compiler import compile_policy  # noqa: E402
from policy_capabilities import requires_multi_product  # noqa: E402
from qa import validate_workflow  # noqa: E402
from workflow_generator import build_workflow  # noqa: E402


class MultiCapabilityDetectionTests(unittest.TestCase):
    def test_source_and_paraphrase_detection_without_single_product_false_positive(self) -> None:
        self.assertTrue(requires_multi_product({"raw_brief": "get_account returns one to three products."}))
        self.assertTrue(requires_multi_product({"verified_call": "State all the products first."}))
        self.assertTrue(requires_multi_product({"verified_call": "Handle products one by one."}))
        self.assertFalse(requires_multi_product({"raw_brief": "The caller has one product."}))

    def test_v9_model_paraphrase_keeps_required_multi_product_reducer(self) -> None:
        api_path = ROOT / "tools" / "openapi.json"
        api = json.loads(api_path.read_text())
        policy = compile_policy([str(ROOT / "briefs" / "brief-a.md"),
                                 str(ROOT / "briefs" / "brief-b.md")], str(api_path), None)
        # The failed v9 build used this exact model-authored summary. Its source
        # still includes Brief B's explicit one-to-three-product requirements.
        policy["verified_call"] = (ROOT / "tests" / "fixtures" / "v9_verified_call.txt").read_text()
        policy["constraints"].pop("product_limits", None)
        workflow = build_workflow(policy, api)
        self.assertIn("multi_advance", workflow["nodes"])
        self.assertEqual([], validate_workflow(workflow, api))
        routes = workflow["nodes"]["multi_advance"]["transitions"]
        self.assertTrue(any(item["target_node"] == "prepare_confirmation" and
                            "multi_next" in json.dumps(item["condition"]) for item in routes))


if __name__ == "__main__":
    unittest.main()

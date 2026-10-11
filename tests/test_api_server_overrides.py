"""Offline regression for operation and path OpenAPI server precedence."""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from api_servers import operation_url  # noqa: E402
from probe_changed_brief import synthetic_api, synthetic_policy  # noqa: E402
from qa import validate_workflow  # noqa: E402
from workflow_generator import build_workflow  # noqa: E402


def _account_url(workflow: dict) -> str:
    return (workflow["nodes"]["load_account"]["pre_actions"][0]["function"]
            ["parameters"]["properties"]["url"]["default"])


class ApiServerOverrideTests(unittest.TestCase):
    def test_operation_server_overrides_path_and_root(self) -> None:
        api = synthetic_api()
        path = api["paths"]["/get_account"]
        path["servers"] = [{"url": "https://path.synthetic.invalid/v7"}]
        path["post"]["servers"] = [{"url": "https://accounts.synthetic.invalid/v8"}]
        expected = "https://accounts.synthetic.invalid/v8/get_account"
        self.assertEqual(expected, operation_url(api, "/get_account", "POST"))
        workflow = build_workflow(synthetic_policy(), api)
        self.assertEqual(expected, _account_url(workflow))
        self.assertEqual([], validate_workflow(workflow, api))

        wrong = copy.deepcopy(workflow)
        wrong["nodes"]["load_account"]["pre_actions"][0]["function"]["parameters"]["properties"]["url"]["default"] = (
            "https://synthetic.invalid/v7/get_account")
        self.assertTrue(any("exact URL" in error for error in validate_workflow(wrong, api)))

    def test_path_server_overrides_root(self) -> None:
        api = synthetic_api()
        api["paths"]["/get_account"]["servers"] = [{"url": "https://path.synthetic.invalid/v9"}]
        self.assertEqual("https://path.synthetic.invalid/v9/get_account",
                         operation_url(api, "/get_account", "post"))
        workflow = build_workflow(synthetic_policy(), api)
        self.assertEqual([], validate_workflow(workflow, api))

    def test_server_variable_uses_declared_default(self) -> None:
        api = synthetic_api()
        api["paths"]["/get_account"]["post"]["servers"] = [{
            "url": "https://{region}.synthetic.invalid/v8",
            "variables": {"region": {"default": "east", "enum": ["east", "west"]}},
        }]
        self.assertEqual("https://east.synthetic.invalid/v8/get_account",
                         operation_url(api, "/get_account", "post"))

    def test_missing_or_invalid_effective_server_fails_closed(self) -> None:
        api = synthetic_api()
        api["paths"]["/get_account"]["post"]["servers"] = []
        with self.assertRaisesRegex(ValueError, "needs a server URL"):
            build_workflow(synthetic_policy(), api)
        api["paths"]["/get_account"]["post"]["servers"] = [{"url": "https://{region}.invalid"}]
        with self.assertRaisesRegex(ValueError, "no definition"):
            operation_url(api, "/get_account", "post")


if __name__ == "__main__":
    unittest.main()

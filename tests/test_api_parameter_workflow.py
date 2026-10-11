"""Generated tool and QA checks for required OpenAPI operation parameters."""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from probe_changed_brief import synthetic_api, synthetic_policy  # noqa: E402
from qa import validate_workflow  # noqa: E402
from workflow_generator import build_workflow  # noqa: E402


def _parameter(name: str, location: str, *, enum: list[str] | None = None,
               default: str | None = None) -> dict:
    schema: dict = {"type": "string"}
    if enum is not None:
        schema["enum"] = enum
    if default is not None:
        schema["default"] = default
    return {"name": name, "in": location, "required": True, "schema": schema}


def _account_headers(workflow: dict) -> dict:
    action = workflow["nodes"]["load_account"]["pre_actions"][0]
    return action["function"]["parameters"]["properties"]["headers"]["properties"]


class ApiParameterWorkflowTests(unittest.TestCase):
    def test_required_operation_header_is_sent_and_qa_checks_it(self) -> None:
        api = synthetic_api()
        api["paths"]["/get_account"]["post"]["parameters"] = [
            _parameter("X-Channel", "header", enum=["voice"])]
        workflow = build_workflow(synthetic_policy(), api)
        self.assertEqual("voice", _account_headers(workflow)["X-Channel"]["default"])
        self.assertEqual([], validate_workflow(workflow, api))

        missing = copy.deepcopy(workflow)
        del _account_headers(missing)["X-Channel"]
        self.assertTrue(any("X-Channel" in error for error in validate_workflow(missing, api)))
        wrong = copy.deepcopy(workflow)
        _account_headers(wrong)["X-Channel"]["default"] = "web"
        self.assertTrue(any("X-Channel" in error for error in validate_workflow(wrong, api)))

    def test_path_header_is_inherited_by_generated_tool(self) -> None:
        api = synthetic_api()
        api["paths"]["/get_account"]["parameters"] = [
            _parameter("X-Channel", "header", default="voice")]
        workflow = build_workflow(synthetic_policy(), api)
        self.assertEqual("voice", _account_headers(workflow)["X-Channel"]["default"])
        self.assertEqual([], validate_workflow(workflow, api))

    def test_unsupported_required_query_and_path_fail_closed(self) -> None:
        api = synthetic_api()
        api["paths"]["/get_account"]["post"]["parameters"] = [
            _parameter("locale", "query", default="en")]
        with self.assertRaisesRegex(ValueError, "unsupported query parameter"):
            build_workflow(synthetic_policy(), api)
        baseline = build_workflow(synthetic_policy(), synthetic_api())
        self.assertTrue(any("unsupported query parameter" in error
                            for error in validate_workflow(baseline, api)))

        api = synthetic_api()
        api["paths"]["/get_account/{accountId}"] = api["paths"].pop("/get_account")
        api["paths"]["/get_account/{accountId}"]["post"]["operationId"] = "get_account"
        with self.assertRaisesRegex(ValueError, "unsupported path parameter"):
            build_workflow(synthetic_policy(), api)


if __name__ == "__main__":
    unittest.main()

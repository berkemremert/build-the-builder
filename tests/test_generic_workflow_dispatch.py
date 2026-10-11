"""Contract-driven builder dispatch and QA without practice operation names."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import builder  # noqa: E402
from qa import validate_generic_workflow  # noqa: E402
from policy_compiler import compile_policy  # noqa: E402


def contract() -> dict:
    return {"openapi": "3.0.0", "servers": [{"url": "https://source.example"}],
            "paths": {"/statement": {"post": {"operationId": "readStatement",
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object", "properties": {"reference": {"type": "string"}},
                    "required": ["reference"]}}}},
                "responses": {"200": {"description": "Statement returned"}}}}}}


def workflow() -> dict:
    tool = {"type": "function", "function": {"name": "readStatement",
            "handler": "__function__:api_call", "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "default": "https://mock.example/statement"},
                "method": {"type": "string", "default": "POST"},
                "body": {"type": "object", "properties": {
                    "reference": {"type": "string", "default": "{{reference}}"}},
                    "required": ["reference"]}}}}}
    return {"initial_node": "ask", "system_prompt": "Use the supplied contract.", "nodes": {
        "ask": {"name": "ask", "node_type": "conversation", "task_message": "Ask for a reference.",
                "on_enter": {"type": "llm"}, "tools": [tool],
                "transitions": [{"target_node": "ended", "condition": {"operator": "always"}}]},
        "ended": {"name": "ended", "node_type": "end_call", "on_enter": {"type": "silent"},
                  "terminal": {"type": "function", "function": {
                      "name": "end_call", "handler": "__function__:end_call",
                      "parameters": {"type": "object", "properties": {}}}}}}}


class GenericWorkflowDispatchTests(unittest.TestCase):
    def test_qa_accepts_only_declared_operation(self) -> None:
        api = builder._mock_openapi(contract(), "https://mock.example")
        self.assertEqual([], validate_generic_workflow(workflow(), api))
        invalid = workflow()
        invalid["nodes"]["ask"]["tools"][0]["function"]["name"] = "inventedTool"
        self.assertTrue(any("operation name" in error for error in validate_generic_workflow(invalid, api)))

    def test_qa_rejects_missing_contract_tool(self) -> None:
        api = builder._mock_openapi(contract(), "https://mock.example")
        missing = workflow()
        missing["nodes"]["ask"].pop("tools")
        self.assertTrue(any("readStatement" in error and "not exposed" in error
                            for error in validate_generic_workflow(missing, api)))

    def test_case_folder_dispatches_when_practice_operations_are_absent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btb-generic-dispatch-") as directory:
            folder = Path(directory) / "case"
            folder.mkdir()
            (folder / "BRIEF.md").write_text("Use only the documented statement service.", encoding="utf-8")
            (folder / "openapi.json").write_text(json.dumps(contract()), encoding="utf-8")
            module = types.ModuleType("generic_workflow_fallback")
            generic = mock.Mock(return_value=workflow())
            module.build_generic_workflow = generic

            def model(_prompt: str, *, purpose: str) -> dict:
                self.assertEqual("compile bank brief and tool policy", purpose)
                return {"global_rules": "Follow the source.", "opening": "Ask for a reference.",
                        "identity": "", "verified_call": "Use the statement service.",
                        "outcomes": {}, "tool_policy": {"readStatement": "Only on request."},
                        "raw_brief": "", "constraints": {"extraction_status": "model"}}

            args = argparse.Namespace(case_folder=str(folder), mock_url="https://mock.example",
                                      brief=None, openapi=None, slot="generic", out=str(Path(directory) / "out"),
                                      no_live=True, no_test=True)
            with mock.patch.dict(sys.modules, {"generic_workflow_fallback": module}), \
                 mock.patch.object(builder, "_model_caller", return_value=model):
                result = builder.run(args)
            generic.assert_called_once()
            trace = [json.loads(line) for line in Path(result["trace"]).read_text().splitlines()]
            self.assertIn({"event": "workflow_generator_selected", "kind": "contract_driven"},
                          [{"event": row["event"], "kind": row.get("kind")}
                           for row in trace if row["event"] == "workflow_generator_selected"])

    def test_case_folder_falls_back_when_named_contract_shape_is_incompatible(self) -> None:
        with tempfile.TemporaryDirectory(prefix="btb-generic-shape-") as directory:
            folder = Path(directory) / "case"
            folder.mkdir()
            (folder / "BRIEF.md").write_text("Use source-only bank rules.", encoding="utf-8")
            source = ROOT / "tools/openapi.json"
            (folder / "openapi.json").write_bytes(source.read_bytes())
            module = types.ModuleType("generic_workflow_fallback")
            generic = mock.Mock(return_value=workflow())
            module.build_generic_workflow = generic
            extracted = compile_policy([str(ROOT / "briefs/brief-a.md")], str(source), None)
            extracted["constraints"]["extraction_status"] = "model"
            args = argparse.Namespace(case_folder=str(folder), mock_url="https://mock.example",
                                      brief=None, openapi=None, slot="shape", out=str(Path(directory) / "out"),
                                      no_live=True, no_test=True)
            with mock.patch.dict(sys.modules, {"generic_workflow_fallback": module}), \
                 mock.patch.object(builder, "_model_caller", return_value=lambda *_a, **_kw: extracted), \
                 mock.patch("workflow_generator.build_workflow", side_effect=ValueError("incompatible schema")), \
                 mock.patch("qa.validate_generic_workflow", return_value=[]):
                result = builder.run(args)
            generic.assert_called_once()
            events = [json.loads(line) for line in Path(result["trace"]).read_text().splitlines()]
            self.assertIn("deterministic_generator_incompatible", [row["event"] for row in events])


if __name__ == "__main__":
    unittest.main()

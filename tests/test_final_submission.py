"""Offline checks for the single-workflow submission package."""

from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools import final_submission as submission


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


class FinalSubmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = base / "repo"
        self.root.mkdir()
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.name", "Offline Test")
        _git(self.root, "config", "user.email", "offline@example.test")
        (self.root / "builder.py").write_text("# offline builder fixture\n")
        _git(self.root, "add", "builder.py")
        _git(self.root, "commit", "-qm", "initial")
        source = base / "source"
        source.mkdir()
        self.workflow = source / "final.workflow.json"
        self.trace = source / "final.trace.jsonl"
        self.workflow.write_text(json.dumps({"workflow_id": "workflow-1", "agent_id": "agent-1",
                                             "workflow": {"nodes": {"start": {"node_type": "conversation"}}}}))
        self.events = [
            {"event": "start", "no_live": False},
            {"event": "freya_saved_published", "workflow_id": "workflow-1",
             "agent_id": "agent-1", "version": 1},
            {"event": "freya_exported", "workflow_id": "workflow-1", "path": str(self.workflow)},
            {"event": "end", "status": "ok", "workflow_id": "workflow-1",
             "agent_id": "agent-1", "trace": str(self.trace)},
        ]
        self._write_trace()

    def _write_trace(self) -> None:
        self.trace.write_text("\n".join(json.dumps(row) for row in self.events) + "\n")

    def test_stage_then_manifest_uses_committed_artifacts_and_current_commit(self) -> None:
        workflow, trace = submission.stage_artifacts(self.root, self.workflow, self.trace)
        self.assertEqual(("submission_artifacts/final.workflow.json",
                          "submission_artifacts/final.trace.jsonl"), (workflow, trace))
        _git(self.root, "add", "submission_artifacts")
        _git(self.root, "commit", "-qm", "include final artifacts")
        output = Path(self.temp.name) / "submission.json"
        manifest = submission.create_manifest(
            self.root, workflow, trace, "https://mock.berking.workers.dev",
            "python3 builder.py {case_folder} {mock_url}", output)
        self.assertEqual(submission.TEAM, manifest["team"])
        self.assertEqual(submission.MEMBERS, manifest["members"])
        self.assertEqual(submission.REPO_URL, manifest["repo"])
        self.assertEqual(submission._git(self.root, "rev-parse", "HEAD"), manifest["commit"])
        self.assertEqual(("workflow-1", "agent-1"), submission.check_manifest(self.root, manifest))
        self.assertEqual(manifest, json.loads(output.read_text()))
        manifest["mock_url"] = "https://YOUR-MOCK.YOUR-SUBDOMAIN.workers.dev"
        with self.assertRaisesRegex(submission.SubmissionError, "public HTTPS URL"):
            submission.check_manifest(self.root, manifest)

    def test_local_workflow_or_mismatched_trace_is_rejected(self) -> None:
        self.events[1]["agent_id"] = "different-agent"
        self._write_trace()
        with self.assertRaisesRegex(submission.SubmissionError, "IDs do not match"):
            submission.validate_export(self.workflow, self.trace)
        self.events[1]["agent_id"] = "agent-1"
        self._write_trace()
        self.workflow.write_text(json.dumps({"initial_node": "start", "nodes": {"start": {}}}))
        with self.assertRaisesRegex(submission.SubmissionError, "live Freya export"):
            submission.validate_export(self.workflow, self.trace)

    def test_artifact_secret_and_uncommitted_manifest_path_are_rejected(self) -> None:
        self.events.append({"event": "debug", "token": "fak_" + "REALISH_SECRET_123456789"})
        self._write_trace()
        with self.assertRaisesRegex(submission.SubmissionError, "Possible credential"):
            submission.validate_export(self.workflow, self.trace)
        self.events.pop()
        self._write_trace()
        workflow, trace = submission.stage_artifacts(self.root, self.workflow, self.trace)
        with self.assertRaisesRegex(submission.SubmissionError, "must be committed"):
            submission.create_manifest(self.root, workflow, trace,
                                       "https://mock.berking.workers.dev",
                                       "python3 builder.py {case_folder} {mock_url}",
                                       Path(self.temp.name) / "submission.json")

    def test_case_snapshot_rejects_added_or_changed_source(self) -> None:
        case = Path(self.temp.name) / "case"
        case.mkdir()
        document = case / "BRIEF.md"
        document.write_text("original case\n")
        path = str(document.resolve())
        fingerprint = {"path": path, "sha256": hashlib.sha256(document.read_bytes()).hexdigest()}
        self.events[0:0] = [
            {"event": "case_discovered", "folder": str(case), "documents": [path],
             "mock_url": "https://mock.berking.workers.dev"},
            {"event": "inputs", "source_documents": [fingerprint]},
        ]
        self._write_trace()
        self.assertEqual(("workflow-1", "agent-1"), submission.validate_export(
            self.workflow, self.trace, "https://mock.berking.workers.dev"))
        with self.assertRaisesRegex(submission.SubmissionError, "mock URL"):
            submission.validate_export(self.workflow, self.trace, "https://other.workers.dev")
        (case / "2026-10-10-follow-up.txt").write_text("new source\n")
        with self.assertRaisesRegex(submission.SubmissionError, "source snapshot"):
            submission.validate_export(self.workflow, self.trace)
        (case / "2026-10-10-follow-up.txt").unlink()
        document.write_text("revised case\n")
        with self.assertRaisesRegex(submission.SubmissionError, "source changed"):
            submission.validate_export(self.workflow, self.trace)

    def test_export_and_trace_versions_must_agree(self) -> None:
        self.events[-1]["version"] = 2
        self._write_trace()
        with self.assertRaisesRegex(submission.SubmissionError, "end version"):
            submission.validate_export(self.workflow, self.trace)
        self.events[-1]["version"] = 1
        self.workflow.write_text(json.dumps({"workflow_id": "workflow-1", "agent_id": "agent-1",
                                             "version": 2,
                                             "workflow": {"nodes": {"start": {"node_type": "conversation"}}}}))
        self._write_trace()
        with self.assertRaisesRegex(submission.SubmissionError, "Export version"):
            submission.validate_export(self.workflow, self.trace)

    def test_same_ids_from_different_export_are_rejected(self) -> None:
        other = Path(self.temp.name) / "other" / self.workflow.name
        other.parent.mkdir()
        shutil.copy2(self.workflow, other)
        alternate = json.loads(other.read_text())
        alternate["workflow"]["nodes"]["start"]["prompt"] = "different published version"
        other.write_text(json.dumps(alternate))
        with self.assertRaisesRegex(submission.SubmissionError, "differs from the export"):
            submission.validate_export(other, self.trace)

    def test_known_offline_credentials_are_not_treated_as_live_secrets(self) -> None:
        submission._scan_text('Use Bearer authentication; fixture Bearer abcdefghijklmnop', 'test fixture')
        submission._scan_text('sk-or-offline', 'test fixture')
        with self.assertRaisesRegex(submission.SubmissionError, "Possible credential"):
            submission._scan_text('Bearer ' + 'a-different-token-123456789', 'artifact')


if __name__ == "__main__":
    unittest.main()

"""Stage and check one final Freya export, build trace, and submission manifest.

No network requests are made. Run ``stage`` before committing the two artifacts,
then run ``manifest`` after that commit so its commit field names the exact tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
TEAM = "berking"
MEMBERS = [{"name": "Berk Emre Mert", "email": "bmert21@ku.edu.tr"}]
REPO_URL = "https://github.com/berkemremert/build-the-builder"
FIELDS = {"team", "members", "repo", "commit", "run_command", "workflow", "trace", "mock_url"}
_SECRET = re.compile(
    r"\bfak_[A-Za-z0-9_-]{8,}\b|\bsk-[A-Za-z0-9_-]{8,}\b|"
    r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.I,
)
_EXAMPLE_KEYS = ("your_key", "your-key", "OFFLINE_", "sk-or-offline", "sk-or-v1-ignored",
                 "fak_1234567890abcdef", "Bearer authentication", "Bearer abcdefghijklmnop")


class SubmissionError(ValueError):
    """An artifact or manifest is not ready for submission."""


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if result.returncode:
        raise SubmissionError(f"git {' '.join(args)} failed: {result.stderr.strip()[:200]}")
    return result.stdout.strip()


def _repo_file(root: Path, name: str) -> tuple[Path, str]:
    if not isinstance(name, str) or not name:
        raise SubmissionError("Manifest file path must be a nonempty string")
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise SubmissionError(f"Manifest path must be relative and inside the repo: {name!r}")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise SubmissionError(f"Manifest file is missing or outside the repo: {name!r}")
    return resolved, path.as_posix()


def _scan_text(text: str, label: str) -> None:
    for match in _SECRET.finditer(text):
        token = match.group()
        if any(example.lower() in token.lower() for example in _EXAMPLE_KEYS):
            continue
        raise SubmissionError(f"Possible credential or private key in {label}; remove it before submission")


def _read_trace(path: Path) -> list[dict]:
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (UnicodeError, json.JSONDecodeError) as error:
        raise SubmissionError(f"Trace is not valid JSONL: {path.name}") from error
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise SubmissionError("Trace must contain JSONL event objects")
    return rows


def _last_event(rows: list[dict], name: str) -> dict:
    found = [row for row in rows if row.get("event") == name]
    if not found:
        raise SubmissionError(f"Trace has no {name} event")
    return found[-1]


def _check_case_snapshot(rows: list[dict], mock_url: str | None = None) -> None:
    """Check that the traced case inputs still match the complete discovered case."""
    discovered = [row for row in rows if row.get("event") == "case_discovered"]
    if not discovered:
        return
    if len(discovered) != 1:
        raise SubmissionError("Trace must identify exactly one case snapshot")
    case = discovered[0]
    folder = Path(str(case.get("folder") or ""))
    if not folder.is_dir():
        raise SubmissionError("Traced case folder is unavailable for source snapshot check")
    if mock_url is not None and case.get("mock_url") != mock_url:
        raise SubmissionError("Trace mock URL does not match submission mock URL")
    inputs = _last_event(rows, "inputs")
    fingerprints = inputs.get("source_documents")
    if not isinstance(fingerprints, list) or not fingerprints:
        raise SubmissionError("Trace has no source document fingerprints")
    # Use the builder's own discovery rules so a newly added case document
    # cannot be silently absent from the packaged run.
    from builder import _case_inputs
    documents, contract, _ = _case_inputs(folder)
    expected = [str(path.resolve()) for path in documents]
    actual = [item.get("path") if isinstance(item, dict) else None for item in fingerprints]
    if actual != expected or case.get("documents") != expected:
        raise SubmissionError("Case source snapshot differs from the traced document set")
    for item, path in zip(fingerprints, documents):
        if item.get("sha256") != hashlib.sha256(path.read_bytes()).hexdigest():
            raise SubmissionError(f"Case source changed after build: {path.name}")
    recorded_contract = inputs.get("openapi")
    if contract is not None and (not isinstance(recorded_contract, dict) or
                                 recorded_contract.get("path") != str(contract.resolve()) or
                                 recorded_contract.get("sha256") != hashlib.sha256(contract.read_bytes()).hexdigest()):
        raise SubmissionError("Case OpenAPI snapshot differs from the traced contract")


def validate_export(workflow_path: Path, trace_path: Path,
                    mock_url: str | None = None) -> tuple[str, str]:
    """Prove the artifact is a Freya export from the successful traced run."""
    workflow_text = workflow_path.read_text(encoding="utf-8")
    trace_text = trace_path.read_text(encoding="utf-8")
    _scan_text(workflow_text, workflow_path.name)
    _scan_text(trace_text, trace_path.name)
    try:
        export = json.loads(workflow_text)
    except json.JSONDecodeError as error:
        raise SubmissionError("Workflow export is not valid JSON") from error
    if not isinstance(export, dict) or not all(isinstance(export.get(key), str) and export[key]
                                               for key in ("workflow_id", "agent_id")):
        raise SubmissionError("Workflow must be a live Freya export with workflow_id and agent_id")
    if not isinstance(export.get("workflow"), dict) or not export["workflow"].get("nodes"):
        raise SubmissionError("Workflow export has no workflow nodes")
    rows = _read_trace(trace_path)
    _check_case_snapshot(rows, mock_url)
    start = _last_event(rows, "start")
    saved = _last_event(rows, "freya_saved_published")
    exported = _last_event(rows, "freya_exported")
    end = _last_event(rows, "end")
    if start.get("no_live") is True or end.get("status") != "ok":
        raise SubmissionError("Trace does not describe a successful live build")
    workflow_id, agent_id = export["workflow_id"], export["agent_id"]
    if any(row.get("workflow_id") != workflow_id for row in (saved, exported, end)) or any(
            row.get("agent_id") != agent_id for row in (saved, end)):
        raise SubmissionError("Workflow and trace IDs do not match the same published build")
    version = saved.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise SubmissionError("Trace does not confirm a published workflow version")
    if end.get("version") is not None and end["version"] != version:
        raise SubmissionError("Trace end version does not match the published version")
    for field in ("version", "version_id"):
        if field in export and export[field] is not None and export[field] != saved.get(field):
            raise SubmissionError(f"Export {field} does not match the published version")
    if not (rows.index(start) < rows.index(saved) < rows.index(exported) < rows.index(end)):
        raise SubmissionError("Trace events are not in build order")
    source_export = Path(str(exported.get("path") or ""))
    if source_export.name != workflow_path.name:
        raise SubmissionError("Trace export filename does not match the workflow artifact")
    if source_export.is_file():
        if hashlib.sha256(source_export.read_bytes()).digest() != hashlib.sha256(workflow_path.read_bytes()).digest():
            raise SubmissionError("Workflow artifact differs from the export recorded by the trace")
    elif any(row.get("event") == "case_discovered" for row in rows):
        raise SubmissionError("Traced original export is unavailable for artifact identity check")
    if Path(str(end.get("trace") or "")).name != trace_path.name:
        raise SubmissionError("Trace end event does not identify this trace")
    return workflow_id, agent_id


def stage_artifacts(root: Path, workflow: Path, trace: Path,
                    destination: str = "submission_artifacts") -> tuple[str, str]:
    validate_export(workflow, trace)
    if workflow.name == trace.name:
        raise SubmissionError("Workflow and trace filenames must differ")
    folder = root / destination
    if not folder.resolve().is_relative_to(root.resolve()):
        raise SubmissionError("Artifact destination must be inside the repo")
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for source in (workflow, trace):
        target = folder / source.name
        if target.exists() and target.read_bytes() != source.read_bytes():
            raise SubmissionError(f"Refusing to replace existing artifact {target}")
        if not target.exists():
            shutil.copy2(source, target)
        paths.append(target.relative_to(root).as_posix())
    return paths[0], paths[1]


def _committed_file(root: Path, relative: str, path: Path) -> None:
    try:
        committed = _git(root, "rev-parse", f"HEAD:{relative}")
    except SubmissionError as error:
        raise SubmissionError(f"Artifact must be committed before manifest creation: {relative}") from error
    if committed != _git(root, "hash-object", str(path)):
        raise SubmissionError(f"Artifact differs from the committed file: {relative}")


def _scan_committed_files(root: Path) -> None:
    tracked = _git(root, "ls-files", "-z").split("\0")
    forbidden = {"env.sh", ".env", ".freya-agents.json", ".personas.json"}
    for relative in filter(None, tracked):
        if relative in forbidden or relative.startswith(".env.") and relative != ".env.example":
            raise SubmissionError(f"Local credential or workspace file is tracked: {relative}")
        path = root / relative
        if not path.is_file():
            continue
        _scan_text(path.read_bytes().decode("utf-8", errors="ignore"), relative)


def _mock_url(value: str) -> None:
    if not isinstance(value, str):
        raise SubmissionError("mock_url must be a public HTTPS URL")
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username or url.password or
            url.query or url.fragment or url.hostname in {"localhost", "example.com"} or
            url.hostname.endswith(".example") or any(marker in url.hostname.lower()
                for marker in ("your-mock", "your-subdomain", "placeholder"))):
        raise SubmissionError("mock_url must be a public HTTPS URL without credentials or placeholders")


def check_manifest(root: Path, manifest: dict) -> tuple[str, str]:
    if not isinstance(manifest, dict) or set(manifest) != FIELDS:
        raise SubmissionError(f"submission.json must contain exactly: {', '.join(sorted(FIELDS))}")
    if (manifest["team"], manifest["members"], manifest["repo"]) != (TEAM, MEMBERS, REPO_URL):
        raise SubmissionError("Team, sole member, or repository does not match the approved submission")
    if manifest["commit"] != _git(root, "rev-parse", "HEAD"):
        raise SubmissionError("Manifest commit is not the current repository HEAD")
    if _git(root, "status", "--porcelain", "--untracked-files=no"):
        raise SubmissionError("Tracked files have uncommitted changes; commit them before creating submission.json")
    command = manifest["run_command"]
    try:
        tokens = shlex.split(command)
    except (TypeError, ValueError) as error:
        raise SubmissionError("run_command is not a valid shell command") from error
    if not any(Path(token).name in {"builder.py", "build.py"} for token in tokens):
        raise SubmissionError("run_command must invoke the repository builder")
    if "{case_folder}" not in command or "{mock_url}" not in command:
        raise SubmissionError("run_command must accept {case_folder} and {mock_url} placeholders")
    _mock_url(manifest["mock_url"])
    workflow, workflow_rel = _repo_file(root, manifest["workflow"])
    trace, trace_rel = _repo_file(root, manifest["trace"])
    if workflow_rel == trace_rel:
        raise SubmissionError("Workflow and trace must be separate files")
    _committed_file(root, workflow_rel, workflow)
    _committed_file(root, trace_rel, trace)
    _scan_text(json.dumps(manifest), "submission.json")
    _scan_committed_files(root)
    return validate_export(workflow, trace, manifest["mock_url"])


def create_manifest(root: Path, workflow: str, trace: str, mock_url: str,
                    run_command: str, output: Path) -> dict:
    manifest = {"team": TEAM, "members": MEMBERS, "repo": REPO_URL,
                "commit": _git(root, "rev-parse", "HEAD"), "run_command": run_command,
                "workflow": workflow, "trace": trace, "mock_url": mock_url}
    check_manifest(root, manifest)
    output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage", help="copy one matching live export and trace into the repo")
    stage.add_argument("--workflow", type=Path, required=True)
    stage.add_argument("--trace", type=Path, required=True)
    stage.add_argument("--destination", default="submission_artifacts")
    manifest = commands.add_parser("manifest", help="write submission.json after committing artifacts")
    manifest.add_argument("--workflow", required=True, help="committed repo-relative workflow path")
    manifest.add_argument("--trace", required=True, help="committed repo-relative trace path")
    manifest.add_argument("--mock-url", required=True)
    manifest.add_argument("--run-command", required=True)
    manifest.add_argument("--output", type=Path, default=ROOT / "submission.json")
    check = commands.add_parser("check", help="validate a completed submission.json")
    check.add_argument("manifest", type=Path, nargs="?", default=ROOT / "submission.json")
    args = parser.parse_args()
    try:
        if args.command == "stage":
            workflow, trace = stage_artifacts(ROOT, args.workflow, args.trace, args.destination)
            print(json.dumps({"workflow": workflow, "trace": trace}))
        elif args.command == "manifest":
            result = create_manifest(ROOT, args.workflow, args.trace, args.mock_url,
                                     args.run_command, args.output)
            print(f"Wrote {args.output} for commit {result['commit']}")
        else:
            data = json.loads(args.manifest.read_text(encoding="utf-8"))
            workflow_id, agent_id = check_manifest(ROOT, data)
            print(json.dumps({"ok": True, "commit": data["commit"],
                              "workflow_id": workflow_id, "agent_id": agent_id}))
    except (SubmissionError, OSError, json.JSONDecodeError) as error:
        parser.exit(1, f"Submission check failed: {error}\n")


if __name__ == "__main__":
    main()

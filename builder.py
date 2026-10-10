#!/usr/bin/env python3
"""Reproducible bank-brief-to-Freya builder.

    python3 builder.py --brief briefs/brief-a.md --openapi tools/openapi.json \
      --slot brief-a --out out
    python3 builder.py --brief briefs/brief-a.md --brief briefs/brief-b.md \
      --openapi tools/openapi.json --slot brief-b --out out --no-live

The policy and workflow are always generated from the supplied files. Live mode
saves, publishes, exports, and (for recognizable public practice briefs) tests
the agent when the corresponding credentials are present.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

from policy_compiler import compile_policy


MAX_BUILD_SECONDS = 30 * 60
MODEL_TIMEOUT_SECONDS = 4 * 60
KIT = Path(__file__).resolve().parent
_SECRET_PATTERN = re.compile(r"(?:\bfak_[A-Za-z0-9_-]+\b|\bsk-[A-Za-z0-9_-]+\b|\bBearer\s+[A-Za-z0-9._~+/-]+)", re.I)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        for key in ("FREYA_API_KEY", "FREYA_BANK_TEAM_KEY", "ANTHROPIC_API_KEY"):
            secret = os.environ.get(key)
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return _SECRET_PATTERN.sub("[REDACTED]", value)
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Trace:
    def __init__(self, path: Path, started: float) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.started = started
        self.path.write_text("", encoding="utf-8")

    def write(self, event: str, **fields: Any) -> None:
        row = redact({"at": utc_now(), "elapsed_seconds": round(time.monotonic() - self.started, 3), "event": event, **fields})
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _remaining(started: float) -> float:
    remaining = MAX_BUILD_SECONDS - (time.monotonic() - started)
    if remaining <= 0:
        raise TimeoutError("Builder reached its 30-minute limit")
    return remaining


def _model_caller(trace: Trace, started: float):
    claude_executable = shutil.which("claude")
    codex_executable = shutil.which("codex")
    if not claude_executable and not codex_executable:
        trace.write("model_unavailable", reason="Neither Claude nor Codex CLI is installed")
        return None
    claude_model = os.environ.get("BUILDER_MODEL", "sonnet")
    codex_model = os.environ.get("BUILDER_CODEX_MODEL", "gpt-6-sol")
    schema = {
        "type": "object",
        "properties": {
            "global_rules": {"type": "string"},
            "opening": {"type": "string"},
            "identity": {"type": "string"},
            "verified_call": {"type": "string"},
            "outcomes": {"type": "object"},
            "tool_policy": {"type": "object"},
            "raw_brief": {"type": "string"},
            "constraints": {"type": "object"},
        },
        "required": ["global_rules", "opening", "identity", "verified_call", "outcomes", "tool_policy", "raw_brief", "constraints"],
        "additionalProperties": False,
    }

    def call_claude(prompt: str, purpose: str) -> dict[str, Any] | str:
        begun = time.monotonic()
        trace.write("model_call_start", model=claude_model, provider="claude_cli", purpose=purpose)
        command = [
            claude_executable, "--print", "--output-format", "json", "--json-schema", json.dumps(schema),
            "--model", claude_model, "--tools", "", "--no-session-persistence",
            "--system-prompt", "Return only source-grounded structured JSON. Do not use tools or external information.",
        ]
        try:
            result = subprocess.run(
                command, input=prompt, text=True, capture_output=True,
                cwd=KIT, timeout=min(MODEL_TIMEOUT_SECONDS, _remaining(started)), check=False,
            )
        except subprocess.TimeoutExpired as error:
            trace.write("model_call_end", model=claude_model, provider="claude_cli", purpose=purpose, status="timeout", seconds=round(time.monotonic() - begun, 3))
            raise RuntimeError("Claude CLI timed out") from error
        if result.returncode:
            trace.write("model_call_end", model=claude_model, provider="claude_cli", purpose=purpose, status="failed", exit_code=result.returncode, seconds=round(time.monotonic() - begun, 3))
            # Claude's stderr may contain a credential or private brief passage.
            raise RuntimeError(f"Claude CLI exited with status {result.returncode}")
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            trace.write("model_call_end", model=claude_model, provider="claude_cli", purpose=purpose, status="invalid_json", seconds=round(time.monotonic() - begun, 3))
            raise RuntimeError("Claude CLI did not return a JSON envelope") from error
        trace.write(
            "model_call_end", model=claude_model, provider="claude_cli", purpose=purpose, status="ok",
            seconds=round(time.monotonic() - begun, 3),
            input_tokens=(envelope.get("usage") or {}).get("input_tokens"),
            output_tokens=(envelope.get("usage") or {}).get("output_tokens"),
        )
        return envelope.get("structured_output") or envelope.get("result") or envelope

    def call_codex(prompt: str, purpose: str) -> dict[str, Any] | str:
        begun = time.monotonic()
        trace.write("model_call_start", model=codex_model, provider="codex_cli", purpose=purpose)
        with tempfile.TemporaryDirectory(prefix="btb-model-") as directory:
            schema_path = Path(directory) / "schema.json"
            answer_path = Path(directory) / "answer.json"
            # Codex structured output requires fixed properties for every JSON
            # object. Policy subobjects have brief-specific keys, so carry the
            # source-derived policy JSON as a string in a fixed envelope.
            codex_schema = {
                "type": "object",
                "properties": {"policy_json": {"type": "string"}},
                "required": ["policy_json"],
                "additionalProperties": False,
            }
            schema_path.write_text(json.dumps(codex_schema), encoding="utf-8")
            command = [
                codex_executable, "exec", "-s", "read-only", "--skip-git-repo-check",
                "--ephemeral", "--ignore-rules", "-m", codex_model,
                "--output-schema", str(schema_path), "--output-last-message", str(answer_path), "-",
            ]
            try:
                result = subprocess.run(
                    command, input=prompt + "\nDo not use tools. Return a JSON object with one key, policy_json, whose string value is the serialized source-grounded policy JSON object requested above.",
                    text=True, capture_output=True, cwd=KIT,
                    timeout=min(MODEL_TIMEOUT_SECONDS, _remaining(started)), check=False,
                )
            except subprocess.TimeoutExpired as error:
                trace.write("model_call_end", model=codex_model, provider="codex_cli", purpose=purpose, status="timeout", seconds=round(time.monotonic() - begun, 3))
                raise RuntimeError("Codex CLI timed out") from error
            if result.returncode:
                trace.write("model_call_end", model=codex_model, provider="codex_cli", purpose=purpose, status="failed", exit_code=result.returncode, seconds=round(time.monotonic() - begun, 3))
                raise RuntimeError(f"Codex CLI exited with status {result.returncode}")
            answer = answer_path.read_text(encoding="utf-8") if answer_path.exists() else result.stdout
            if not answer.strip():
                trace.write("model_call_end", model=codex_model, provider="codex_cli", purpose=purpose, status="empty", seconds=round(time.monotonic() - begun, 3))
                raise RuntimeError("Codex CLI returned an empty response")
            try:
                policy_json = json.loads(answer)["policy_json"]
            except (ValueError, KeyError, TypeError) as error:
                trace.write("model_call_end", model=codex_model, provider="codex_cli", purpose=purpose, status="invalid_json", seconds=round(time.monotonic() - begun, 3))
                raise RuntimeError("Codex CLI returned an invalid policy envelope") from error
            trace.write("model_call_end", model=codex_model, provider="codex_cli", purpose=purpose, status="ok", seconds=round(time.monotonic() - begun, 3))
            return policy_json

    def call(prompt: str, *, purpose: str) -> dict[str, Any] | str:
        if claude_executable:
            try:
                return call_claude(prompt, purpose)
            except RuntimeError as error:
                trace.write("model_backend_fallback", from_provider="claude_cli", reason=str(error)[:200])
        if codex_executable:
            return call_codex(prompt, purpose)
        raise RuntimeError("No working model backend")

    return call


def _practice_brief(paths: list[Path]) -> str | None:
    names = {p.name.lower() for p in paths}
    if "brief-b.md" in names:
        return "B"
    if "brief-a.md" in names:
        return "A"
    return None


def _validation_failed(result: Any) -> bool:
    if result is False:
        return True
    if isinstance(result, dict):
        if result.get("valid") is False or result.get("ok") is False:
            return True
        if result.get("errors"):
            return True
    if isinstance(result, (list, tuple)) and result and all(isinstance(x, str) for x in result):
        return True
    return False


def _run_practice_test(agent_id: str, brief: str, out: Path, trace: Trace, started: float) -> dict[str, Any]:
    result_path = out / "practice-results.json"
    command = [sys.executable, str(KIT / "tools" / "run_cases.py"), agent_id, "--brief", brief, "--out", str(result_path)]
    begun = time.monotonic()
    trace.write("test_start", brief=brief, agent_id=agent_id)
    try:
        result = subprocess.run(command, cwd=KIT, text=True, capture_output=True, timeout=min(20 * 60, _remaining(started)), check=False)
    except subprocess.TimeoutExpired:
        trace.write("test_end", brief=brief, status="timeout", seconds=round(time.monotonic() - begun, 3))
        return {"status": "timeout"}
    (out / "practice-test.log").write_text(redact(result.stdout + ("\n" + result.stderr if result.stderr else "")), encoding="utf-8")
    summary: dict[str, Any] = {"status": "ok" if result.returncode == 0 else "failed", "exit_code": result.returncode}
    if result_path.exists():
        data = json.loads(result_path.read_text(encoding="utf-8"))
        summary.update({key: data.get(key) for key in ("simulation_id", "iteration_id", "passed", "total", "seconds")})
    trace.write("test_end", brief=brief, agent_id=agent_id, duration_seconds=round(time.monotonic() - begun, 3), **summary)
    return summary


def run(args: argparse.Namespace) -> dict[str, Any]:
    begun = time.monotonic()
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    trace = Trace(out / f"{args.slot}.trace.jsonl", begun)
    briefs = [Path(path).expanduser().resolve() for path in args.brief]
    openapi_path = Path(args.openapi).expanduser().resolve()
    trace.write("start", slot=args.slot, briefs=[str(path) for path in briefs], openapi=str(openapi_path), no_live=args.no_live)
    summary: dict[str, Any] = {"slot": args.slot, "trace": str(trace.path)}
    try:
        openapi = json.loads(openapi_path.read_text(encoding="utf-8"))
        model_call = _model_caller(trace, begun)
        try:
            policy = compile_policy([str(path) for path in briefs], str(openapi_path), model_call)
        except (RuntimeError, ValueError) as error:
            # Keep a complete, source-grounded artifact even when a model account
            # is unavailable or returns invalid structured output.
            trace.write("policy_model_fallback", error_type=type(error).__name__, reason=str(error)[:200])
            policy = compile_policy([str(path) for path in briefs], str(openapi_path), None)
        policy_path = out / f"{args.slot}.policy.json"
        policy_path.write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        trace.write("policy_written", path=str(policy_path), tool_count=len(policy["tool_policy"]), extraction_status=policy.get("constraints", {}).get("extraction_status", "model"))
        summary["policy"] = str(policy_path)

        from workflow_generator import build_workflow
        from qa import validate_workflow

        workflow = build_workflow(policy, openapi)
        if not isinstance(workflow, dict):
            raise TypeError("build_workflow must return a workflow object")
        local_path = out / f"{args.slot}.local.workflow.json"
        local_path.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        trace.write("workflow_written", path=str(local_path), node_count=len(workflow.get("nodes", {})), global_node_count=len(workflow.get("global_nodes", {})))
        summary["local_workflow"] = str(local_path)
        validation = validate_workflow(workflow, openapi)
        if _validation_failed(validation):
            trace.write("validation_failed", result=validation)
            raise ValueError(f"Workflow validation failed: {validation}")
        trace.write("validation_passed", result=validation if isinstance(validation, (str, bool, int, float, type(None))) else "see workflow")

        live = bool(not args.no_live and os.environ.get("FREYA_API_KEY"))
        if live:
            import freya

            _remaining(begun)
            dry_run = freya.validate_workflow(args.slot, workflow)
            trace.write("freya_dry_run", status="ok", result_type=type(dry_run).__name__)
            # Keep slot identity stable even when successive runs use different
            # output directories; Freya has a finite workflow quota.
            state_file = KIT / ".freya-agents.json"
            saved = freya.save_workflow(args.slot, workflow, state_file=str(state_file))
            trace.write("freya_saved_published", workflow_id=saved.get("workflow_id"), agent_id=saved.get("agent_id"), version=saved.get("version"))
            summary.update({key: saved.get(key) for key in ("workflow_id", "agent_id", "version")})
            export_path = out / f"{args.slot}.workflow.json"
            freya.export_workflow(saved["workflow_id"], str(export_path))
            trace.write("freya_exported", path=str(export_path), workflow_id=saved["workflow_id"])
            summary["workflow"] = str(export_path)
            practice = _practice_brief(briefs)
            if not args.no_test and practice and os.environ.get("FREYA_BANK_TEAM_KEY"):
                summary["test"] = _run_practice_test(saved["agent_id"], practice, out, trace, begun)
            else:
                reason = "disabled" if args.no_test else "no matching public practice brief or team key"
                trace.write("test_skipped", reason=reason)
        else:
            workflow_path = out / f"{args.slot}.workflow.json"
            workflow_path.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            trace.write("freya_skipped", reason="--no-live" if args.no_live else "FREYA_API_KEY unavailable", path=str(workflow_path))
            trace.write("test_skipped", reason="live agent unavailable")
            summary["workflow"] = str(workflow_path)
        summary["duration_seconds"] = round(time.monotonic() - begun, 3)
        trace.write("end", status="ok", **summary)
        return summary
    except Exception as error:
        trace.write("end", status="failed", error_type=type(error).__name__, message=str(error)[:1000], duration_seconds=round(time.monotonic() - begun, 3))
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--brief", action="append", required=True, help="Brief path; repeat in precedence order")
    parser.add_argument("--openapi", required=True, help="Bank tool OpenAPI JSON")
    parser.add_argument("--slot", required=True, help="Reusable Freya workflow slot")
    parser.add_argument("--out", required=True, help="Output directory for artifacts and trace")
    parser.add_argument("--no-live", action="store_true", help="Generate locally without Freya save/export/test")
    parser.add_argument("--no-test", action="store_true", help="Save and export but skip the public practice simulation")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", args.slot) or args.slot in {".", ".."}:
        parser.error("--slot must be a simple name using letters, digits, dot, underscore or hyphen")
    try:
        print(json.dumps(run(args), ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(f"builder failed: {type(error).__name__}: {redact(str(error))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

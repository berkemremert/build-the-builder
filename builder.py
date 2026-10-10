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
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any
import urllib.error
import urllib.request

from policy_compiler import compile_policy


MAX_BUILD_SECONDS = 30 * 60
MODEL_TIMEOUT_SECONDS = 900
OPENAI_URL = "https://api.openai.com/v1/responses"
OPENAI_MODEL = "gpt-6-sol"
OPENAI_MAX_OUTPUT_TOKENS = 25_000
KIT = Path(__file__).resolve().parent
_SECRET_PATTERN = re.compile(r"(?:\bfak_[A-Za-z0-9_-]+\b|\bsk-[A-Za-z0-9_-]+\b|\bBearer\s+[A-Za-z0-9._~+/-]+)", re.I)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        for key in ("FREYA_API_KEY", "FREYA_BANK_TEAM_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
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


@contextmanager
def _within_budget(started: float):
    """Bound a blocking Freya step by the remaining build wall-clock budget."""
    remaining = _remaining(started)
    can_alarm = hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
    if not can_alarm:
        try:
            yield
        finally:
            _remaining(started)
        return

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def expired(_signum, _frame):
        raise TimeoutError("Builder reached its 30-minute limit during a Freya step")

    entered = time.monotonic()
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, remaining)
    try:
        yield
        _remaining(started)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, max(0, previous_timer[0] - (time.monotonic() - entered)), previous_timer[1])


def _input_fingerprint(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _new_slot_published_version(freya: Any, workflow_id: str) -> tuple[int, str]:
    """Read back the initial version, which create_workflow does not return."""
    result = freya.api(
        "GET", f"/api/v2/versions/workflow/{workflow_id}",
        workspaceId=freya.workspace_id(),
    )
    versions = result.get("versions") if isinstance(result, dict) else None
    published = [version for version in versions or []
                 if isinstance(version, dict) and version.get("isPublished") is True]
    if len(published) != 1:
        raise RuntimeError(f"Cannot confirm one published version for new workflow {workflow_id}")
    version = published[0]
    number, version_id = version.get("versionNumber"), version.get("id")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1 or not isinstance(version_id, str) or not version_id:
        raise RuntimeError(f"Published version metadata is invalid for new workflow {workflow_id}")
    return number, version_id


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _openai_response(payload: dict[str, Any], key: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        OPENAI_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "build-the-builder/1.0",
        },
        method="POST",
    )
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
        return json.load(response)


class _ModelResponseError(RuntimeError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


def _model_caller(trace: Trace, started: float):
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        trace.write("model_unavailable", provider="openai", reason="OPENAI_API_KEY is missing")
        raise RuntimeError("OPENAI_API_KEY is required for policy extraction")

    def call(prompt: str, *, purpose: str) -> str:
        begun = time.monotonic()
        trace.write("model_call_start", model=OPENAI_MODEL, requested_model=OPENAI_MODEL,
                    provider="openai", purpose=purpose, max_output_tokens=OPENAI_MAX_OUTPUT_TOKENS,
                    reasoning_effort="medium")
        actual_model = None
        response_id = None
        response_status = None
        incomplete_reason = None
        usage: dict[str, Any] = {}
        status = "failed"
        http_status = None
        try:
            payload = {
                "model": OPENAI_MODEL,
                "input": [{"role": "user", "content": prompt}],
                "text": {"format": {"type": "json_object"}},
                "reasoning": {"effort": "medium"},
                "max_output_tokens": OPENAI_MAX_OUTPUT_TOKENS,
                "store": False,
                "stream": False,
            }
            response = _openai_response(payload, key, min(MODEL_TIMEOUT_SECONDS, _remaining(started)))
            if not isinstance(response, dict) or response.get("error"):
                raise _ModelResponseError("invalid_response", "OpenAI returned an error or invalid response")
            response_id = response.get("id") if isinstance(response.get("id"), str) else None
            actual_model = response.get("model")
            if not isinstance(actual_model, str) or not actual_model:
                raise _ModelResponseError("invalid_response", "OpenAI did not identify the model")
            usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
            response_status = response.get("status")
            details = response.get("incomplete_details")
            if isinstance(details, dict):
                incomplete_reason = details.get("reason")
            if response_status != "completed":
                raise _ModelResponseError("incomplete", f"OpenAI policy response did not complete ({response_status or 'unknown'}; {incomplete_reason or 'no reason'})")
            output = response.get("output")
            if not isinstance(output, list):
                raise _ModelResponseError("invalid_response", "OpenAI returned no output items")
            parts = []
            for item in output:
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                for content in item.get("content") or []:
                    if isinstance(content, dict) and content.get("type") == "output_text" and isinstance(content.get("text"), str):
                        parts.append(content["text"])
            text = "".join(parts)
            if not text.strip():
                raise _ModelResponseError("empty", "OpenAI returned no policy text")
            status = "ok"
            return text
        except _ModelResponseError as error:
            status = error.status
            raise
        except urllib.error.HTTPError as error:
            status = "http_error"
            http_status = error.code
            error.close()
            raise RuntimeError(f"OpenAI HTTP {error.code} during policy extraction") from error
        except (TimeoutError, ConnectionError) as error:
            status = "timeout" if isinstance(error, TimeoutError) else "network_error"
            raise RuntimeError(f"OpenAI policy request {status}") from error
        except urllib.error.URLError as error:
            status = "network_error"
            raise RuntimeError("OpenAI policy request failed on the network") from error
        except (ValueError, TypeError) as error:
            status = "invalid_response"
            raise RuntimeError("OpenAI returned malformed response JSON") from error
        finally:
            output_details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
            trace.write("model_call_end", model=OPENAI_MODEL, requested_model=OPENAI_MODEL,
                        actual_model=actual_model, provider="openai", purpose=purpose, status=status,
                        seconds=round(time.monotonic() - begun, 3), response_id=response_id,
                        response_status=response_status, incomplete_reason=incomplete_reason,
                        http_status=http_status, input_tokens=usage.get("input_tokens"),
                        output_tokens=usage.get("output_tokens"),
                        reasoning_tokens=output_details.get("reasoning_tokens"))

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
    result_path.unlink(missing_ok=True)
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
        passed, total = data.get("passed"), data.get("total")
        if not isinstance(passed, int) or not isinstance(total, int) or total <= 0 or passed != total:
            summary["status"] = "failed"
    else:
        summary["status"] = "failed"
        summary["reason"] = "practice runner produced no result file"
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
        trace.write("inputs", briefs=[_input_fingerprint(path) for path in briefs], openapi=_input_fingerprint(openapi_path))
        openapi = json.loads(openapi_path.read_text(encoding="utf-8"))
        model_call = _model_caller(trace, begun)
        model_degraded = model_call is None
        try:
            policy = compile_policy([str(path) for path in briefs], str(openapi_path), model_call)
        except (RuntimeError, ValueError) as error:
            trace.write("policy_compile_failed", error_type=type(error).__name__, reason=str(error)[:200])
            raise
        trace.write("policy_validation_passed", key_count=len(policy),
                    tool_count=len(policy["tool_policy"]))
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
            if model_degraded or policy.get("constraints", {}).get("extraction_status") == "source_only":
                trace.write("publish_blocked", reason="source-only policy after unavailable or failed model extraction")
                raise RuntimeError("Refusing to publish a source-only policy; restore model extraction and rerun")
            import freya

            with _within_budget(begun):
                dry_run = freya.validate_workflow(args.slot, workflow)
            trace.write("freya_dry_run", status="ok", result_type=type(dry_run).__name__)
            # Keep slot identity stable even when successive runs use different
            # output directories; Freya has a finite workflow quota.
            state_file = KIT / ".freya-agents.json"
            with _within_budget(begun):
                saved = freya.save_workflow(args.slot, workflow, state_file=str(state_file))
            published_version = saved.get("version")
            version_id = None
            if published_version is None:
                trace.write("freya_created", workflow_id=saved.get("workflow_id"), agent_id=saved.get("agent_id"))
                with _within_budget(begun):
                    published_version, version_id = _new_slot_published_version(freya, saved["workflow_id"])
            trace.write("freya_saved_published", workflow_id=saved.get("workflow_id"), agent_id=saved.get("agent_id"), version=published_version, version_id=version_id)
            summary.update({"workflow_id": saved.get("workflow_id"), "agent_id": saved.get("agent_id"), "version": published_version})
            export_path = out / f"{args.slot}.workflow.json"
            with _within_budget(begun):
                freya.export_workflow(saved["workflow_id"], str(export_path))
            trace.write("freya_exported", path=str(export_path), workflow_id=saved["workflow_id"])
            summary["workflow"] = str(export_path)
            practice = _practice_brief(briefs)
            if not args.no_test and practice and os.environ.get("FREYA_BANK_TEAM_KEY"):
                summary["test"] = _run_practice_test(saved["agent_id"], practice, out, trace, begun)
                if summary["test"]["status"] != "ok":
                    raise RuntimeError(f"Practice test failed: {summary['test'].get('passed', '?')}/{summary['test'].get('total', '?')} passed")
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

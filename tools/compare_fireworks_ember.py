"""Run one offline Ember policy extraction and retain the attempt record.

Run from the kit root after loading FIREWORKS_API_KEY into the environment.
This does not publish or simulate a workflow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from policy_compiler import compile_policy
from compare_fireworks_policy import API_URL, BRIEFS, _grounding_flags

MODEL = "accounts/fireworks/models/ember-1"


def run(brief: str, attempt: str, reasoning_effort: str, max_tokens: int, timeout: int, out: Path) -> dict:
    key = os.environ.get("FIREWORKS_API_KEY")
    if not key:
        raise RuntimeError("FIREWORKS_API_KEY is missing")
    out.mkdir(parents=True, exist_ok=True)
    stem = f"ember-1-{brief.lower()}-{attempt}"
    record = {
        "requested_model": MODEL,
        "brief": brief,
        "attempt": attempt,
        "settings": {
            "reasoning_effort": reasoning_effort,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "timeout_seconds": timeout,
        },
        "actual_model": None,
        "latency_seconds": None,
        "usage": None,
        "finish_reason": None,
        "json_valid": False,
        "schema_valid": False,
        "grounding_flags": [],
    }
    started = time.monotonic()

    def call_model(prompt: str, *, purpose: str) -> str:
        record["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        record["prompt_chars"] = len(prompt)
        body = {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "reasoning_effort": reasoning_effort,
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
        }
        request = urllib.request.Request(
            API_URL,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            raw = error.read(4096).decode("utf-8", errors="replace")
            raise RuntimeError(f"Fireworks HTTP {error.code}: {raw.replace(key, '[redacted]')[:1000]}") from None
        record["request_id"] = result.get("id")
        record["actual_model"] = result.get("model")
        record["usage"] = result.get("usage")
        choice = (result.get("choices") or [{}])[0]
        record["finish_reason"] = choice.get("finish_reason")
        message = choice.get("message") or {}
        content = message.get("content")
        reasoning = message.get("reasoning_content")
        record["reasoning_chars"] = len(reasoning) if isinstance(reasoning, str) else 0
        if not isinstance(content, str):
            raise ValueError("Fireworks returned no text content")
        (out / f"{stem}.response.txt").write_text(content, encoding="utf-8")
        record["response_chars"] = len(content)
        try:
            record["json_valid"] = isinstance(json.loads(content), dict)
        except json.JSONDecodeError:
            pass
        return content

    try:
        policy = compile_policy([str(path) for path in BRIEFS[brief]], str(ROOT / "tools/openapi.json"), call_model)
        record["schema_valid"] = True
        record["grounding_flags"] = _grounding_flags(policy, brief)
        (out / f"{stem}.policy.json").write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as error:
        record["error"] = f"{type(error).__name__}: {error}".replace(key, "[redacted]")[:1200]
    finally:
        record["latency_seconds"] = round(time.monotonic() - started, 3)
        results_path = out / "ember-results.json"
        records = json.loads(results_path.read_text(encoding="utf-8")) if results_path.exists() else []
        records.append(record)
        results_path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brief", choices=BRIEFS, required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high"), default="low")
    parser.add_argument("--max-tokens", type=int, default=32768)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--out", type=Path, default=ROOT / "out/fireworks-policy-comparison")
    args = parser.parse_args()
    record = run(args.brief, args.attempt, args.reasoning_effort, args.max_tokens, args.timeout, args.out.resolve())
    print(json.dumps(record, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()

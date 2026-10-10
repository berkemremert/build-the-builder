"""Offline GPT-6 Sol Responses API policy extraction for Bank A or B.

Load OPENAI_API_KEY from ignored env.sh before running. This harness does not
save, publish, or simulate Freya workflows.
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
from compare_fireworks_policy import BRIEFS, _grounding_flags

API_URL = "https://api.openai.com/v1/responses"
MODEL = "gpt-6-sol"
PRICING_URL = "https://developers.openai.com/api/docs/models/gpt-6-sol"
INPUT_PER_MILLION_USD = 2.00
CACHED_INPUT_PER_MILLION_USD = 0.20
CACHE_WRITE_PER_MILLION_USD = 2.50
OUTPUT_PER_MILLION_USD = 10.00


def _output_text(response: dict) -> str:
    parts: list[str] = []
    for item in response.get("output") or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "".join(parts)


def _estimated_cost(usage: dict | None) -> float | None:
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    details = usage.get("input_tokens_details") or {}
    cached = details.get("cached_tokens") or 0
    cache_write = details.get("cache_write_tokens") or 0
    uncached = max(0, input_tokens - cached - cache_write)
    return round((uncached * INPUT_PER_MILLION_USD + cached * CACHED_INPUT_PER_MILLION_USD
                  + cache_write * CACHE_WRITE_PER_MILLION_USD
                  + output_tokens * OUTPUT_PER_MILLION_USD) / 1_000_000, 6)


def run(brief: str, out: Path, timeout: int, max_output_tokens: int) -> dict:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is missing")
    out.mkdir(parents=True, exist_ok=True)
    stem = f"gpt-6-sol-{brief.lower()}-medium"
    record = {
        "requested_model": MODEL,
        "brief": brief,
        "settings": {
            "endpoint": API_URL,
            "reasoning": {"effort": "medium"},
            "text": {"format": {"type": "json_object"}},
            "max_output_tokens": max_output_tokens,
            "stream": False,
            "store": False,
            "timeout_seconds": timeout,
        },
        "actual_model": None,
        "latency_seconds": None,
        "status": None,
        "incomplete_reason": None,
        "usage": None,
        "estimated_cost_usd": None,
        "pricing_source": PRICING_URL,
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
            "reasoning": {"effort": "medium"},
            "text": {"format": {"type": "json_object"}},
            "input": [{"role": "user", "content": prompt}],
            "max_output_tokens": max_output_tokens,
            "stream": False,
            "store": False,
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
            raw = error.read(4096).decode("utf-8", errors="replace").replace(key, "[redacted]")
            raise RuntimeError(f"OpenAI HTTP {error.code}: {raw[:1000]}") from None
        record["response_id"] = result.get("id")
        record["actual_model"] = result.get("model")
        record["status"] = result.get("status")
        record["incomplete_reason"] = (result.get("incomplete_details") or {}).get("reason")
        record["usage"] = result.get("usage")
        record["estimated_cost_usd"] = _estimated_cost(result.get("usage"))
        content = _output_text(result)
        (out / f"{stem}.response.txt").write_text(content, encoding="utf-8")
        record["response_chars"] = len(content)
        if not content:
            raise ValueError(f"OpenAI returned no output text (status={record['status']}, reason={record['incomplete_reason']})")
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
        results_path = out / "results.json"
        records = json.loads(results_path.read_text(encoding="utf-8")) if results_path.exists() else []
        records.append(record)
        results_path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brief", choices=BRIEFS, required=True)
    parser.add_argument("--out", type=Path, default=ROOT / "out/openai-policy-comparison")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--max-output-tokens", type=int, default=25000)
    args = parser.parse_args()
    record = run(args.brief, args.out.resolve(), args.timeout, args.max_output_tokens)
    print(json.dumps(record, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()

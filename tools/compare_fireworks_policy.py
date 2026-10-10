"""Offline Fireworks comparison using the production policy compiler prompt.

Run from the kit root after exporting FIREWORKS_API_KEY from ignored env.sh:
    set -a; source env.sh; set +a
    python3 tools/compare_fireworks_policy.py --out out/fireworks-policy-comparison

This script only calls Fireworks. It does not build, publish, or test agents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from policy_compiler import compile_policy


API_URL = "https://api.fireworks.ai/inference/v1/chat/completions"
MODELS = (
    "accounts/fireworks/models/qwen3p8-max",
    "accounts/fireworks/models/kimi-k3",
    "accounts/fireworks/models/glm-5p3",
    "accounts/fireworks/models/ember-1",
)
BRIEFS = {
    "A": (ROOT / "briefs/brief-a.md",),
    "B": (ROOT / "briefs/brief-a.md", ROOT / "briefs/brief-b.md"),
}


def _request(model: str, prompt: str, timeout: int) -> dict:
    key = os.environ.get("FIREWORKS_API_KEY")
    if not key:
        raise RuntimeError("FIREWORKS_API_KEY is missing")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 12000,
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
            return json.load(response)
    except urllib.error.HTTPError as error:
        raw = error.read(4096).decode("utf-8", errors="replace")
        # The credential is header-only; this additional redaction keeps an
        # unexpected server echo out of local artifacts and terminal output.
        raise RuntimeError(f"Fireworks HTTP {error.code}: {raw.replace(key, '[redacted]')[:1000]}") from None


def _grounding_flags(policy: dict, brief: str) -> list[str]:
    """Conservative checks; each flag is a review lead, not a semantic verdict."""
    text = json.dumps({key: value for key, value in policy.items() if key != "raw_brief"}, ensure_ascii=False).lower()
    flags = []

    def require(label: str, *patterns: str) -> None:
        if not all(re.search(pattern, text) for pattern in patterns):
            flags.append("possible omission: " + label)

    require("callback window 10:00–19:00", r"10:00", r"19:00")
    require("three wrong identity answers", r"three|3", r"wrong|failed|incorrect")
    require("early/late business-day deadlines", r"12(?:th)?\s+(?:business|working)", r"7(?:th)?\s+(?:business|working)")
    require("October 26 bank holiday", r"2026-10-26")
    require("exact promise SMS sentence", re.escape("you will receive a text message confirming this promise."))
    require("confirm before create_promise", r"confirm", r"create_promise")
    if brief == "B":
        require("individual product deadlines", r"per.product|each product", r"deadline")
        require("shared date capped per product", r"shared|same|one date", r"deadline")
        require("single combined confirmation", r"all promises|all products|together|combined", r"confirm")
        require("note unpromised products", r"add_note", r"without a promise|no promise|unpromised")
        require("no notes on transfer", r"transfer", r"no notes|without notes|do not.*note")
    source_dates = set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", "\n".join(path.read_text(encoding="utf-8") for path in BRIEFS[brief])))
    invented_dates = sorted(set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)) - source_dates)
    if invented_dates:
        flags.append("dates absent from source: " + ", ".join(invented_dates[:8]))
    return flags


def _one(model: str, brief: str, out: Path, timeout: int) -> dict:
    slug = model.rsplit("/", 1)[-1]
    stem = f"{slug}-{brief.lower()}"
    record: dict = {"requested_model": model, "brief": brief, "response_model": None,
                    "latency_seconds": None, "usage": None, "finish_reason": None,
                    "json_valid": False, "schema_valid": False, "grounding_flags": []}
    started = time.monotonic()

    def call_model(prompt: str, *, purpose: str) -> str:
        record["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        record["prompt_chars"] = len(prompt)
        response = _request(model, prompt, timeout)
        record["response_model"] = response.get("model")
        record["usage"] = response.get("usage")
        choice = (response.get("choices") or [{}])[0]
        record["finish_reason"] = choice.get("finish_reason")
        content = (choice.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise ValueError("Fireworks returned no text content")
        (out / f"{stem}.response.txt").write_text(content, encoding="utf-8")
        record["response_chars"] = len(content)
        try:
            record["json_valid"] = isinstance(json.loads(content), dict)
        except json.JSONDecodeError:
            record["json_valid"] = False
        return content

    try:
        policy = compile_policy([str(path) for path in BRIEFS[brief]], str(ROOT / "tools/openapi.json"), call_model)
        record["schema_valid"] = True
        record["grounding_flags"] = _grounding_flags(policy, brief)
        (out / f"{stem}.policy.json").write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as error:
        key = os.environ.get("FIREWORKS_API_KEY", "")
        record["error"] = (f"{type(error).__name__}: {error}").replace(key, "[redacted]")[:1200] if key else f"{type(error).__name__}: {error}"[:1200]
    finally:
        record["latency_seconds"] = round(time.monotonic() - started, 3)
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "out/fireworks-policy-comparison")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    records = []
    for model in MODELS:
        for brief in BRIEFS:
            print(f"Running {model.rsplit('/', 1)[-1]} / Bank {brief}...", flush=True)
            record = _one(model, brief, out, args.timeout)
            records.append(record)
            (out / "results.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"  {record['latency_seconds']}s; JSON={record['json_valid']}; schema={record['schema_valid']}; "
                  f"model={record['response_model'] or 'unknown'}; "
                  f"error={record.get('error', 'none')[:180]}", flush=True)
    print(out / "results.json", flush=True)


if __name__ == "__main__":
    main()

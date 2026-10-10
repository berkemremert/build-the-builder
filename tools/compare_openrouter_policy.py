"""Compare OpenRouter policy extraction with the production compiler.

Dry run (no network, no credentials needed):
    python3 tools/compare_openrouter_policy.py

After direct approval to send briefs/brief-a.md, briefs/brief-b.md, and
tools/openapi.json to OpenRouter and the models selected by both routers:
    set -a; source env.sh; set +a
    python3 tools/compare_openrouter_policy.py --live

Direct Claude Opus 5.5 comparison, after separate approval:
    python3 tools/compare_openrouter_policy.py --live \\
      --only-model anthropic/claude-opus-5.5 --no-price-ceiling \\
      --reasoning-effort medium --max-completion-tokens 20000

This script never builds, publishes, or simulates a Freya workflow.
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
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from policy_compiler import compile_policy  # noqa: E402


API_URL = "https://openrouter.ai/api/v1/chat/completions"
GENERATION_URL = "https://openrouter.ai/api/v1/generation"
CREDITS_URL = "https://openrouter.ai/api/v1/credits"
KEY_URL = "https://openrouter.ai/api/v1/key"
MODELS = ("openrouter/auto", "typesafe/jev-router")
DIRECT_MODELS = ("anthropic/claude-opus-5.5",)
PLUGINS = {
    "openrouter/auto": {"id": "auto-router", "cost_tier": "max"},
    "typesafe/jev-router": {"id": "jev-router", "cost_tier": "high"},
}
BRIEFS = {
    "A": (ROOT / "briefs/brief-a.md",),
    "B": (ROOT / "briefs/brief-a.md", ROOT / "briefs/brief-b.md"),
}
OPENAPI = ROOT / "tools/openapi.json"
# OpenRouter provider maximum prices in USD per million tokens.
# The preflight counts every UTF-8 input byte as a token and adds 10% headroom.
DEFAULT_MAX_PROMPT_PRICE = 10.0
DEFAULT_MAX_COMPLETION_PRICE = 50.0
DIRECT_RATES = {"anthropic/claude-opus-5.5": (4.0, 20.0)}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _safe_error(error: Exception, key: str) -> str:
    if isinstance(error, urllib.error.HTTPError):
        try:
            body = json.loads(error.read(4096))
            detail = body.get("error", {}).get("message", "")
        except (ValueError, AttributeError, TypeError):
            detail = ""
        message = f"OpenRouter HTTP {error.code}: {detail}"
    else:
        message = f"{type(error).__name__}: {error}"
    return message.replace(key, "[redacted]")[:800] if key else message[:800]


def _request_json(url: str, key: str, *, payload: dict | None, timeout: int) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "X-OpenRouter-Metadata": "enabled",
        },
        method="POST" if payload is not None else "GET",
    )
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("OpenRouter response is not a JSON object")
    return result


def _request_body(model: str, prompt: str, max_completion_tokens: int,
                  max_prompt_price: float | None, max_completion_price: float | None,
                  require_parameters: bool, reasoning_effort: str) -> dict:
    provider = {}
    if max_prompt_price is not None and max_completion_price is not None:
        provider["max_price"] = {"prompt": max_prompt_price,
                                 "completion": max_completion_price}
    if require_parameters:
        provider["require_parameters"] = True
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "reasoning": {"effort": reasoning_effort},
        "max_completion_tokens": max_completion_tokens,
        "stream": False,
    }
    if model in PLUGINS:
        body["plugins"] = [PLUGINS[model]]
    if provider:
        body["provider"] = provider
    return body


def _available_credit(key: str, timeout: int) -> float:
    account = _request_json(CREDITS_URL, key, payload=None, timeout=timeout).get("data", {})
    current_key = _request_json(KEY_URL, key, payload=None, timeout=timeout).get("data", {})
    account_remaining = float(account["total_credits"]) - float(account["total_usage"])
    key_remaining = current_key.get("limit_remaining")
    return min(account_remaining, float(key_remaining)) if key_remaining is not None else account_remaining


def _maximum_cost(models: tuple[str, ...], max_completion_tokens: int,
                  max_prompt_price: float | None, max_completion_price: float | None) -> float:
    prompt_bytes = {}
    for brief, paths in BRIEFS.items():
        def capture(prompt: str, *, purpose: str) -> dict:
            prompt_bytes[brief] = len(prompt.encode("utf-8"))
            return {}
        try:
            compile_policy([str(path) for path in paths], str(OPENAPI), capture)
        except ValueError:
            pass  # The capture callback only needs the production prompt.
        if brief not in prompt_bytes:
            raise RuntimeError(f"Could not capture Bank {brief} prompt for budget preflight")
    total = sum((prompt_bytes[brief] * (max_prompt_price if max_prompt_price is not None else DIRECT_RATES[model][0])
                 + max_completion_tokens * (max_completion_price if max_completion_price is not None else DIRECT_RATES[model][1])) / 1_000_000
                for model in models for brief in BRIEFS)
    return total * 1.1


def _grounding_flags(policy: dict, brief: str) -> list[str]:
    """Conservative review leads, not a semantic pass/fail judgment."""
    text = json.dumps({key: value for key, value in policy.items() if key != "raw_brief"},
                      ensure_ascii=False).lower()
    flags: list[str] = []

    def require(label: str, *patterns: str) -> None:
        if not all(re.search(pattern, text) for pattern in patterns):
            flags.append("possible omission: " + label)

    require("recording disclosure on opening", r"recorded|recording", r"digital assistant")
    require("callback hours", r"10:00", r"19:00")
    require("three wrong security answers", r"three|3", r"wrong|failed|incorrect")
    require("early and late deadlines", r"12(?:th)?\s+(?:business|working)",
            r"7(?:th)?\s+(?:business|working)")
    require("bank holiday October 26", r"2026-10-26")
    require("exact promise SMS sentence", re.escape("you will receive a text message confirming this promise."))
    require("confirmation before promise write", r"confirm", r"create_promise")
    if brief == "B":
        require("all products stated before payment question", r"all|every", r"products", r"before")
        require("all today then returned product order", r"all.{0,80}today", r"order|one by one")
        require("per-product attempts and deadlines", r"per.product|each product", r"attempt", r"deadline")
        require("shared date capped per product", r"shared|same|one date", r"deadline")
        require("one combined confirmation", r"together|combined|all promises", r"confirm")
        require("notes for products without promises", r"add_note", r"without a promise|no promise|unpromised")
        require("no notes after transfer", r"transfer", r"no notes|without notes|do not.*note")
    source = "\n".join(path.read_text(encoding="utf-8") for path in BRIEFS[brief])
    invented = sorted(set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text))
                      - set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", source)))
    if invented:
        flags.append("dates absent from source: " + ", ".join(invented[:8]))
    return flags


def _generation_metadata(response: dict, key: str, timeout: int) -> dict:
    metadata = response.get("openrouter_metadata") or {}
    result: dict = {}
    if isinstance(metadata, dict):
        for field in ("provider_name", "provider", "upstream_provider"):
            value = metadata.get(field)
            if isinstance(value, str) and value:
                result["provider_name"] = value
                break
    generation_id = response.get("id")
    if not isinstance(generation_id, str) or not generation_id:
        return result
    url = GENERATION_URL + "?" + urllib.parse.urlencode({"id": generation_id})
    try:
        data = _request_json(url, key, payload=None, timeout=min(timeout, 15)).get("data") or {}
        if isinstance(data, dict):
            if isinstance(data.get("provider_name"), str):
                result["provider_name"] = data["provider_name"]
            if isinstance(data.get("total_cost"), (int, float)):
                result["cost_usd"] = data["total_cost"]
        return result
    except (OSError, ValueError):
        return result


def _one(model: str, brief: str, out: Path, timeout: int,
         max_completion_tokens: int, max_prompt_price: float | None,
         max_completion_price: float | None, require_parameters: bool,
         reasoning_effort: str, key: str) -> dict:
    slug = model.replace("/", "-")
    stem = f"{slug}-{brief.lower()}"
    record: dict = {"requested_model": model, "brief": brief, "response_model": None,
                    "provider_name": None, "completion_latency_seconds": None,
                    "latency_seconds": None, "usage": None, "cost_usd": None,
                    "prompt_tokens": None, "completion_tokens": None,
                    "reasoning_tokens": None,
                    "finish_reason": None, "json_valid": False, "schema_valid": False,
                    "grounding_flags": [], "grounding_status": "unassessed: invalid policy"}
    record["request_config"] = {"max_completion_tokens": max_completion_tokens,
                                "max_prompt_price": max_prompt_price,
                                "max_completion_price": max_completion_price,
                                "require_parameters": require_parameters,
                                "reasoning_effort": reasoning_effort,
                                "timeout_seconds": timeout,
                                "plugin": PLUGINS.get(model)}
    started = time.monotonic()

    def call_model(prompt: str, *, purpose: str) -> str:
        record["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        record["prompt_chars"] = len(prompt)
        request_started = time.monotonic()
        try:
            response = _request_json(API_URL, key,
                                     payload=_request_body(model, prompt, max_completion_tokens,
                                                           max_prompt_price, max_completion_price,
                                                           require_parameters, reasoning_effort), timeout=timeout)
        finally:
            record["completion_latency_seconds"] = round(time.monotonic() - request_started, 3)
        record["response_model"] = response.get("model")
        record["usage"] = response.get("usage")
        record.update(_generation_metadata(response, key, timeout))
        usage = record["usage"]
        if isinstance(usage, dict) and isinstance(usage.get("cost"), (int, float)):
            record["cost_usd"] = usage["cost"]
        if isinstance(usage, dict):
            record["prompt_tokens"] = usage.get("prompt_tokens")
            record["completion_tokens"] = usage.get("completion_tokens")
            detail = usage.get("completion_tokens_details")
            if isinstance(detail, dict):
                record["reasoning_tokens"] = detail.get("reasoning_tokens")
        choice = (response.get("choices") or [{}])[0]
        record["finish_reason"] = choice.get("finish_reason")
        if record["finish_reason"] == "length":
            raise ValueError("OpenRouter response reached the completion-token allowance")
        content = (choice.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise ValueError("OpenRouter returned no text content")
        (out / f"{stem}.response.txt").write_text(content, encoding="utf-8")
        record["response_chars"] = len(content)
        try:
            record["json_valid"] = isinstance(json.loads(content), dict)
        except json.JSONDecodeError:
            pass
        return content

    try:
        policy = compile_policy([str(path) for path in BRIEFS[brief]], str(OPENAPI), call_model)
        record["schema_valid"] = True
        record["grounding_flags"] = _grounding_flags(policy, brief)
        record["grounding_status"] = "heuristic review complete"
        (out / f"{stem}.policy.json").write_text(
            json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as error:
        record["error"] = _safe_error(error, key)
    finally:
        record["latency_seconds"] = round(time.monotonic() - started, 3)
    return record


def _dry_run(max_completion_tokens: int) -> None:
    for brief, paths in BRIEFS.items():
        fallback = compile_policy([str(path) for path in paths], str(OPENAPI), None)
        captured: dict = {}

        def capture(prompt: str, *, purpose: str) -> dict:
            captured["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            captured["prompt_chars"] = len(prompt)
            captured["has_complete_openapi"] = json.dumps(
                json.loads(OPENAPI.read_text(encoding="utf-8")), ensure_ascii=False) in prompt
            captured["has_all_briefs"] = all(path.read_text(encoding="utf-8").strip() in prompt for path in paths)
            captured["request_models"] = len(MODELS)
            captured["output_cap"] = max_completion_tokens
            return dict(fallback, raw_brief="")

        compile_policy([str(path) for path in paths], str(OPENAPI), capture)
        if not captured["has_complete_openapi"] or not captured["has_all_briefs"]:
            raise AssertionError("Production prompt omitted an input source")
        print(f"Bank {brief}: {json.dumps(captured, sort_keys=True)}")
    print("Dry run complete; no network requests were made.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Send the source documents to OpenRouter")
    parser.add_argument("--out", type=Path, default=ROOT / "out/openrouter-policy-comparison")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--max-completion-tokens", type=int, default=4500)
    parser.add_argument("--max-budget-usd", type=float, default=1.90)
    parser.add_argument("--reserve-usd", type=float, default=3.0)
    parser.add_argument("--max-prompt-price", type=float, default=DEFAULT_MAX_PROMPT_PRICE)
    parser.add_argument("--max-completion-price", type=float, default=DEFAULT_MAX_COMPLETION_PRICE)
    parser.add_argument("--no-price-ceiling", action="store_true")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high"), default="low")
    parser.add_argument("--require-parameters", action="store_true")
    parser.add_argument("--only-model", choices=MODELS + DIRECT_MODELS)
    args = parser.parse_args()
    if (args.timeout < 1 or args.max_completion_tokens < 1 or args.max_budget_usd <= 0
            or args.reserve_usd < 0 or args.max_prompt_price <= 0 or args.max_completion_price <= 0):
        parser.error("timeout, max-completion-tokens, and budget must be positive; reserve must be nonnegative")
    if not args.live:
        _dry_run(args.max_completion_tokens)
        return
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        parser.error("OPENROUTER_API_KEY is missing")
    models = (args.only_model,) if args.only_model else MODELS
    if args.no_price_ceiling and any(model not in DIRECT_RATES for model in models):
        parser.error("A no-ceiling budget estimate requires a priced direct model")
    max_prompt_price = None if args.no_price_ceiling else args.max_prompt_price
    max_completion_price = None if args.no_price_ceiling else args.max_completion_price
    maximum_cost = _maximum_cost(models, args.max_completion_tokens,
                                 max_prompt_price, max_completion_price)
    if maximum_cost > args.max_budget_usd:
        parser.error(f"Estimated upper cost ${maximum_cost:.2f} exceeds ${args.max_budget_usd:.2f} cap")
    try:
        opening_credit = _available_credit(key, min(args.timeout, 15))
    except Exception as error:
        parser.error("Could not verify OpenRouter credit: " + _safe_error(error, key))
    if opening_credit < args.max_budget_usd + args.reserve_usd:
        parser.error(f"Available credit ${opening_credit:.2f} cannot cover the cap and reserve")
    print(f"Budget preflight: at most ${maximum_cost:.2f} estimated; "
          f"${args.max_budget_usd:.2f} cap; ${args.reserve_usd:.2f} reserve.", flush=True)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    records = []
    for model in models:
        for brief in BRIEFS:
            try:
                remaining_credit = _available_credit(key, min(args.timeout, 15))
            except Exception as error:
                parser.error("Could not recheck OpenRouter credit: " + _safe_error(error, key))
            if remaining_credit < opening_credit - args.max_budget_usd or remaining_credit < args.reserve_usd:
                parser.error("Stopped before another request: spending cap or reserve reached")
            print(f"Running {model} / Bank {brief}...", flush=True)
            record = _one(model, brief, out, args.timeout, args.max_completion_tokens,
                          max_prompt_price, max_completion_price,
                          args.require_parameters, args.reasoning_effort, key)
            try:
                after_credit = _available_credit(key, min(args.timeout, 15))
                record["credit_delta_usd"] = round(max(0.0, remaining_credit - after_credit), 6)
                if record["cost_usd"] is None:
                    record["cost_usd"] = record["credit_delta_usd"]
            except Exception as error:
                record["credit_error"] = _safe_error(error, key)
            records.append(record)
            (out / "results.json").write_text(
                json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"  {record['latency_seconds']}s; JSON={record['json_valid']}; "
                  f"schema={record['schema_valid']}; provider={record['provider_name'] or 'unknown'}; "
                  f"error={record.get('error', 'none')[:180]}", flush=True)
    print(out / "results.json", flush=True)


if __name__ == "__main__":
    main()

"""Compile arbitrary bank briefs into a source-grounded policy for the workflow builder.

The model is an extractor, not a source of bank rules.  Every run retains the
complete input text so downstream generation can resolve unusual document forms
and later overrides without relying on facts baked into this module.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Callable


REQUIRED_KEYS = (
    "global_rules", "opening", "identity", "verified_call", "outcomes",
    "tool_policy", "raw_brief", "constraints",
)


def _tool_names(openapi: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for path, operations in openapi.get("paths", {}).items():
        for method in ("post", "get", "put", "patch", "delete"):
            operation = operations.get(method)
            if isinstance(operation, dict):
                names.append(operation.get("operationId") or path.rstrip("/").split("/")[-1])
    return list(dict.fromkeys(names))


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        if isinstance(value.get("structured_output"), dict):
            return value["structured_output"]
        if isinstance(value.get("result"), str):
            return _json_object(value["result"])
        return value
    if not isinstance(value, str):
        raise ValueError("Policy model returned neither text nor an object")
    value = value.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        start, end = value.find("{"), value.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Policy model returned no JSON object") from None
        result = json.loads(value[start:end + 1])
    if not isinstance(result, dict):
        raise ValueError("Policy model JSON is not an object")
    return result


def _source_fallback(raw_brief: str, tool_names: list[str]) -> dict[str, Any]:
    """Lossless fallback when an authenticated model is unavailable.

    No guessed policy facts enter the output: the workflow generator receives the
    entire brief in ``raw_brief`` and the OpenAPI-derived tool names.
    """
    sections: list[tuple[str, str]] = []
    for piece in re.split(r"(?=^#{1,4}\s+\S)", raw_brief, flags=re.MULTILINE):
        piece = piece.strip()
        if piece:
            heading = piece.splitlines()[0].lower()
            sections.append((heading, piece))

    def extract(*terms: str) -> str:
        selected = [body for heading, body in sections if any(term in heading for term in terms)]
        # Unstructured source has no reliable stage boundaries. Retain it whole.
        return "\n\n".join(selected) if selected else raw_brief

    lines = raw_brief.splitlines()
    return {
        "global_rules": raw_brief,
        "opening": extract("opening", "who is on the line", "contact", "someone else", "third party", "callback"),
        "identity": extract("identity", "security", "verification"),
        "verified_call": extract("debt", "date", "amount", "promise", "product", "confirm", "record", "what changes", "other things"),
        "outcomes": {"source_text": extract("closing", "outcome")},
        "tool_policy": {name: "\n".join(line for line in lines if name in line) for name in tool_names},
        "raw_brief": raw_brief,
        "constraints": {"extraction_status": "source_only", "source_text": extract("date", "amount", "calendar", "limit", "what changes")},
    }


def compile_policy(
    brief_paths: list[str],
    openapi_path: str,
    call_model: Callable[..., Any] | None,
) -> dict[str, Any]:
    """Return a policy with the stable keys consumed by ``build_workflow``.

    ``call_model`` is called once as ``call_model(prompt, purpose=...)``. It may
    return a JSON object or JSON text. An unavailable model triggers a lossless
    source-only fallback; a malformed model response raises, so a bad extraction
    never silently masquerades as a compiled policy.
    """
    if not brief_paths:
        raise ValueError("At least one brief is required")
    openapi = json.loads(Path(openapi_path).read_text(encoding="utf-8"))
    names = _tool_names(openapi)
    if not names:
        raise ValueError("OpenAPI file contains no operations")

    parts = []
    for path in brief_paths:
        file = Path(path)
        parts.append(f"\n===== SOURCE: {file.name} =====\n{file.read_text(encoding='utf-8')}\n")
    raw_brief = "".join(parts).strip()
    if call_model is None:
        return _source_fallback(raw_brief, names)

    # Pass the complete API source. Filtering operations previously discarded
    # path/query/header parameters, server overrides, security requirements,
    # and component schemas referenced by $ref. Those can define mandatory
    # fields or enum values in an unseen tool API.
    prompt = (
        "You compile bank call policy from supplied source documents. Return one JSON object only. "
        "Use the documents in their supplied order; later amendments override earlier rules only where they conflict. "
        "Treat mail, code lists, tables, notes, and policy extracts as binding source material where applicable. "
        "Do not invent a bank fact, value, field, enum, outcome, reason, date, amount, or exception. "
        "Preserve exact quoted customer-facing sentences when a source requires one. "
        "For ambiguous or missing details, write an explicit uncertainty in constraints instead of guessing. "
        "Distinguish conditions that apply before verification, after verification, to each product, and at close. "
        "Capture priority and override relationships. Be precise enough for an automated workflow generator.\n\n"
        "Return exactly these keys:\n"
        "global_rules: string with always-applicable rules and precedence.\n"
        "opening: string with initial contact, third-party, callback, wrong-number and pre-verification rules.\n"
        "identity: string with exact questions, order, retries, hints, and refusal behavior.\n"
        "verified_call: string with account presentation, product sequencing, negotiation, confirmation, and exception behavior.\n"
        "outcomes: object mapping outcomes to permitted reasons and the source-derived conditions for each.\n"
        "tool_policy: object keyed by each listed tool name; each value is a detailed string on when/how to call it, "
        "required values, failure handling, and call ordering.\n"
        "raw_brief: empty string (the caller inserts exact source).\n"
        "constraints: object of explicit calendars, calculations, limits, timing, product rules, forbidden actions, "
        "and any unresolved ambiguity. Preserve numerals and enums from the source.\n\n"
        f"TOOL NAMES: {json.dumps(names)}\n"
        f"OPENAPI DOCUMENT (including parameters, servers, security, and components): {json.dumps(openapi, ensure_ascii=False)}\n\n"
        f"SOURCE DOCUMENTS IN ORDER:\n{raw_brief}"
    )
    policy = _json_object(call_model(prompt, purpose="compile bank brief and tool policy"))
    missing = [key for key in REQUIRED_KEYS if key not in policy]
    if missing:
        raise ValueError("Policy model omitted keys: " + ", ".join(missing))
    for key in ("global_rules", "opening", "identity", "verified_call"):
        if not isinstance(policy[key], str):
            raise ValueError(f"Policy {key} must be a string")
    for key in ("outcomes", "tool_policy", "constraints"):
        if not isinstance(policy[key], dict):
            raise ValueError(f"Policy {key} must be an object")
    unknown_tools = set(policy["tool_policy"]) - set(names)
    if unknown_tools:
        raise ValueError("Policy references unknown tools: " + ", ".join(sorted(unknown_tools)))
    missing_tool_rules = [name for name in names if not isinstance(policy["tool_policy"].get(name), str)
                          or not policy["tool_policy"][name].strip()]
    if missing_tool_rules:
        raise ValueError("Policy omitted tool instructions: " + ", ".join(missing_tool_rules))
    policy["tool_policy"] = {name: policy["tool_policy"][name] for name in names}
    policy["raw_brief"] = raw_brief
    return policy

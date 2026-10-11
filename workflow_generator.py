"""Build a Freya collections workflow from a parsed policy and public OpenAPI.

The returned object is the workflow value, not the REST ``name/workflow`` wrapper.
All HTTP calls are derived from the supplied OpenAPI document.
"""

from __future__ import annotations

import inspect
import re
from typing import Any

from api_bindings import (account_response_collection, identity_response_fields,
                          identity_selector_field, note_request_fields,
                          promise_request_fields, transfer_request_fields,
                          close_request_fields)
from api_parameters import required_header_bindings
from api_servers import operation_url
from account_item_bindings import product_item_fields
from business_calendar import business_weekdays
from bank_names import BANK_NAME, source_bank_name
from deadline_tables import parse_deadline_tables
from identity_order import latest_explicit_identity_order
from openapi_resolver import normalize_openapi
from payment_amounts import parse_payment_amount
from payment_dates import parse_payment_date
from source_code_mappings import explicit_code_mappings
from reason_code_mappings import explicit_reason_codes
from success_messages import exact_success_sentence
from policy_capabilities import requires_multi_product


_PAYMENT_AMOUNT_SOURCE = inspect.getsource(parse_payment_amount)
_PAYMENT_DATE_SOURCE = inspect.getsource(parse_payment_date)


def _with_amount_parser(code: str) -> str:
    """Embed the shared strict parser in a standalone Freya code node."""
    return _PAYMENT_AMOUNT_SOURCE + "\n" + code


def _check(variable: str, operator: str, operand: Any = "") -> dict:
    item = {"variable": variable, "operator": operator}
    if operator not in ("is_set", "is_not_set"):
        item["operand"] = operand
    return item


def _when(*checks: dict) -> dict:
    return {"logic": "or", "conditions": list(checks)}


def _to(target: str, description: str, condition: dict) -> dict:
    return {"target_node": target, "description": description, "condition": condition}


def _eq(target: str, variable: str, value: str, description: str = "") -> dict:
    return _to(target, description or value, _when(_check(variable, "equals", value)))


def _always(target: str) -> dict:
    return _to(target, "Continue", {"operator": "always"})


def _bool(target: str, variable: str, description: str) -> dict:
    return _to(target, description, _when(_check(variable, "equals", "true"), _check(variable, "equals", "True")))


def _bank_response(name: str, codes: list[str], flag: str, value: bool) -> dict:
    """Require a current bank HTTP status and an explicit write-result flag."""
    text = "true" if value else "false"
    return _when(*(
        {"logic": "and", "conditions": [
            _check(name + "_response.status_code", "equals", code),
            _check(name + "_response.data." + flag, "equals", spelling),
        ]}
        for code in codes for spelling in (text, text.title())
    ))


def _bank_non_recording(name: str, false_codes: list[str], no_write_codes: list[str], flag: str) -> dict:
    """Only source-documented non-recording responses can authorize a retry."""
    return _when(*_bank_response(name, false_codes, flag, False)["conditions"],
                 *(_check(name + "_response.status_code", "equals", code) for code in no_write_codes))


def _enum(name: str, choices: list[str], description: str, min_confidence: float = 0.65) -> dict:
    return {"name": name, "type": "string", "enum_values": choices, "description": description,
            "min_confidence": min_confidence}


def _code_node(name: str, source: str, next_node: str) -> dict:
    if "{{" in source or "}}" in source:
        raise ValueError("Code node source must not contain template braces")
    return {
        "name": name,
        "node_type": "code",
        "on_enter": {"type": "silent"},
        "passthrough": True,
        "passthrough_default_transition": next_node,
        "pre_actions": [{
            "type": "function",
            "function": {
                "name": "code_exec",
                "handler": "__function__:code_exec",
                "parameters": {"type": "object", "properties": {
                    "code": {"type": "string", "default": source},
                    "timeout_ms": {"type": "number", "default": 10000},
                }},
            },
        }],
        "transitions": [_always(next_node)],
    }


def _conversation(name: str, prompt: str, *, opening: str | None = None,
                  extraction: list[dict] | None = None, tools: list[dict] | None = None,
                  transitions: list[dict] | None = None, max_turns: int = 30,
                  max_target: str | None = None) -> dict:
    node = {
        "name": name,
        "node_type": "conversation",
        "task_message": prompt,
        "on_enter": {"type": "tts", "text": opening} if opening is not None else {"type": "llm"},
        "transitions": transitions or [],
        "max_node_turns": max_turns,
    }
    if extraction:
        node["extraction_schema"] = extraction
    if tools:
        node["tools"] = tools
    if max_target:
        node["max_turn_transition"] = max_target
    return node


def _api_spec(openapi: dict, name: str) -> tuple[str, str, dict]:
    operations = [
        (path, method.upper(), operation)
        for path, path_item in (openapi.get("paths") or {}).items()
        if isinstance(path_item, dict)
        for method, operation in path_item.items()
        if method.lower() in ("post", "put", "patch", "get", "delete") and isinstance(operation, dict)
    ]
    matches = [item for item in operations if item[2].get("operationId") == name]
    if not matches:
        # The public draft has no operationIds. Only then use its path-tail
        # convention; an operation with a different ID must not be rebound.
        matches = [item for item in operations
                   if not item[2].get("operationId") and item[0].rstrip("/").split("/")[-1] == name]
    if len(matches) != 1:
        raise ValueError(f"OpenAPI must define exactly one operation for {name}; found {len(matches)}")
    path, method, operation = matches[0]
    schema = operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
    return path, method, schema


def _source_says_no_write(description: str) -> bool:
    """Recognize only explicit API guarantees that a write did not happen."""
    text = description.lower()
    return bool(re.search(
        r"\b(?:nothing|no (?:promise|write|record|action))\b.{0,50}"
        r"\b(?:recorded|written|persisted|created|carried out|made)\b|"
        r"\b(?:not|never)\b.{0,35}\b(?:recorded|written|persisted|created|carried out)\b|"
        r"\bno write\b|\bnothing was recorded\b",
        text,
    ))


def _promise_response_contract(openapi: dict) -> tuple[list[str], str, list[str], list[str]]:
    """Read the write-result field and retry guarantees from create_promise OpenAPI."""
    path, method, _ = _api_spec(openapi, "create_promise")
    operation = openapi["paths"][path][method.lower()]
    responses = operation.get("responses") or {}
    success_codes: list[str] = []
    false_codes: list[str] = []
    no_write_codes: list[str] = []
    flags: set[str] = set()
    for raw_code, response in responses.items():
        code = str(raw_code)
        if not (len(code) == 3 and code.isdigit() and isinstance(response, dict)):
            continue
        description = str(response.get("description") or "")
        if code.startswith("2"):
            schema = (response.get("content") or {}).get("application/json", {}).get("schema") or {}
            properties = schema.get("properties") or {}
            candidates = [
                field for field, spec in properties.items()
                if isinstance(spec, dict) and spec.get("type") == "boolean" and (
                    re.search(r"(?:^|_)(?:ok|success|succeeded|recorded|saved|created|written|persisted|completed)$", field, re.I)
                    or re.search(r"\btrue\b.{0,50}\b(?:recorded|written|persisted|created|carried out)\b",
                                 str(spec.get("description") or ""), re.I)
                )
            ]
            if len(candidates) != 1:
                continue
            flag = candidates[0]
            flags.add(flag)
            success_codes.append(code)
            if "false" in (description + " " + str(properties[flag].get("description") or "")).lower() and _source_says_no_write(
                description + " " + str(properties[flag].get("description") or "")
            ):
                false_codes.append(code)
        elif _source_says_no_write(description):
            no_write_codes.append(code)
    if len(flags) != 1 or not success_codes:
        raise ValueError("create_promise OpenAPI must unambiguously define one boolean write-success field")
    return success_codes, next(iter(flags)), false_codes, no_write_codes


def _api_tool(openapi: dict, name: str, *, fixed: dict | None = None,
              description: str = "", post_code: str = "") -> dict:
    path, method, schema = _api_spec(openapi, name)
    headers = {"Content-Type": {"type": "string", "default": "application/json"}}
    headers.update(required_header_bindings(openapi, path, method))
    fixed = fixed or {}
    properties = {}
    for field, spec in (schema.get("properties") or {}).items():
        leaf = {"type": spec.get("type", "string"),
                "description": spec.get("description") or f"{field} for {name}."}
        if spec.get("enum"):
            leaf["enum"] = spec["enum"]
        leaf["default"] = fixed[field] if field in fixed else "{{" + field + "}}"
        properties[field] = leaf
    params = {"type": "object", "properties": {
        "url": {"type": "string", "default": operation_url(openapi, path, method)},
        "method": {"type": "string", "default": method},
        "headers": {"type": "object", "properties": headers},
        "body": {"type": "object", "properties": properties, "required": schema.get("required") or []},
    }}
    tool = {"type": "function", "config": {"timeoutMs": 12000, "maxRetries": 0},
            "function": {"name": name, "handler": "__function__:api_call",
                         "description": description or f"Call {name} when its policy condition is met.",
                         "parameters": params}}
    if post_code:
        tool["post_process_code"] = post_code
    return tool


_NUMBER_WORDS = {
    "zero": 0, "one": 1, "first": 1, "two": 2, "second": 2,
    "three": 3, "third": 3, "four": 4, "fourth": 4,
    "five": 5, "fifth": 5, "six": 6, "sixth": 6,
    "seven": 7, "seventh": 7, "eight": 8, "eighth": 8,
    "nine": 9, "ninth": 9, "ten": 10, "tenth": 10,
    "eleven": 11, "eleventh": 11, "twelve": 12, "twelfth": 12,
    "thirteen": 13, "thirteenth": 13, "fourteen": 14, "fourteenth": 14,
    "fifteen": 15, "fifteenth": 15, "sixteen": 16, "sixteenth": 16,
    "seventeen": 17, "seventeenth": 17, "eighteen": 18, "eighteenth": 18,
    "nineteen": 19, "nineteenth": 19, "twenty": 20, "twentieth": 20,
    "thirty": 30, "thirtieth": 30, "forty": 40, "fortieth": 40,
    "fifty": 50, "fiftieth": 50,
}
_NUMBER_TEXT = r"[A-Za-z]+(?:[- ][A-Za-z]+)?|\d+(?:st|nd|rd|th)?"
_MONTHS = {name.lower(): index for index, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"), 1)}


def _source_number(value: object) -> int | None:
    text = str(value).strip().lower()
    digit = re.fullmatch(r"(\d+)(?:st|nd|rd|th)?", text)
    if digit:
        return int(digit.group(1))
    parts = re.split(r"[ -]+", text)
    numbers = [_NUMBER_WORDS.get(part) for part in parts]
    return sum(numbers) if numbers and all(number is not None for number in numbers) else None


def _mentions_number(text: str, expected: int) -> bool:
    tokens = re.findall(r"\b\d+(?:st|nd|rd|th)?\b|\b[A-Za-z]+\b", text)
    return any(_source_number(token) == expected for token in tokens) or any(
        _source_number(left + " " + right) == expected for left, right in zip(tokens, tokens[1:]))


def _source_date_matches(source: str) -> list[tuple[int, int, str]]:
    """Recognize ISO and month-name dates without interpreting unrelated numbers."""
    import datetime
    found: list[tuple[int, int, str]] = []
    patterns = (
        r"\b(?P<iso>20\d{2}-\d{1,2}-\d{1,2})\b",
        r"\b(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+"
        rf"(?P<day>{_NUMBER_TEXT}),?\s+(?P<year>20\d{{2}})\b",
        rf"\b(?P<day>{_NUMBER_TEXT})\s+(?:of\s+)?"
        r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|December),?\s+"
        r"(?P<year>20\d{2})\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, source, re.I):
            try:
                if match.groupdict().get("iso"):
                    date = datetime.date.fromisoformat(match.group("iso"))
                else:
                    day = _source_number(match.group("day"))
                    if day is None:
                        continue
                    date = datetime.date(int(match.group("year")),
                                         _MONTHS[match.group("month").lower()], day)
            except (ValueError, KeyError):
                continue
            if not any(start <= match.start() < end for start, end, _ in found):
                found.append((match.start(), match.end(), date.isoformat()))
    return sorted(found)


def _holiday_dates(policy: dict) -> list[str]:
    """Apply source calendar statements in order, including later revocations."""
    source = str(policy.get("raw_brief") or "")
    if not source:
        source = "\n".join(str(policy.get(key) or "") for key in
                           ("global_rules", "verified_call", "opening"))
    dates: set[str] = set()
    matches = _source_date_matches(source)
    if not matches and re.search(r"\b(?:bank\s+)?holidays?\b", source, re.I) and not re.search(
            r"\b(?:no|without)\s+(?:bank\s+)?holidays?\b", source, re.I):
        raise ValueError("Cannot establish bank holiday dates from source")
    for index, (start_at, end_at, date) in enumerate(matches):
        start = max(source.rfind(mark, 0, start_at) for mark in (";", ".", "\n")) + 1
        ends = [source.find(mark, end_at) for mark in (";", ".", "\n")]
        end = min((point for point in ends if point >= 0), default=len(source))
        clause = source[start:end].lower()
        if "holiday" not in clause:
            continue
        next_date = matches[index + 1][0] if index + 1 < len(matches) else len(source)
        following = source[end_at:min(end, next_date)].lower()
        preceding = source[max(start, start_at - 55):start_at].lower()
        revoked = re.search(r"\b(?:no longer|not|removed|revoked|cancelled|canceled)\b", following)
        revoked = revoked or re.search(r"\b(?:remove|revoke|cancel)\b.{0,35}\bholiday\b", preceding)
        if revoked:
            dates.discard(date)
        else:
            dates.add(date)
    return sorted(dates)


def _queue_for(openapi: dict, role: str, policy: dict) -> str:
    schema = _api_spec(openapi, "transfer_to_agent")[2]
    queue_spec = (schema.get("properties") or {}).get(transfer_request_fields(schema)["queue"]) or {}
    values = [str(value) for value in queue_spec.get("enum") or []]
    if not values:
        return role
    source = str(policy.get("raw_brief") or "") or str(policy.get("global_rules") or "")
    explicit = explicit_code_mappings(source, "queue", values)
    if role in explicit:
        return explicit[role]
    hints = {
        "restructuring": ("restructur", "repayment", "payment_plan", "workout", "arrangement", "specialist"),
        "support": ("support", "care", "hardship", "assist"),
        "human": ("general", "human", "agent", "service"),
    }
    candidates = {value for value in values if any(hint in value.lower() for hint in hints[role])}
    if len(candidates) == 1:
        return candidates.pop()
    raise ValueError(f"Cannot map {role} queue unambiguously to OpenAPI enum {values!r}")


def _identity_fields(openapi: dict, policy: dict) -> list[str]:
    schema = _api_spec(openapi, "verify_identity")[2]
    _, values = identity_selector_field(schema)
    explicit = latest_explicit_identity_order(str(policy.get("raw_brief") or ""), values)
    if explicit:
        return explicit
    source = "\n".join(str(policy.get(key) or "") for key in ("identity", "raw_brief"))
    aliases = {
        "date_of_birth": ("date of birth", "birth date", "dob"),
        "phone_last4": ("last four digits of the phone", "last 4 digits of the phone", "phone number"),
        "birth_year": ("birth year",),
    }
    positions = {}
    for value in values:
        names = aliases.get(value, (value.replace("_", " "),))
        hits = [match.start() for name in names for match in re.finditer(re.escape(name), source, re.I)]
        if hits:
            positions[value] = min(hits)
    if len(positions) >= 2:
        return sorted(positions, key=positions.get)[:2]
    return values[:2]


def _outcome_names(openapi: dict, policy: dict | None = None) -> dict[str, str]:
    schema = _api_spec(openapi, "close_call")[2]
    outcome_field = close_request_fields(schema)["outcome"]
    values = [str(value) for value in ((schema.get("properties") or {}).get(outcome_field) or {}).get("enum") or []]
    if not values:
        return {role: role for role in ("promise", "paid", "transferred", "no_promise", "not_verified", "not_reached")}
    aliases = {
        "promise": ("promise", "pledge", "commit"), "paid": ("paid", "settled"),
        "transferred": ("transfer", "handoff"), "no_promise": ("no_promise", "refus", "unresolved"),
        "not_verified": ("not_verified", "unverified", "identity_fail"),
        "not_reached": ("not_reached", "unreached", "contact_fail"),
    }
    result = {}
    policy_outcomes = (policy or {}).get("outcomes") or {}
    policy_keys = list(policy_outcomes) if isinstance(policy_outcomes, dict) else []
    source = str((policy or {}).get("raw_brief") or "")
    source_codes = explicit_code_mappings(source, "outcome", values)
    for role, hints in aliases.items():
        replacements = re.findall(r"replace\s+(?:outcome\s+)?" + re.escape(role) +
                                  r"\s+with\s+([a-z0-9_-]+)", source, flags=re.IGNORECASE)
        if role in source_codes:
            result[role] = source_codes[role]
        elif replacements and replacements[-1] in values:
            result[role] = replacements[-1]
        elif isinstance(policy_outcomes, dict) and policy_outcomes.get(role) in values:
            result[role] = policy_outcomes[role]
        elif role in values:
            result[role] = role
        else:
            candidates = [value for value in values if value in policy_keys]
            result[role] = next((value for hint in hints for value in candidates + values
                                 if hint in value.lower() and not (role == "promise" and value.lower().startswith("no_"))), None)
            if result[role] is None:
                raise ValueError(f"Cannot map {role} outcome to OpenAPI enum {values!r}")
    return result


def _reason_names(openapi: dict, policy: dict) -> tuple[dict[str, str], dict[str, str], list[str], list[str]]:
    close_schema = _api_spec(openapi, "close_call")[2]
    reason_field = close_request_fields(close_schema)["reason"]
    close_enum = [str(value) for value in ((close_schema.get("properties") or {}).get(reason_field) or {}).get("enum") or []]
    note_schema = _api_spec(openapi, "add_note")[2]
    note_reason_field = note_request_fields(note_schema)["reason"]
    note_enum = [str(value) for value in ((note_schema.get("properties") or {}).get(note_reason_field) or {}).get("enum") or []]
    source = str(policy.get("raw_brief") or policy.get("tool_policy") or "")
    note_explicit, close_explicit = explicit_reason_codes(source, note_enum, close_enum)
    aliases = {
        "refused": ("refused", "declined", "refusal"),
        "hardship": ("hardship", "vulnerable", "difficulty"),
        "dispute": ("dispute", "contested", "challenge"),
        "paid": ("paid", "settled", "cleared"),
        "service_error": ("service_error", "write_failed", "system_error", "failed_write"),
        "human": ("human", "general", "agent"),
        "support": ("support", "care", "assistance"),
        "restructuring": ("restructuring", "repayment", "payment_plan", "workout"),
        "failed": ("failed", "identity_failed"),
        "declined": ("declined", "refused"),
        "callback": ("callback", "booked"),
        "no_callback": ("no_callback", "unbooked"),
        "wrong_number": ("wrong_number", "incorrect_number"),
        "deceased": ("deceased", "died"),
        "unclear": ("unclear", "unconfirmed"),
    }
    result = {}
    close_result = {}
    for role, hints in aliases.items():
        replacements = re.findall(r"replace\s+(?:the\s+)?(?:refusal\s+code\s+)?" + re.escape(role) +
                                  r"\s+with\s+([a-z0-9_-]+)", source, flags=re.IGNORECASE)
        for allowed, target, explicit_codes in ((note_enum, result, note_explicit),
                                                (close_enum, close_result, close_explicit)):
            explicit = explicit_codes.get(role)
            if replacements and (not allowed or replacements[-1] in allowed):
                target[role] = replacements[-1]
            elif explicit and (not allowed or explicit in allowed):
                target[role] = explicit
            elif explicit and target is close_result:
                raise ValueError(f"Reason code {explicit!r} for {role} is absent from close_call enum {allowed!r}")
            elif not allowed:
                target[role] = role
            elif role in allowed:
                target[role] = role
            else:
                target[role] = next((value for hint in hints for value in allowed if hint in value.lower()), "")
    required_note = {"refused", "hardship", "dispute", "paid", "service_error"}
    required_close = {"refused", "hardship", "dispute", "service_error",
                      "human", "support", "restructuring", "failed", "declined",
                      "callback", "no_callback", "wrong_number", "deceased", "unclear"}
    for operation, allowed, mapped, required in (
        ("add_note", note_enum, result, required_note),
        ("close_call", close_enum, close_result, required_close),
    ):
        if allowed:
            missing = sorted(role for role in required if mapped.get(role) not in allowed)
            if missing:
                raise ValueError(f"Cannot map {operation} reason code(s) from policy source and API enum: {', '.join(missing)}")
    return result, close_result, note_enum, close_enum


def _bank_name(policy: dict) -> str:
    raw_name = source_bank_name(str(policy.get("raw_brief") or ""))
    if raw_name:
        return raw_name
    constraints = policy.get("constraints") or {}
    if isinstance(constraints, dict):
        for key in ("bank_name", "institution_name", "company_name", "organization_name"):
            value = constraints.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    source = "\n".join(str(policy.get(key) or "") for key in ("opening", "global_rules", "raw_brief"))
    match = re.search(r"\b([A-Z][A-Za-z0-9&-]*(?:\s+[A-Z][A-Za-z0-9&-]*){0,4}\s+(?:Bank|Credit Union))\b", source)
    return match.group(1) if match else "the bank"


def _opening_line(policy: dict, bank_name: str) -> str:
    """Render the source's initial disclosure before asking who answered."""
    raw = str(policy.get("raw_brief") or "")
    opening_source = str(policy.get("opening") or "")
    for text in (raw, opening_source):
        exact_lines = []
        for line in text.splitlines():
            if not re.search(r"\b(?:first sentence|opening(?: sentence| line)?|start(?: the)? call|begin(?: the)? call)\b", line, re.I):
                continue
            if not re.search(r"\b(?:exact(?:ly)?|verbatim|word.for.word|must say|required wording)\b", line, re.I):
                continue
            quoted_parts = re.findall(r'["“]([^"”]+)["”]', line)
            quoted_parts += re.findall(r"(?<!\w)'((?:[^']|'(?=\w))*)'", line)
            for quoted in quoted_parts:
                named = {match.group() for match in BANK_NAME.finditer(quoted)}
                if (not named or bank_name in named) and (bank_name.lower() in quoted.lower() or "digital assistant" in quoted.lower()):
                    exact_lines.append(quoted)
        if exact_lines:
            exact = re.sub(r"(?<!\{)\{(title|last_name)\}(?!\})", r"{{\1}}", exact_lines[-1].strip())
            return exact if "?" in exact else exact + " Am I speaking with {{title}} {{last_name}}?"
    opening_lines = [line for line in raw.splitlines()
                     if re.search(r"\b(?:first sentence|opening(?: sentence| line)?|start(?: the)? call|begin(?: the)? call)\b", line, re.I)]
    opening_source = "\n".join([opening_source, *opening_lines]).strip()
    recording_disclosure = bool(re.search(
        r"\b(?:call|conversation)\s+(?:is|was|will be|may be|must be|shall be|is being)\s+recorded\b|"
        r"\brecord(?:ing)?\s+(?:this|the)\s+(?:call|conversation)\b|"
        r"\brecording\s+disclosure\b",
        opening_source, re.I,
    ))
    first_sentence = f"Hello, this is {bank_name}'s digital assistant"
    if recording_disclosure:
        first_sentence += ", and we are recording this call"
    return first_sentence + ". Am I speaking with {{title}} {{last_name}}?"


def _deadline_bands(policy: dict) -> list[dict]:
    source = str(policy.get("raw_brief") or "")
    if not source:
        source = "\n".join(str(policy.get(key) or "") for key in ("verified_call", "global_rules"))
    found: list[tuple[int, dict]] = parse_deadline_tables(source)
    range_pattern = re.compile(
        r"\b(?P<low>\d+)\s*(?:to|through|[-–])\s*(?P<high>\d+)\s+days?\b"
        r"(?:\s+(?:late|overdue))?|"
        r"\boverdue\s+from\s+(?P<pre_low>\d+)\s*(?:to|through|[-–])\s*"
        r"(?P<pre_high>\d+)\s+days?\b", re.I,
    )
    ranges = list(range_pattern.finditer(source))
    partial: list[tuple[int, int, int]] = []
    for index, match in enumerate(ranges):
        line = source[source.rfind("\n", 0, match.start()) + 1:source.find("\n", match.end()) if "\n" in source[match.end():] else len(source)]
        if "|" in line:  # The table parser preserves its explicit band label.
            continue
        low = int(match.group("low") or match.group("pre_low"))
        high = int(match.group("high") or match.group("pre_high"))
        next_start = ranges[index + 1].start() if index + 1 < len(ranges) else len(source)
        window = source[match.end():min(next_start, match.end() + 420)]
        blank = re.search(r"\n\s*\n|===== SOURCE:", window)
        if blank:
            window = window[:blank.start()]
        deadline = re.search(rf"\bdeadline\b\s*(?:is|falls?|=|:)?\s*(?:on|after|the)?\s*"
                             rf"(?P<days>{_NUMBER_TEXT})\s+business\s+days?\b", window, re.I)
        attempts = re.search(r"(?P<count>[A-Za-z]+(?:-[A-Za-z]+)?|\d+)\s+"
                             r"(?:persuasion\s+)?attempts?\b", window, re.I)
        if not attempts:
            attempts = re.search(rf"\b(?:persuasion\s+)?attempts?\s*(?:are|is|=|:)\s*"
                                 r"(?P<count>[A-Za-z]+(?:-[A-Za-z]+)?|\d+)\b", window, re.I)
        days = _source_number(deadline.group("days")) if deadline else None
        count = _source_number(attempts.group("count")) if attempts else None
        if days is not None and days > 0 and count is not None and count >= 0:
            found.append((match.start(), {"low": low, "high": high, "label": "",
                                          "business_days": days, "attempts": count}))
        elif re.search(r"\bdeadline\b.{0,80}\bbusiness\s+day|\bpersuasion\s+attempt", window, re.I | re.S):
            partial.append((match.start(), low, high))

    constraints = policy.get("constraints") or {}
    structured = constraints.get("deadline_bands") if isinstance(constraints, dict) else None
    if isinstance(structured, list):
        for item in structured:
            if not isinstance(item, dict):
                continue
            try:
                band = {"low": int(item["low"]), "high": int(item["high"]),
                        "business_days": int(item["business_days"]), "attempts": int(item["attempts"]),
                        "label": str(item.get("label") or "")}
            except (KeyError, TypeError, ValueError):
                continue
            if band["low"] > band["high"] or band["business_days"] < 1 or band["attempts"] < 0:
                continue
            # Model extraction is a fallback, never an override of a later source rule.
            for match in ranges:
                if (int(match.group("low") or match.group("pre_low")),
                    int(match.group("high") or match.group("pre_high"))) != (band["low"], band["high"]):
                    continue
                excerpt = source[match.start():match.end() + 420]
                deadline_context = re.search(r"\bdeadline\b([^.;\n]{0,100})", excerpt, re.I)
                attempt_context = re.search(r"([^.;\n]{0,70})\battempts?\b", excerpt, re.I)
                if (deadline_context and attempt_context and
                    _mentions_number(deadline_context.group(1), band["business_days"]) and
                    _mentions_number(attempt_context.group(1), band["attempts"])):
                    found.insert(0, (-1, band))
                    partial = [entry for entry in partial if entry !=
                               (match.start(), band["low"], band["high"])]
                    break
    missing = [(low, high) for position, low, high in partial if not any(
        later >= position and (band["low"], band["high"]) == (low, high)
        for later, band in found)]
    if missing:
        raise ValueError("Cannot establish source deadline and persuasion attempts for overdue band(s): " +
                         ", ".join(f"{low} to {high} days" for low, high in missing))
    if not found and re.search(r"\bdeadline\b", source, re.I) and re.search(r"\bbusiness\s+days?\b", source, re.I):
        raise ValueError("Cannot establish source deadline and persuasion attempts before building workflow")
    return [band for _, band in sorted(found, key=lambda item: item[0])]


def _success_sentence(policy: dict) -> str:
    source = str(policy.get("raw_brief") or "")
    return exact_success_sentence(source) or ""


def _confirmation_question(policy: dict) -> str:
    source = str(policy.get("raw_brief") or "")
    matches = re.findall(r"read[^\n]{0,100}back[^\n]{0,100}ask\s+[\"“]([^\"”]+\?)[\"”]", source, flags=re.IGNORECASE)
    return matches[-1] if matches else "Do you confirm?"


def _finish_node(name: str, outcome: str, reason: str = "") -> dict:
    source = (
        "try:\n"
        f"    vars['close_outcome'] = {outcome!r}\n"
        f"    vars['close_reason'] = {reason!r}\n"
        "except Exception:\n"
        f"    vars['close_outcome'] = {outcome!r}\n"
        f"    vars['close_reason'] = {reason!r}\n"
    )
    return _code_node(name, source, "farewell")


_VERIFY_HOOK = """try:
    result.pop('header', None)
    data = result.get('data') if isinstance(result, dict) else None
    if isinstance(data, dict):
        if data.get(__COMPLETE_FIELD__) is True:
            data['what_to_do_next'] = 'Identity is fully verified. Continue to account lookup.'
        elif data.get(__ANSWER_FIELD__) is True:
            data['what_to_do_next'] = 'This answer passed. Ask only the next security question.'
        elif data.get('hint'):
            data['what_to_do_next'] = 'The service returned a hint. Apply the exact hint-handling rule in the policy before retrying.'
        elif data.get(__REMAINING_FIELD__) == 0:
            data['what_to_do_next'] = 'Security check ended. Follow the policy failure guidance; disclose no account information.'
        else:
            data['what_to_do_next'] = 'This answer did not pass. Ask the same question again without revealing the correct answer.'
except Exception:
    pass
"""


_PROMISE_HOOK = """try:
    import json
    result.pop('header', None)
    data = result.get('data') if isinstance(result, dict) else None
    if not isinstance(data, dict):
        data = dict()
        result['data'] = data
    pid = str(vars.get('pending_promise_product_id') or vars.get('pending_product_id') or '')
    ids = json.loads(str(vars.get('recorded_ids') or '[]'))
    attempts = json.loads(str(vars.get('promise_attempts') or '{}'))
    status_code = str(result.get('status_code') or '')
    if status_code in __SUCCESS_CODES__ and data.get(__RESULT_FLAG__) is True:
        if pid and pid not in ids:
            ids.append(pid)
        vars['recorded_ids'] = json.dumps(ids)
        vars['recorded_count'] = str(len(ids))
        data['what_to_do_next'] = 'Promise was recorded. Say the exact successful-promise confirmation required by the policy. Continue with remaining products; never record this product again.'
    elif (status_code in __FALSE_CODES__ and data.get(__RESULT_FLAG__) is False) or status_code in __NO_WRITE_CODES__:
        attempts[pid] = int(attempts.get(pid, 0)) + 1
        vars['promise_attempts'] = json.dumps(attempts)
        if attempts[pid] < 2:
            data['what_to_do_next'] = 'Promise was NOT recorded. Retry create_promise once with the same product, amount and date. Do not claim success.'
        else:
            data['what_to_do_next'] = 'Promise failed twice. Tell caller it could not be recorded. For a multi-product account add a service_error note for this product, then continue.'
    else:
        data['what_to_do_next'] = 'The write result is unknown. Do not retry or claim that a promise was recorded.'
except Exception:
    pass
"""


_NOTE_HOOK = """try:
    import json
    result.pop('header', None)
    data = result.get('data') if isinstance(result, dict) else None
    if not isinstance(data, dict):
        data = dict()
        result['data'] = data
    if data.get('ok') is True:
        notes = json.loads(str(vars.get('note_reasons') or '{}'))
        pid = str(vars.get('pending_note_product_id') or vars.get('add_note_args.product_id') or '')
        reason = str(vars.get('pending_note_reason') or vars.get('add_note_args.reason') or '')
        if pid:
            notes[pid] = reason
        vars['note_reasons'] = json.dumps(notes)
        data['what_to_do_next'] = 'Note saved. Continue with the next product or finish if all products have a result.'
    else:
        data['what_to_do_next'] = 'Note was not saved. Retry it once before closing, unless the customer requests a transfer.'
except Exception:
    pass
"""


_TRANSFER_HOOK = """try:
    result.pop('header', None)
    data = result.get('data') if isinstance(result, dict) else None
    if not isinstance(data, dict):
        data = dict()
        result['data'] = data
    if data.get('ok') is True:
        queue = str(vars.get('pending_transfer_queue') or vars.get('transfer_to_agent_args.queue') or '')
        reason = str(vars.get('pending_transfer_reason') or '')
        vars['transfer_reason'] = reason or ('human' if queue == 'general' else queue)
        data['what_to_do_next'] = 'Transfer requested successfully. Thank the caller and finish now. Add no notes.'
    else:
        data['what_to_do_next'] = 'Transfer failed. Say it could not be completed and try once more; do not claim the transfer happened.'
except Exception:
    pass
"""


_CALLBACK_HOOK = """try:
    result.pop('header', None)
    data = result.get('data') if isinstance(result, dict) else None
    if isinstance(data, dict):
        if data.get('ok') is True:
            vars['callback_recorded'] = 'yes'
            data['what_to_do_next'] = 'Callback booked. Thank the caller without disclosing why the bank called.'
        else:
            data['what_to_do_next'] = 'Callback was NOT booked. Retry once, or explain it could not be arranged.'
except Exception:
    pass
"""


def _prepare_code(holidays: list[str], weekdays: tuple[int, ...], item_bindings: dict, bands: list[dict],
                  offer_last_business_day: bool, collection_field: str) -> str:
    id_field = item_bindings["product_id"]
    kind_field = item_bindings["kind"]
    amount_field = item_bindings["past_due"]
    days_field = item_bindings["days_late"]
    floor_fields = item_bindings["floors"]
    card_field = item_bindings["card_last4"]
    return f"""import json
import datetime
try:
    raw = vars.get('account_products') or vars.get('load_account_response.data.{collection_field}') or vars.get('get_account_response.data.{collection_field}') or []
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, list):
        raw = []
    if not raw:
        groups = dict()
        for prefix in ('load_account_response.data.{collection_field}.', 'get_account_response.data.{collection_field}.'):
            for key, value in vars.items():
                if str(key).startswith(prefix):
                    tail = str(key)[len(prefix):].split('.', 1)
                    if len(tail) == 2 and tail[0].isdigit():
                        groups.setdefault(int(tail[0]), dict())[tail[1]] = value
        raw = [groups[i] for i in sorted(groups)]
    now = datetime.datetime.fromisoformat(str(vars.get('now') or '').replace('Z', '+00:00'))
    today = now.date()
    holidays = set({holidays!r})
    weekdays = set({weekdays!r})
    def business(day):
        return day.weekday() in weekdays and day.isoformat() not in holidays
    def nth(day, count):
        for ignored in range(count):
            day += datetime.timedelta(days=1)
            while not business(day):
                day += datetime.timedelta(days=1)
        return day
    week_start = today - datetime.timedelta(days=today.weekday())
    friday = None
    for week in range(53):
        dates = [week_start + datetime.timedelta(days=7 * week + index) for index in weekdays]
        choices = [day for day in dates if day > today and business(day)]
        if choices:
            friday = max(choices)
            break
    if friday is None:
        raise ValueError('No future business day for offer')
    result = []
    lines = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError('Malformed account product')
        for required_field in ({id_field!r}, {kind_field!r}, {amount_field!r}, {days_field!r}):
            if item.get(required_field) is None or item.get(required_field) == '':
                raise ValueError('Missing required account product field')
        kind = str(item[{kind_field!r}])
        label = kind.replace('_', ' ')
        if 'card' in kind and {card_field!r} and item.get({card_field!r}):
            label += ' ending ' + str(item.get({card_field!r}))
        amount = float(item[{amount_field!r}])
        days = int(float(item[{days_field!r}]))
        floors = [float(item.get(field) or 0) for field in {floor_fields!r}]
        floor = next((value for value in floors if value > 0), None)
        if amount <= 0 or days < 0 or floor is None or floor > amount:
            raise ValueError('Invalid account product amounts or age')
        band = next((rule for rule in reversed({bands!r}) if rule['low'] <= days <= rule['high']), None)
        deadline = nth(today, band['business_days']) if band else None
        entry = dict(product_id=str(item[{id_field!r}]), label=label,
                     past_due=amount, floor=floor, days_late=days,
                     band=band['label'] if band else '', deadline=deadline.isoformat() if deadline else '',
                     attempts=band['attempts'] if band else 0)
        result.append(entry)
        lines.append(str(index + 1) + '. ' + label + ': past due ' + format(amount, '.2f') +
                     ', ' + str(days) + ' days late; floor ' + format(floor, '.2f') +
                     '; deadline ' + (deadline.isoformat() if deadline else 'use policy rule') +
                     '; persuasion attempts ' + (str(entry['attempts']) if band else 'use policy rule') +
                     '; product ID ' + entry['product_id'])
    vars['product_json'] = json.dumps(result)
    vars['product_count'] = str(len(result))
    vars['account_facts'] = '\\n'.join(lines) if lines else 'No account products were returned.'
    vars['date_facts'] = ('Now: ' + str(vars.get('now') or '') + '; today: ' + today.isoformat() +
                         ('; final business day offer: ' + friday.isoformat() if {offer_last_business_day!r} else '') +
                         '; bank holidays: ' + ', '.join(sorted(holidays)))
    vars['last_business_day_offer'] = friday.isoformat() if {offer_last_business_day!r} else ''
    vars['single_floor_stage'] = '0'
    vars['single_revision_attempts'] = '0'
    vars['recorded_ids'] = '[]'
    vars['recorded_count'] = '0'
    vars['note_reasons'] = '{{}}'
    vars['missing_products'] = ', '.join(str(item.get('product_id')) for item in result)
except Exception:
    vars['product_json'] = '[]'
    vars['product_count'] = '0'
    vars['account_facts'] = 'Account details unavailable.'
    vars['date_facts'] = 'Use the call variable now for dates.'
    vars['recorded_ids'] = '[]'
    vars['recorded_count'] = '0'
    vars['note_reasons'] = '{{}}'
    vars['missing_products'] = ''
"""


_FINAL_CODE = """import json
try:
    outcome_map = __OUTCOME_MAP__
    reason_map = __REASON_MAP__
    close_reason_map = __CLOSE_REASON_MAP__
    note_codes = __NOTE_CODES__
    close_codes = __CLOSE_CODES__
    transfer_first = __TRANSFER_FIRST__
    transfer = str(vars.get('transfer_reason') or '')
    products = json.loads(str(vars.get('product_json') or '[]'))
    recorded = json.loads(str(vars.get('recorded_ids') or '[]'))
    notes = json.loads(str(vars.get('note_reasons') or '{}'))
    pending_overrides = json.loads(str(vars.get('pending_note_overrides') or '{}'))
    attempts = json.loads(str(vars.get('promise_attempts') or '{}'))
    if transfer and (transfer_first or not recorded):
        vars['close_outcome'] = 'transferred'
        vars['close_reason'] = transfer
        vars['processing_complete'] = 'yes'
    else:
        if len(products) == 1 and not recorded:
            pid = str(products[0].get('product_id'))
            if pid in pending_overrides:
                notes[pid] = pending_overrides[pid]
            elif int(attempts.get(pid, 0)) >= 2:
                notes[pid] = reason_map.get('service_error') or 'service_error'
            elif pid not in notes:
                candidate = str(vars.get('single_product_reason') or '')
                if candidate in note_codes:
                    notes[pid] = candidate
                elif candidate in reason_map and reason_map[candidate] in note_codes:
                    notes[pid] = reason_map[candidate]
                else:
                    notes[pid] = reason_map.get('refused') or 'refused'
        missing = [str(item.get('product_id')) for item in products
                   if str(item.get('product_id')) not in recorded and
                   str(item.get('product_id')) not in notes]
        vars['missing_products'] = ', '.join(missing)
        vars['processing_complete'] = 'yes' if not missing or len(products) == 1 else 'no'
        if recorded:
            vars['close_outcome'] = 'promise'
            vars['close_reason'] = ''
        elif products and all(notes.get(str(item.get('product_id'))) == reason_map.get('paid', 'paid') for item in products):
            vars['close_outcome'] = 'paid'
            vars['close_reason'] = ''
        else:
            reason = reason_map.get('refused') or 'refused'
            for item in products:
                candidate = notes.get(str(item.get('product_id')))
                if candidate and candidate != reason_map.get('paid', 'paid'):
                    reason = candidate
                    break
            vars['close_outcome'] = 'no_promise'
            vars['close_reason'] = reason
    role = str(vars.get('close_outcome') or '')
    vars['close_outcome'] = outcome_map.get(role, role)
    reason = str(vars.get('close_reason') or '')
    if reason:
        if not close_codes or reason in close_codes:
            vars['close_reason'] = reason
        else:
            canonical = next((role for role, code in reason_map.items() if code == reason), reason)
            vars['close_reason'] = close_reason_map.get(canonical, '')
except Exception:
    vars['close_outcome'] = __ERROR_OUTCOME__
    vars['close_reason'] = __ERROR_REASON__
    vars['processing_complete'] = 'yes'
"""


_PREPARE_CONFIRMATION_CODE = """import json
import datetime
import re
__PAYMENT_DATE_PARSER__
try:
    vars['proposal_validation_state'] = 'invalid'
    vars['validation_state'] = 'invalid'
    vars['confirmation_status'] = 'unclear'
    vars['discussion_action'] = 'continue'
    vars['proposal_error'] = ''
    vars['readback_terms_json'] = '[]'
    vars['confirmation_readback'] = ''
    products = json.loads(str(vars.get('product_json') or '[]'))
    raw = vars.get('proposed_terms_json') or ''
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, dict):
        raw = raw.get('promises') or raw.get('products') or [raw]
    if not isinstance(raw, list) or not raw:
        raise ValueError('No proposed terms')
    now = datetime.datetime.fromisoformat(str(vars.get('now') or '').replace('Z', '+00:00'))
    today = now.date()
    holidays = set(__HOLIDAYS__)
    weekdays = set(__WEEKDAYS__)
    def business(day):
        return day.weekday() in weekdays and day.isoformat() not in holidays
    seen = set()
    confirmed = []
    adjustments = []
    for term in raw:
        if not isinstance(term, dict):
            raise ValueError('Malformed terms')
        pid = str(term.get('product_id') or '')
        if not pid and len(products) == 1:
            pid = str(products[0].get('product_id') or '')
        product = next((item for item in products if str(item.get('product_id')) == pid), None)
        if product is None or pid in seen:
            raise ValueError('Unknown or repeated product')
        seen.add(pid)
        amount_raw = term.get('amount')
        if isinstance(amount_raw, str) and amount_raw.lower().strip() in ('full', 'full amount', 'past due', 'past_due'):
            amount = float(product.get('past_due') or 0)
        elif isinstance(amount_raw, str) and amount_raw.lower().strip() in ('floor', 'minimum', 'minimum payment', 'instalment'):
            amount = float(product.get('floor') or 0)
        elif amount_raw is None:
            amount = float(product.get('past_due') or 0)
        else:
            amount = parse_payment_amount(amount_raw)
        minimum = float(product.get('floor') or 0)
        maximum = float(product.get('past_due') or 0)
        if amount < minimum or amount > maximum:
            raise ValueError('Amount outside approved range')
        date_words = term.get('date_phrase') or term.get('date_words') or term.get('raw_date')
        date_raw = str(date_words or term.get('date') or '').strip().lower()
        if len(raw) == 1 and not date_words:
            spoken = str(vars.get('proposed_date_words') or '').strip().lower()
            iso = re.search(r'\\d{4}-\\d{2}-\\d{2}', date_raw)
            if spoken and (not date_raw or (iso and datetime.date.fromisoformat(iso.group()) < today)):
                date_raw = spoken
        if not date_raw:
            raise ValueError('Payment date missing')
        day = parse_payment_date(date_raw, today)
        deadline_raw = str(product.get('deadline') or '')
        if day < today:
            raise ValueError('Date outside approved range')
        if deadline_raw and day > datetime.date.fromisoformat(deadline_raw):
            day = datetime.date.fromisoformat(deadline_raw)
            adjustments.append('The latest date I can record for ' + str(product.get('label') or pid) + ' is ' + day.isoformat() + '.')
        original_day = day
        while not business(day):
            day += datetime.timedelta(days=1)
        if deadline_raw and day > datetime.date.fromisoformat(deadline_raw):
            raise ValueError('No valid business day before deadline')
        if day != original_day:
            adjustments.append('The next business day for ' + str(product.get('label') or pid) + ' is ' + day.isoformat() + '.')
        confirmed.append(dict(product_id=pid, amount=amount, date=day.isoformat()))
    ordered = sorted(confirmed, key=lambda term: next(index for index, item in enumerate(products) if str(item.get('product_id')) == term['product_id']))
    lines = []
    for term in ordered:
        product = next(item for item in products if str(item.get('product_id')) == term['product_id'])
        lines.append(str(product.get('label') or term['product_id']) + ': ' + format(term['amount'], '.2f') + ' on ' + term['date'])
    vars['readback_terms_json'] = json.dumps(ordered)
    vars['confirmation_readback'] = ' '.join(adjustments) + (' ' if adjustments else '') + 'To confirm, you will pay ' + '; '.join(lines) + '.'
    vars['proposal_validation_state'] = 'ready'
    if len(products) == 1:
        vars['single_revision_base_json'] = json.dumps(ordered)
    vars['multi_correction_attempts'] = '0'
    vars['proposed_terms_json'] = ''
    vars['proposed_date_words'] = ''
except Exception as error:
    vars['proposal_validation_state'] = 'invalid'
    vars['readback_terms_json'] = '[]'
    vars['confirmation_readback'] = ''
    vars['proposed_terms_json'] = ''
    vars['proposed_date_words'] = ''
    vars['discussion_action'] = 'continue'
    vars['proposal_error'] = str(error)
"""


_VALIDATE_CONFIRMATION_CODE = """import json
import datetime
try:
    vars['validation_state'] = 'invalid'
    vars['pending_promises_json'] = '[]'
    if str(vars.get('confirmation_status') or '') != 'confirmed':
        raise ValueError('No explicit confirmation')
    readback = vars.get('readback_terms_json')
    if readback:
        if str(vars.get('proposal_validation_state') or '') != 'ready' or not str(vars.get('confirmation_readback') or ''):
            raise ValueError('No validated read-back')
        raw = readback
    else:
        raw = vars.get('confirmed_terms_json') or ''
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, list) or not raw:
        raise ValueError('No confirmed terms')
    products = json.loads(str(vars.get('product_json') or '[]'))
    now = datetime.datetime.fromisoformat(str(vars.get('now') or '').replace('Z', '+00:00'))
    today = now.date()
    holidays = set(__HOLIDAYS__)
    weekdays = set(__WEEKDAYS__)
    confirmed = []
    seen = set()
    for term in raw:
        if not isinstance(term, dict):
            raise ValueError('Malformed term')
        pid = str(term.get('product_id') or '')
        product = next((item for item in products if str(item.get('product_id')) == pid), None)
        if product is None or pid in seen:
            raise ValueError('Unknown product')
        seen.add(pid)
        amount = float(term.get('amount'))
        if not float(product.get('floor') or 0) <= amount <= float(product.get('past_due') or 0):
            raise ValueError('Invalid amount')
        day = datetime.date.fromisoformat(str(term.get('date') or ''))
        deadline = str(product.get('deadline') or '')
        if day < today or day.weekday() not in weekdays or day.isoformat() in holidays or (deadline and day > datetime.date.fromisoformat(deadline)):
            raise ValueError('Invalid date')
        confirmed.append(dict(product_id=pid, amount=amount, date=day.isoformat()))
    vars['pending_promises_json'] = json.dumps(confirmed)
    vars['validation_state'] = 'ready'
except Exception:
    vars['validation_state'] = 'invalid'
    vars['pending_promises_json'] = '[]'
"""


_CLEAR_CONFIRMATION_CODE = """try:
    vars['confirmation_status'] = 'changed'
    vars['validation_state'] = 'invalid'
    vars['proposal_validation_state'] = 'invalid'
    vars['confirmed_terms_json'] = ''
    vars['pending_promises_json'] = '[]'
    vars['readback_terms_json'] = '[]'
    vars['proposed_terms_json'] = ''
    vars['confirmation_readback'] = ''
except Exception:
    pass
"""


_SINGLE_REVISE_CODE = """import json
try:
    attempts = int(vars.get('single_revision_attempts') or 0) + 1
    vars['single_revision_attempts'] = str(attempts)
    if attempts > 3:
        vars['single_revision_state'] = 'exhausted'
        raise RuntimeError('Too many correction attempts')
    products = json.loads(str(vars.get('product_json') or '[]'))
    if len(products) != 1:
        raise ValueError('Expected one product')
    product = products[0]
    pid = str(product.get('product_id') or '')
    base = json.loads(str(vars.get('single_revision_base_json') or vars.get('readback_terms_json') or '[]'))
    previous = base[0] if isinstance(base, list) and len(base) == 1 else dict(product_id=pid, amount=product.get('past_due'), date='')
    raw = vars.get('single_corrected_terms_json') or ''
    raw = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
    raw = raw[0] if isinstance(raw, list) and len(raw) == 1 else raw
    if not isinstance(raw, dict) or str(raw.get('product_id') or pid) != pid:
        raise ValueError('No corrected terms')
    amount = raw.get('amount')
    date = str(raw.get('date_phrase') or raw.get('date_words') or raw.get('raw_date') or raw.get('date') or '').strip()
    if amount is None and not date:
        raise ValueError('No changed amount or day')
    merged = dict(product_id=pid, amount=amount if amount is not None else previous.get('amount'),
                  date=date or previous.get('date') or '')
    if merged['amount'] is None or not merged['date']:
        raise ValueError('Incomplete corrected terms')
    vars['proposed_terms_json'] = json.dumps([merged])
    vars['single_revision_state'] = 'ready'
except Exception:
    vars['proposed_terms_json'] = ''
    if str(vars.get('single_revision_state') or '') != 'exhausted':
        vars['single_revision_state'] = 'clarify'
finally:
    vars['single_corrected_terms_json'] = ''
    vars['confirmation_status'] = 'changed'
    vars['validation_state'] = 'invalid'
    vars['proposal_validation_state'] = 'invalid'
    vars['pending_promises_json'] = '[]'
    vars['readback_terms_json'] = '[]'
    vars['confirmation_readback'] = ''
"""


_SINGLE_DATE_CAPTURE_CODE = """import json
try:
    products = json.loads(str(vars.get('product_json') or '[]'))
    if len(products) != 1:
        raise ValueError('Expected one product')
    product = products[0]
    date = str(vars.get('single_exact_date_words') or '').strip() or str(product.get('deadline') or '')
    if not date:
        raise ValueError('No exact day or deadline')
    amount = vars.get('single_date_amount')
    vars['proposed_terms_json'] = json.dumps([dict(product_id=product.get('product_id'),
                                               amount=amount if amount is not None else product.get('past_due'),
                                               date=date)])
    vars['single_date_state'] = 'ready'
except Exception:
    vars['single_date_state'] = 'invalid'
    vars['proposed_terms_json'] = ''
finally:
    vars['single_exact_date_words'] = ''
    vars['single_date_amount'] = None
"""


_SINGLE_CLOSE_CODE = """try:
    vars['single_product_reason'] = __REASON__
    vars['proposed_terms_json'] = ''
    vars['pending_promises_json'] = '[]'
    vars['validation_state'] = 'invalid'
    vars['confirmation_status'] = 'declined'
except Exception:
    pass
"""


_SINGLE_ROBOT_ROUTE_CODE = """try:
    action = str(vars.get('discussion_action') or '')
    terms = str(vars.get('proposed_terms_json') or '').strip()
    amount_intent = str(vars.get('single_amount_intent') or '')
    vars['single_robot_next'] = ('single_proposal_gate' if action == 'proposal_ready' and terms else
                                 'single_floor_prepare' if amount_intent == 'full_too_much' and str(vars.get('single_floor_stage') or '0') == '0' else
                                 'single_exact_date' if action == 'vague_date' else
                                 'prepare_notes' if action == 'done_no_promise' else 'account_discussion')
    vars['single_robot_question'] = 'no'
except Exception:
    vars['single_robot_next'] = 'account_discussion'
    vars['single_robot_question'] = 'no'
"""


_SINGLE_FLOOR_PREPARE_CODE = """import json
try:
    products = json.loads(str(vars.get('product_json') or '[]'))
    if len(products) != 1:
        raise ValueError('Expected one product')
    product = products[0]
    date = str(vars.get('last_business_day_offer') or '')
    amount = float(product.get('floor') or 0)
    if not date or amount <= 0:
        raise ValueError('No fixed floor offer')
    vars['single_floor_offer_terms_json'] = json.dumps([dict(product_id=product.get('product_id'), amount=amount, date=date)])
    vars['single_floor_offer_text'] = ('The minimum payment for ' + str(product.get('label') or 'this product') +
                                       ' is ' + format(amount, '.2f') + ' on ' + date +
                                       '. The rest stays overdue. Can you make that payment?')
    vars['single_floor_stage'] = '1'
    vars['single_floor_action'] = 'unclear'
    vars['single_amount_intent'] = 'none'
    vars['single_floor_state'] = 'ready'
except Exception:
    vars['single_floor_state'] = 'invalid'
"""


_SINGLE_REFUSAL_PREPARE_CODE = """import json
try:
    products = json.loads(str(vars.get('product_json') or '[]'))
    if len(products) != 1:
        raise ValueError('Expected one product')
    product = products[0]
    date = str(vars.get('last_business_day_offer') or '')
    if not date:
        raise ValueError('No source-derived first offer day')
    amount = float(product.get('past_due') or 0)
    vars['single_floor_offer_terms_json'] = json.dumps([dict(product_id=product.get('product_id'), amount=amount, date=date)])
    vars['single_floor_offer_text'] = ('For ' + str(product.get('label') or 'this product') +
                                       ', can you pay ' + format(amount, '.2f') + ' on ' + date + '?')
    vars['single_floor_stage'] = '1'
    vars['single_floor_action'] = 'unclear'
    vars['single_refusal_intent'] = 'no'
    vars['single_floor_state'] = 'ready'
except Exception:
    vars['single_floor_state'] = 'invalid'
"""


_SINGLE_PROPOSAL_GATE_CODE = """import json
import re
try:
    products = json.loads(str(vars.get('product_json') or '[]'))
    if len(products) != 1:
        raise ValueError('Expected one product')
    product = products[0]
    raw = vars.get('proposed_terms_json') or ''
    raw = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
    raw = raw.get('promises') or raw.get('products') or [raw] if isinstance(raw, dict) else raw
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
        raise ValueError('No one-product proposal')
    term = raw[0]
    pid = str(product.get('product_id') or '')
    if str(term.get('product_id') or pid) != pid:
        raise ValueError('Wrong product')
    floor = float(product.get('floor') or 0)
    amount_raw = term.get('amount')
    words = str(amount_raw or '').lower().strip()
    if words in ('floor', 'minimum', 'minimum payment', 'instalment', 'installment'):
        amount = floor
    elif words in ('full', 'full amount', 'past due', 'past_due') or amount_raw is None:
        amount = float(product.get('past_due') or 0)
    else:
        amount = parse_payment_amount(amount_raw)
    date = str(term.get('date_phrase') or term.get('date_words') or term.get('raw_date') or term.get('date') or
               vars.get('proposed_date_words') or '').strip()
    vars['single_proposal_gate_state'] = 'normal'
    if amount < floor and date:
        vars['single_floor_offer_terms_json'] = json.dumps([dict(product_id=pid, amount=floor, date=date)])
        vars['single_floor_offer_text'] = ('I cannot record ' + format(amount, '.2f') +
                                           ' because it is below the minimum payment of ' + format(floor, '.2f') +
                                           '. The rest stays overdue. Can you pay ' + format(floor, '.2f') +
                                           ' on ' + date + '?')
        vars['single_floor_action'] = 'unclear'
        vars['single_proposal_gate_state'] = 'below_floor'
except Exception:
    vars['single_proposal_gate_state'] = 'normal'
"""


_SINGLE_FLOOR_CAPTURE_CODE = """import json
try:
    vars['proposed_terms_json'] = ''
    offered = json.loads(str(vars.get('single_floor_offer_terms_json') or '[]'))
    if not isinstance(offered, list) or len(offered) != 1:
        raise ValueError('No fixed offer')
    term = offered[0]
    action = str(vars.get('single_floor_action') or '')
    if action == 'accepted':
        chosen = term
    elif action in ('proposal', 'date_needed'):
        raw = vars.get('single_floor_reply_terms_json') or ''
        raw = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
        raw = raw[0] if isinstance(raw, list) and len(raw) == 1 else raw
        raw = raw if isinstance(raw, dict) else dict()
        pid = str(raw.get('product_id') or term.get('product_id') or '')
        if pid != str(term.get('product_id') or ''):
            raise ValueError('Wrong product')
        amount = raw.get('amount') if raw.get('amount') is not None else term.get('amount')
        date = str(raw.get('date_phrase') or raw.get('date_words') or raw.get('raw_date') or raw.get('date') or '').strip()
        if not date:
            vars['single_date_amount'] = amount
            vars['single_floor_capture_state'] = 'needs_date'
        else:
            chosen = dict(product_id=pid, amount=amount, date=date)
    else:
        raise ValueError('No accepted terms')
    if action == 'accepted' or (action in ('proposal', 'date_needed') and date):
        vars['proposed_terms_json'] = json.dumps([chosen])
        vars['single_floor_capture_state'] = 'ready'
except Exception:
    vars['single_floor_capture_state'] = 'needs_date'
finally:
    vars['single_floor_reply_terms_json'] = ''
"""


_SINGLE_FLOOR_NEXT_CODE = """import json
try:
    products = json.loads(str(vars.get('product_json') or '[]'))
    if len(products) != 1:
        raise ValueError('Expected one product')
    product = products[0]
    stage = int(vars.get('single_floor_stage') or 1)
    limit = int(product.get('attempts') or 1)
    if stage < limit and product.get('deadline'):
        amount = float(product.get('floor') or 0)
        date = str(product.get('deadline'))
        vars['single_floor_offer_terms_json'] = json.dumps([dict(product_id=product.get('product_id'), amount=amount, date=date)])
        vars['single_floor_offer_text'] = ('For ' + str(product.get('label') or 'this product') + ', can you pay ' +
                                           format(amount, '.2f') + ' on ' + date + '?')
        vars['single_floor_stage'] = str(stage + 1)
        vars['single_floor_action'] = 'unclear'
        vars['single_floor_state'] = 'ready'
    else:
        vars['single_product_reason'] = __REFUSAL_REASON__
        vars['single_floor_state'] = 'close'
except Exception:
    vars['single_floor_state'] = 'invalid'
"""


_SELECT_PROMISE_CODE = """import json
try:
    for name in ('create_promise_call', 'retry_promise_call'):
        for suffix in ('response.status', 'response.status_code', 'response.data.__RESULT_FLAG__', 'status_code'):
            vars[name + '_' + suffix] = ''
    vars['pending_promise_product_id'] = ''
    vars['write_state'] = 'done'
    if str(vars.get('confirmation_status') or '') != 'confirmed' or str(vars.get('validation_state') or '') != 'ready':
        vars['write_state'] = 'invalid'
    else:
        promises = json.loads(str(vars.get('pending_promises_json') or '[]'))
        index = int(vars.get('write_index') or 0)
        if index < len(promises):
            item = promises[index]
            vars['pending_product_id'] = str(item['product_id'])
            vars['pending_amount'] = item['amount']
            vars['pending_date'] = str(item['date'])
            vars['write_state'] = 'promise'
except Exception:
    vars['write_state'] = 'invalid'
"""


_ADVANCE_PROMISE_CODE = """try:
    import json
    recorded = json.loads(str(vars.get('recorded_ids') or '[]'))
    pid = str(vars.get('pending_product_id') or '')
    if pid and pid not in recorded:
        recorded.append(pid)
    vars['recorded_ids'] = json.dumps(recorded)
    vars['recorded_count'] = str(len(recorded))
    vars['write_index'] = str(int(vars.get('write_index') or 0) + 1)
except Exception:
    vars['write_index'] = '1'
"""


_MARK_PROMISE_ERROR_CODE = """import json
try:
    notes = json.loads(str(vars.get('pending_note_overrides') or '{}'))
    pid = str(vars.get('pending_product_id') or '')
    if pid:
        notes[pid] = __SERVICE_ERROR_REASON__
    vars['pending_note_overrides'] = json.dumps(notes)
    vars['write_index'] = str(int(vars.get('write_index') or 0) + 1)
except Exception:
    vars['write_index'] = str(int(vars.get('write_index') or 0) + 1)
"""


_PREPARE_NOTES_CODE = """import json
try:
    products = json.loads(str(vars.get('product_json') or '[]'))
    recorded = set(json.loads(str(vars.get('recorded_ids') or '[]')))
    known = json.loads(str(vars.get('note_reasons') or '{}'))
    overrides = json.loads(str(vars.get('pending_note_overrides') or '{}'))
    known.update(overrides)
    raw = vars.get('pending_note_reasons_json') or ''
    if isinstance(raw, str) and raw.strip():
        raw = json.loads(raw)
    if isinstance(raw, dict):
        for pid, reason in raw.items():
            if str(reason) in __NOTE_CODES__:
                known[str(pid)] = str(reason)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                pid = str(item.get('product_id') or '')
                reason = str(item.get('reason') or '')
                if pid and reason in __NOTE_CODES__:
                    known[pid] = reason
    if len(products) == 1:
        pid = str(products[0].get('product_id') or '')
        if pid not in recorded and pid not in known:
            reason = str(vars.get('single_product_reason') or '')
            known[pid] = reason if reason in __NOTE_CODES__ else __REFUSAL_REASON__
        vars['note_reasons'] = json.dumps(known)
    else:
        for item in products:
            pid = str(item.get('product_id') or '')
            if pid not in recorded and pid not in known:
                known[pid] = __REFUSAL_REASON__
    items = [dict(product_id=str(item.get('product_id') or ''), reason=known.get(str(item.get('product_id') or '')))
             for item in products if str(item.get('product_id') or '') not in recorded and len(products) > 1]
    vars['pending_notes_json'] = json.dumps(items)
    vars['note_index'] = '0'
    vars['note_write_state'] = 'notes' if items else 'done'
except Exception:
    vars['pending_notes_json'] = '[]'
    vars['note_write_state'] = 'done'
"""


_SELECT_NOTE_CODE = """import json
try:
    notes = json.loads(str(vars.get('pending_notes_json') or '[]'))
    index = int(vars.get('note_index') or 0)
    if index < len(notes):
        item = notes[index]
        vars['pending_note_product_id'] = str(item['product_id'])
        vars['pending_note_reason'] = str(item['reason'])
        vars['note_write_state'] = 'notes'
    else:
        vars['note_write_state'] = 'done'
except Exception:
    vars['note_write_state'] = 'done'
"""


_ADVANCE_NOTE_CODE = """try:
    import json
    notes = json.loads(str(vars.get('note_reasons') or '{}'))
    pid = str(vars.get('pending_note_product_id') or '')
    reason = str(vars.get('pending_note_reason') or '')
    if pid and reason:
        notes[pid] = reason
    vars['note_reasons'] = json.dumps(notes)
    vars['note_index'] = str(int(vars.get('note_index') or 0) + 1)
except Exception:
    vars['note_index'] = '1'
"""


_SKIP_NOTE_CODE = """try:
    vars['note_write_error'] = 'yes'
    vars['note_index'] = str(int(vars.get('note_index') or 0) + 1)
except Exception:
    vars['note_write_error'] = 'yes'
    vars['note_index'] = '1'
"""


_MULTI_ADVANCE_CODE = """import json
import re
try:
    products = json.loads(str(vars.get('product_json') or '[]'))
    product_by_id = dict((str(item.get('product_id') or ''), item) for item in products)
    terms = json.loads(str(vars.get('multi_terms_json') or '{}'))
    notes = json.loads(str(vars.get('multi_notes_json') or '{}'))
    floor_first = set(json.loads(str(vars.get('multi_floor_first_json') or '[]')))
    def resolve_pid(value, fallback=''):
        name = re.sub(r'\\s+', ' ', str(value or '').lower().replace('_', ' ')).strip()
        if not name:
            return fallback if fallback in product_by_id else ''
        if name in product_by_id:
            return name
        matches = []
        for known_pid, product in product_by_id.items():
            label = re.sub(r'\\s+', ' ', str(product.get('label') or '').lower().replace('_', ' ')).strip()
            suffix_match = re.search(r'\\bending (\\d{4})\\b', label)
            aliases = {label, label.split(' ending ')[0]}
            if suffix_match:
                suffix = suffix_match.group(1)
                aliases.update((suffix, 'ending ' + suffix, 'card ' + suffix,
                                'card ending ' + suffix, 'credit card ' + suffix))
            if name in aliases:
                matches.append(known_pid)
        return matches[0] if len(matches) == 1 else ''
    def resolve_amount(value, product):
        due = float(product.get('past_due') or 0)
        floor = float(product.get('floor') or 0)
        if value is None:
            return due
        if isinstance(value, str):
            words = value.lower().strip()
            if words in ('full', 'full amount', 'past due', 'past_due'):
                return due
            if words in ('floor', 'minimum', 'minimum payment', 'instalment', 'installment'):
                return floor
            try:
                number = parse_payment_amount(value)
            except ValueError:
                return None
        else:
            try:
                number = parse_payment_amount(value)
            except ValueError:
                return None
        return number if floor <= number <= due else None
    def note_reason(value):
        reason = str(value or '').strip().lower().replace(' ', '_')
        if reason in ('paid', 'already_paid', 'previously_paid', 'paid_already'):
            reason = __PAID_REASON__
        elif reason in ('dispute', 'disputed', 'not_mine', 'never_had_product'):
            reason = __DISPUTE_REASON__
        return reason if reason in __NOTE_CODES__ else ''
    def parsed(value):
        if isinstance(value, str):
            value = json.loads(value) if value.strip() else []
        if isinstance(value, dict):
            value = value.get('promises') or value.get('products') or [value]
        return value if isinstance(value, list) else []
    def add_terms(value, fallback_pid=''):
        added = False
        for item in parsed(value):
            if not isinstance(item, dict):
                continue
            pid = resolve_pid(item.get('product_id'), fallback_pid)
            if pid not in product_by_id or pid in notes:
                continue
            date = str(item.get('date_phrase') or item.get('date_words') or item.get('raw_date') or item.get('date') or '').strip()
            if not date:
                continue
            amount = resolve_amount(item.get('amount'), product_by_id[pid])
            if amount is None:
                continue
            terms[pid] = dict(product_id=pid, amount=amount, date=date)
            notes.pop(pid, None)
            added = True
        return added
    phase = str(vars.get('multi_phase') or 'initial')
    next_step = ''
    if phase == 'initial':
        disclosure = str(vars.get('multi_disclosure_state') or 'none')
        raw_notes = '{}' if disclosure in ('incomplete', 'hardship') else (vars.get('pending_note_reasons_json') or '{}')
        raw_notes = json.loads(raw_notes) if isinstance(raw_notes, str) else raw_notes
        if isinstance(raw_notes, dict):
            for raw_pid, raw_reason in raw_notes.items():
                claim_pid = resolve_pid(raw_pid)
                reason = note_reason(raw_reason)
                if claim_pid and reason and reason != __REFUSAL_REASON__:
                    notes[claim_pid] = reason
                    terms.pop(claim_pid, None)
        if str(vars.get('all_today_status') or '') == 'yes':
            today = str(vars.get('now') or '')[:10]
            for pid, item in product_by_id.items():
                if pid not in notes:
                    terms[pid] = dict(product_id=pid, amount=item.get('past_due'), date=today)
        else:
            common = str(vars.get('multi_common_date_words') or '').strip()
            if common:
                for pid, item in product_by_id.items():
                    if pid not in notes:
                        terms[pid] = dict(product_id=pid, amount=item.get('past_due'), date=common)
            add_terms(vars.get('proposed_terms_json') or '')
            if disclosure != 'incomplete':
                for pid in parsed(vars.get('multi_full_too_much_ids_json') or '[]'):
                    resolved = resolve_pid(pid)
                    if resolved:
                        floor_first.add(resolved)
        vars['multi_offer_stage'] = '1'
    else:
        pid = str(vars.get('multi_focus_product_id') or '')
        action = str(vars.get('multi_offer_action') or 'unclear')
        if phase == 'disclosure_completion':
            raw_claims = vars.get('multi_reply_notes_json') or '{}'
            claims = json.loads(raw_claims) if isinstance(raw_claims, str) else raw_claims
            resolved_claims = []
            if isinstance(claims, dict):
                for raw_pid, raw_reason in claims.items():
                    target = resolve_pid(raw_pid)
                    reason = note_reason(raw_reason)
                    if target and reason in (__PAID_REASON__, __DISPUTE_REASON__):
                        resolved_claims.append((target, reason))
                    else:
                        next_step = 'disclosure'
                        break
            if next_step == 'disclosure':
                rounds = int(vars.get('multi_disclosure_rounds') or 0) + 1
                vars['multi_disclosure_rounds'] = str(rounds)
                next_step = 'disclosure' if rounds < 2 else 'service_error'
            else:
                for target, reason in resolved_claims:
                    terms.pop(target, None)
                    notes[target] = reason
                add_terms(vars.get('multi_reply_terms_json') or '', pid)
        elif phase == 'identify':
            target = resolve_pid(vars.get('multi_reply_product_id'))
            claim = str(vars.get('multi_claim_action') or '')
            if target and claim in ('paid', 'dispute'):
                terms.pop(target, None)
                notes[target] = __PAID_REASON__ if claim == 'paid' else __DISPUTE_REASON__
                vars['multi_claim_action'] = ''
                vars['multi_claim_attempts'] = '0'
                if target != pid and pid not in terms and pid not in notes:
                    next_step = 'clarify'
            else:
                attempts = int(vars.get('multi_claim_attempts') or 0) + 1
                vars['multi_claim_attempts'] = str(attempts)
                next_step = 'identify' if attempts < 2 else 'service_error'
        elif phase == 'support':
            if action == 'proposal':
                add_terms(vars.get('multi_reply_terms_json') or '', pid)
            if pid not in terms and pid not in notes:
                notes[pid] = __HARDSHIP_REASON__
        elif action == 'accepted' and phase == 'offer':
            if pid in notes:
                pass
            elif str(vars.get('multi_fixed_offer') or '') == 'yes':
                terms[pid] = dict(product_id=pid, amount=vars.get('multi_offer_amount'), date=str(vars.get('multi_offer_date') or ''))
                notes.pop(pid, None)
            elif not add_terms(vars.get('multi_reply_terms_json') or '', pid):
                next_step = 'clarify'
        elif action == 'proposal':
            common = str(vars.get('multi_reply_common_date_words') or '').strip()
            if common:
                for item in products:
                    other = str(item.get('product_id') or '')
                    if other not in terms and other not in notes:
                        terms[other] = dict(product_id=other, amount=item.get('past_due'), date=common)
            add_terms(vars.get('multi_reply_terms_json') or '', pid)
            if pid not in terms and pid not in notes:
                next_step = 'clarify'
        elif action in ('paid', 'dispute'):
            target = resolve_pid(vars.get('multi_reply_product_id'))
            if target:
                terms.pop(target, None)
                notes[target] = __PAID_REASON__ if action == 'paid' else __DISPUTE_REASON__
                if target != pid and pid not in terms and pid not in notes:
                    next_step = 'clarify'
            else:
                vars['multi_claim_action'] = action
                vars['multi_claim_attempts'] = '0'
                next_step = 'identify'
        elif action == 'hardship':
            next_step = 'support'
        elif action == 'full_too_much' and pid not in floor_first:
            floor_first.add(pid)
            next_step = 'offer'
        elif phase == 'clarify' and action != 'proposal':
            stage = int(vars.get('multi_offer_stage') or 1)
            limit = int(product_by_id.get(pid, dict()).get('attempts') or 1)
            if stage < limit:
                next_step = 'offer'
                vars['multi_offer_stage'] = str(stage + 1)
            else:
                notes[pid] = __REFUSAL_REASON__
        else:
            stage = int(vars.get('multi_offer_stage') or 1)
            limit = int(product_by_id.get(pid, dict()).get('attempts') or 1)
            if stage < limit:
                vars['multi_offer_stage'] = str(stage + 1)
                next_step = 'offer'
            else:
                notes[pid] = __REFUSAL_REASON__
        vars['multi_reply_terms_json'] = ''
        vars['multi_reply_notes_json'] = ''
        vars['multi_reply_common_date_words'] = ''
        vars['multi_reply_product_id'] = ''
    unresolved = [(index, str(item.get('product_id') or '')) for index, item in enumerate(products)
                  if str(item.get('product_id') or '') not in terms and str(item.get('product_id') or '') not in notes]
    if next_step == 'service_error':
        vars['multi_next'] = 'service_error'
    elif next_step in ('support', 'clarify', 'identify', 'disclosure'):
        vars['multi_phase'] = next_step
        vars['multi_next'] = next_step
        if next_step != 'disclosure':
            vars['multi_offer_action'] = 'unclear'
    elif next_step == 'offer' and pid in product_by_id and pid not in terms and pid not in notes:
        vars['multi_phase'] = 'offer'
        vars['multi_next'] = 'offer'
    elif unresolved:
        index, focus = unresolved[0]
        vars['multi_focus_index'] = str(index)
        vars['multi_focus_product_id'] = focus
        vars['multi_offer_stage'] = '1'
        vars['multi_phase'] = 'offer'
        vars['multi_next'] = 'offer'
        if phase == 'initial' and not terms and not notes and disclosure in ('incomplete', 'hardship'):
            vars['multi_focus_label'] = str(product_by_id[focus].get('label') or focus)
            vars['multi_phase'] = 'disclosure' if disclosure == 'incomplete' else 'support'
            vars['multi_next'] = vars['multi_phase']
            vars['multi_disclosure_rounds'] = '0'
    else:
        vars['multi_next'] = 'confirm' if terms else 'notes'
        vars['proposed_terms_json'] = json.dumps([terms[str(item.get('product_id') or '')] for item in products
                                                  if str(item.get('product_id') or '') in terms])
        vars['pending_note_reasons_json'] = json.dumps(notes)
    vars['multi_terms_json'] = json.dumps(terms)
    vars['multi_notes_json'] = json.dumps(notes)
    vars['multi_floor_first_json'] = json.dumps(sorted(floor_first))
except Exception:
    vars['multi_next'] = 'service_error'
"""


_MULTI_DISCLOSURE_RESOLVE_CODE = """import json
try:
    answer = str(vars.get('multi_disclosure_followup') or 'incomplete')
    claim_present = str(vars.get('multi_disclosure_claim_present') or 'no') == 'yes'
    raw_terms = str(vars.get('multi_reply_terms_json') or '').strip()
    raw_claims = str(vars.get('multi_reply_notes_json') or '').strip()
    try:
        has_terms = bool(json.loads(raw_terms)) if raw_terms else False
    except (TypeError, ValueError):
        has_terms = False
    try:
        decoded_claims = json.loads(raw_claims) if raw_claims else {}
        has_claims = isinstance(decoded_claims, dict) and bool(decoded_claims)
    except (TypeError, ValueError):
        has_claims = False
    if answer == 'hardship':
        vars['multi_reply_terms_json'] = ''
        vars['multi_reply_notes_json'] = ''
        vars['multi_phase'] = 'support'
        vars['multi_next'] = 'support'
    elif ((answer == 'proposal' and has_terms and (not claim_present or has_claims)) or
          (answer == 'claim' and has_claims)):
        vars['multi_phase'] = 'disclosure_completion'
        vars['multi_next'] = 'advance'
    elif answer == 'unrelated' and not claim_present:
        vars['multi_reply_terms_json'] = ''
        vars['multi_reply_notes_json'] = ''
        vars['multi_phase'] = 'offer'
        vars['multi_next'] = 'offer'
    elif answer == 'full_too_much':
        vars['multi_reply_terms_json'] = ''
        vars['multi_reply_notes_json'] = ''
        floor_first = set(json.loads(str(vars.get('multi_floor_first_json') or '[]')))
        floor_first.add(str(vars.get('multi_focus_product_id') or ''))
        vars['multi_floor_first_json'] = json.dumps(sorted(floor_first))
        vars['multi_phase'] = 'offer'
        vars['multi_next'] = 'offer'
    else:
        vars['multi_reply_terms_json'] = ''
        vars['multi_reply_notes_json'] = ''
        rounds = int(vars.get('multi_disclosure_rounds') or 0) + 1
        vars['multi_disclosure_rounds'] = str(rounds)
        vars['multi_phase'] = 'disclosure'
        vars['multi_next'] = 'disclosure' if rounds < 2 else 'service_error'
except Exception:
    vars['multi_next'] = 'service_error'
"""


_MULTI_OFFER_CODE = """import json
try:
    products = json.loads(str(vars.get('product_json') or '[]'))
    pid = str(vars.get('multi_focus_product_id') or '')
    product = next(item for item in products if str(item.get('product_id') or '') == pid)
    stage = int(vars.get('multi_offer_stage') or 1)
    floor_first = set(json.loads(str(vars.get('multi_floor_first_json') or '[]')))
    if stage == 1:
        amount = product.get('floor') if pid in floor_first else product.get('past_due')
        date = str(vars.get('last_business_day_offer') or '')
    else:
        amount = product.get('floor')
        date = str(product.get('deadline') or '')
    fixed = __FIXED_MULTI_OFFERS__
    if fixed and not date:
        raise ValueError('No source-derived offer date')
    vars['multi_offer_amount'] = amount
    vars['multi_offer_date'] = date
    vars['multi_focus_label'] = str(product.get('label') or pid)
    vars['multi_fixed_offer'] = 'yes' if fixed else 'no'
    remainder = ('The rest would stay overdue. ' if float(amount) < float(product.get('past_due') or 0)
                 and pid in floor_first and stage == 1 else '')
    vars['multi_offer_text'] = ((remainder + 'For ' + vars['multi_focus_label'] + ', can you pay ' +
                                 format(float(amount), '.2f') + ' on ' + date + '?') if fixed else '')
    vars['multi_offer_action'] = 'unclear'
    vars['multi_offer_state'] = 'ready'
except Exception:
    vars['multi_offer_state'] = 'invalid'
    vars['multi_offer_text'] = ''
"""


_MULTI_REVISE_CODE = """import json
import datetime
import re
__PAYMENT_DATE_PARSER__
try:
    attempts = int(vars.get('multi_correction_attempts') or 0) + 1
except Exception:
    attempts = 1
vars['multi_correction_attempts'] = str(attempts)
try:
    terms = json.loads(str(vars.get('multi_terms_json') or '{}'))
    products = json.loads(str(vars.get('product_json') or '[]'))
    product_by_id = dict((str(item.get('product_id') or ''), item) for item in products)
    def resolve_pid(value):
        name = re.sub(r'\\s+', ' ', str(value or '').lower().replace('_', ' ')).strip()
        if not name:
            return ''
        matches = []
        for pid, product in product_by_id.items():
            label = re.sub(r'\\s+', ' ', str(product.get('label') or '').lower().replace('_', ' ')).strip()
            aliases = {pid.lower(), label, label.split(' ending ')[0]}
            suffix = re.search(r'\\bending (\\d{4})\\b', label)
            if suffix:
                digits = suffix.group(1)
                aliases.update((digits, 'ending ' + digits, 'card ' + digits,
                                'card ending ' + digits, 'credit card ' + digits))
            if name in aliases:
                matches.append(pid)
        return matches[0] if len(matches) == 1 else ''
    raw = vars.get('multi_corrected_terms_json') or ''
    raw = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
    if isinstance(raw, list) and raw and all(not isinstance(item, dict) for item in raw):
        if len(raw) != 3 or not all(str(item).strip() for item in raw):
            raise ValueError('Incomplete positional correction')
        raw = [dict(product_id=raw[0], amount=raw[1], date=raw[2])]
    elif isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise ValueError('No corrected terms')
    today = datetime.datetime.fromisoformat(str(vars.get('now') or '').replace('Z', '+00:00')).date()
    changed = False
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError('Malformed corrected terms')
        pid = resolve_pid(item.get('product_id'))
        if not pid or pid in seen:
            raise ValueError('Unknown or ambiguous corrected product')
        seen.add(pid)
        date = str(item.get('date_phrase') or item.get('date_words') or item.get('date') or '').strip()
        if not date:
            raise ValueError('Corrected payment date missing')
        day = parse_payment_date(date, today)
        if day < today:
            raise ValueError('Corrected payment date in past')
        previous = terms.get(pid) or dict()
        amount = parse_payment_amount(item.get('amount', previous.get('amount')))
        product = product_by_id[pid]
        if not float(product.get('floor') or 0) <= amount <= float(product.get('past_due') or 0):
            raise ValueError('Corrected amount outside approved range')
        terms[pid] = dict(product_id=pid, amount=amount, date=date)
        changed = True
    vars['multi_terms_json'] = json.dumps(terms)
    vars['proposed_terms_json'] = json.dumps([terms[str(item.get('product_id') or '')] for item in products
                                              if str(item.get('product_id') or '') in terms])
    vars['multi_corrected_terms_json'] = ''
    vars['multi_next'] = 'confirm' if changed else 'service_error' if attempts >= 3 else 'clarify'
except Exception:
    vars['multi_next'] = 'service_error' if attempts >= 3 else 'clarify'
finally:
    vars['multi_corrected_terms_json'] = ''
"""


def build_workflow(policy: dict, openapi: dict) -> dict:
    """Return a runnable workflow using the supplied policy and OpenAPI schema.

    Extra policy keys are ignored. Missing prose sections fall back to ``raw_brief``.
    """
    openapi = normalize_openapi(openapi)
    policy = policy or {}
    brief = str(policy.get("raw_brief") or "")
    prose = "\n".join(str(policy.get(key) or "") for key in
                      ("global_rules", "opening", "identity", "verified_call"))
    tool_policy = policy.get("tool_policy") or {}
    if not isinstance(tool_policy, dict):
        tool_policy = {}
    account_path, account_method, _ = _api_spec(openapi, "get_account")
    account_operation = openapi["paths"][account_path][account_method.lower()]
    collection_field, response_schema = account_response_collection(account_operation)
    item_schema = response_schema.get("properties", {}).get(collection_field, {}).get("items", {})
    item_bindings = product_item_fields(item_schema)
    identity_path, identity_method, _ = _api_spec(openapi, "verify_identity")
    identity_response = identity_response_fields(openapi["paths"][identity_path][identity_method.lower()])
    holidays = _holiday_dates(policy)
    weekdays = business_weekdays(policy)
    bands = _deadline_bands(policy)
    identity_selector, _ = identity_selector_field(_api_spec(openapi, "verify_identity")[2])
    identity_fields = _identity_fields(openapi, policy)
    outcomes = _outcome_names(openapi, policy)
    reason_names, close_reason_names, note_codes, close_codes = _reason_names(openapi, policy)
    promise_success_codes, promise_result_flag, promise_false_codes, promise_no_write_codes = _promise_response_contract(openapi)
    promise_fields = promise_request_fields(_api_spec(openapi, "create_promise")[2])
    note_fields = note_request_fields(_api_spec(openapi, "add_note")[2])
    transfer_fields = transfer_request_fields(_api_spec(openapi, "transfer_to_agent")[2])
    close_fields = close_request_fields(_api_spec(openapi, "close_call")[2])
    promise_bindings = {promise_fields["product"]: "{{pending_product_id}}",
                        promise_fields["amount"]: "{{pending_amount}}",
                        promise_fields["date"]: "{{pending_date}}"}
    promise_hook = (_PROMISE_HOOK
                    .replace("__SUCCESS_CODES__", repr(promise_success_codes))
                    .replace("__RESULT_FLAG__", repr(promise_result_flag))
                    .replace("__FALSE_CODES__", repr(promise_false_codes))
                    .replace("__NO_WRITE_CODES__", repr(promise_no_write_codes)))
    select_promise_code = _SELECT_PROMISE_CODE.replace("__RESULT_FLAG__", promise_result_flag)
    bank_name = _bank_name(policy)
    source_policy = "\n".join(str(policy.get(key) or "") for key in
                              ("global_rules", "opening", "identity", "verified_call", "outcomes", "tool_policy", "constraints", "raw_brief"))
    offer_last_business_day = "last business day of this week" in source_policy.lower()
    fixed_single_floor_offer = offer_last_business_day and bool(re.search(r"offer the floor,? once", source_policy, re.I))
    multi_enabled = requires_multi_product(policy)
    # Extracted summaries can omit the source's exact offer schedule. Keep the
    # source text in the decision so an authorized offer can be spoken as fixed
    # TTS, without a free-form model turn before the bank write.
    offer_source = "\n".join(str(policy.get(key) or "") for key in ("raw_brief", "verified_call"))
    fixed_multi_offers = ("last business day of this week" in offer_source.lower() and
                          bool(re.search(r"attempt\s*2.{0,100}floor.{0,100}deadline", offer_source, re.I)))

    verify_hook = (_VERIFY_HOOK.replace("__COMPLETE_FIELD__", repr(identity_response["complete"]))
                   .replace("__ANSWER_FIELD__", repr(identity_response["answer"]))
                   .replace("__REMAINING_FIELD__", repr(identity_response["remaining"])))
    verify_dob = _api_tool(openapi, "verify_identity", fixed={identity_selector: identity_fields[0]},
                           description=f"Check the customer's {identity_fields[0].replace('_', ' ')} after their own answer. Never use a third party's answer.", post_code=verify_hook)
    verify_phone = _api_tool(openapi, "verify_identity", fixed={identity_selector: identity_fields[1]},
                             description=f"Check the customer's {identity_fields[1].replace('_', ' ')} after their own answer. Never use a third party's answer.", post_code=verify_hook)
    callback = _api_tool(openapi, "schedule_callback", description="Book a valid callback after a third party gives an exact day and time.", post_code=_CALLBACK_HOOK)
    callback_schema = _api_spec(openapi, "schedule_callback")[2]
    callback_date_fields = [field for field, spec in (callback_schema.get("properties") or {}).items()
                            if isinstance(spec, dict) and (spec.get("format") == "date" or
                                re.search(r"(?:^|_)(?:date|day)$", field))]
    if len(callback_date_fields) != 1:
        raise ValueError("schedule_callback OpenAPI must identify one callback date field")
    callback["pre_process_code"] = f"""import datetime
body = config.get('body')
if not isinstance(body, dict):
    raise ValueError('Callback body is missing')
day = datetime.date.fromisoformat(str(body.get({callback_date_fields[0]!r}) or ''))
today = datetime.datetime.fromisoformat(str(vars.get('now') or '').replace('Z', '+00:00')).date()
if day < today:
    raise ValueError('Callback date is in the past')
if day > today and (day.weekday() not in {weekdays!r} or day.isoformat() in {holidays!r}):
    raise ValueError('Callback date is not a business day')
"""
    account = _api_tool(openapi, "get_account", description="Load late products only after both security answers have passed.")
    promise = _api_tool(openapi, "create_promise", description="Record one confirmed, valid promise for one product. Retry once only after an explicit bank response says nothing was recorded.", post_code=promise_hook)
    note = _api_tool(openapi, "add_note", description="For a multi-product account only, record each product without a promise as refused, hardship, dispute, paid or service_error. Never add a note on a transfer.", post_code=_NOTE_HOOK)
    transfer_hook = _TRANSFER_HOOK.replace("transfer_to_agent_args.queue",
                                           "transfer_to_agent_args." + transfer_fields["queue"])
    transfer = _api_tool(openapi, "transfer_to_agent", description="Transfer only for accepted support, persistent human request, or restructuring. Never add notes on a transfer.", post_code=transfer_hook)
    promise["pre_process_code"] = """try:
    body = config.get('body')
    if isinstance(body, dict):
        vars['pending_promise_product_id'] = str(body.get(__PRODUCT_FIELD__) or '')
except Exception:
    pass
""".replace("__PRODUCT_FIELD__", repr(promise_fields["product"]))
    note["pre_process_code"] = """try:
    body = config.get('body')
    if isinstance(body, dict):
        vars['pending_note_product_id'] = str(body.get(__PRODUCT_FIELD__) or '')
        vars['pending_note_reason'] = str(body.get(__REASON_FIELD__) or '')
except Exception:
    pass
""".replace("__PRODUCT_FIELD__", repr(note_fields["product"])).replace("__REASON_FIELD__", repr(note_fields["reason"]))
    transfer["pre_process_code"] = """try:
    body = config.get('body')
    if isinstance(body, dict):
        vars['pending_transfer_queue'] = str(body.get(__QUEUE_FIELD__) or '')
except Exception:
    pass
""".replace("__QUEUE_FIELD__", repr(transfer_fields["queue"]))
    close_bindings = {close_fields["outcome"]: "{{close_outcome}}"}
    if close_fields["reason"]:
        close_bindings[close_fields["reason"]] = "{{close_reason}}"
    close = _api_tool(openapi, "close_call", fixed=close_bindings,
                      description="Close the call exactly once, after farewell. This is the last bank API call.")
    close["pre_process_code"] = """try:
    body = config.get('body')
    if isinstance(body, dict) and __REASON_FIELD__ and not body.get(__REASON_FIELD__):
        body.pop(__REASON_FIELD__, None)
except Exception:
    pass
""".replace("__REASON_FIELD__", repr(close_fields["reason"]))

    identity_base = ("You are still before identity verification. Follow the policy's confidentiality and disclosure rules exactly; do not reveal account details. "
                     f"If asked whether you are AI, identify yourself truthfully as {bank_name}'s digital assistant. If asked whether this is a scam, follow the policy's verification guidance. "
                     "Never invent information. Keep replies short. After every tool result, speak or call the next tool; do not go silent. ")
    nodes: dict[str, dict] = {}
    nodes["opening"] = _conversation(
        "opening", identity_base +
        "Determine whether the person is the named customer. Only an explicit yes, 'that's me', 'speaking', or the customer's own name confirms. "
        "A hello, go ahead or who is this does not confirm; ask once more after answering any question. "
        "For a third party ask only when the customer can be reached, without explaining the purpose. No tool call is allowed in this turn.",
        opening=_opening_line(policy, bank_name),
        extraction=[_enum("opening_role", ["self", "third_party", "wrong_number", "deceased", "unclear"],
                          "Classify ONLY the latest caller answer: self only on explicit confirmation; third_party if another person answered; wrong_number if number is not theirs; deceased if customer died; unclear otherwise.")],
        transitions=[_eq("finish_wrong_number", "opening_role", "wrong_number"),
                     _eq("finish_deceased", "opening_role", "deceased"),
                     _eq("identity_dob", "opening_role", "self"),
                     _eq("third_party", "opening_role", "third_party"),
                     _to("finish_unclear", "Second identity ask remained unclear",
                         _when({"logic": "and", "conditions": [
                             _check("opening_role", "equals", "unclear"),
                             _check("opening.current_turns", "greater_than", 1),
                         ]}))],
        max_turns=3, max_target="finish_unclear")

    nodes["third_party"] = _conversation(
        "third_party", identity_base +
        "A third party answered. Ask when the named customer can be reached. Apply the exact callback dates, hours, and retry limit in the policy. "
        "Use {{now}} and these bank holidays from the policy: " + (", ".join(holidays) if holidays else "none specified") + ". "
        "Business weekdays from the source are " + ", ".join(("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")[day] for day in weekdays) + ". "
        "Calculate the next business day from {{now}}, these weekdays, and the listed holidays. "
        "If a proposal is outside the permitted window, follow the policy's correction rule. "
        "If an exact valid day/time is accepted, call schedule_callback with ISO date and HH:MM, then follow its result. "
        "Do not ask security questions, share account details, or accept an offer to pay from this person. "
        "If they say the customer has died, follow the policy's bereavement response and ask nothing else. "
        "After any tool result speak or call another tool; never go silent.",
        tools=[callback],
        extraction=[_enum("third_party_status", ["deceased", "wrong_number", "no_callback", "waiting"],
                          "Classify the latest answer. no_callback only after refusal or a second invalid callback proposal; waiting otherwise.")],
        transitions=[_eq("finish_callback", "callback_recorded", "yes", "Callback was successfully booked"),
                     _eq("finish_deceased", "third_party_status", "deceased"),
                     _eq("finish_wrong_number", "third_party_status", "wrong_number"),
                     _eq("finish_no_callback", "third_party_status", "no_callback")],
        max_turns=5, max_target="finish_no_callback")

    dob_prompt = identity_base + (f"Ask for the customer's {identity_fields[0].replace('_', ' ')}, one security question at a time. "
                                  "Format answers exactly as required by the supplied API and policy. "
                                  "Call verify_identity for each answer. Follow any service hint and remaining-attempt count exactly. "
                                  "If an answer fails, ask this same question again. On final failure follow the policy's escalation guidance. "
                                  "A first refusal or question about the call purpose routes to the one-time security explanation. A second refusal or call-purpose question closes through the workflow. Never print tool-call syntax or claim the call is already closed. "
                                  "Do not reveal the account. After tool result, speak or call the next tool.")
    phone_prompt = identity_base + (f"The first security answer passed. Ask only for the customer's {identity_fields[1].replace('_', ' ')}. "
                                    f"Call verify_identity with each answer. Do not ask {identity_fields[0].replace('_', ' ')} again. "
                                    "Use the service's remaining-attempt count and the policy's failure rule. "
                                    "A first refusal or call-purpose question routes to the one-time security explanation. A second refusal or call-purpose question closes through the workflow. Never print tool-call syntax or claim the call is already closed. "
                                    "After tool result, speak or call the next tool.")
    identity_transitions = lambda success: [
        _bool("load_account" if success else "identity_phone", "verify_identity_response.data." + identity_response["complete" if success else "answer"], "Security answer passed"),
        _to("finish_identity_failed", "Security attempts exhausted", _when(_check("verify_identity_response.data." + identity_response["remaining"], "equals", "0"))),
        _to("identity_decline", "Purpose question without a security answer",
            _when({"logic": "and", "conditions": [
                _check("identity_purpose_question", "equals", "yes"),
                {"logic": "or", "conditions": [
                    _check("identity_intent", "equals", "other"),
                    _check("identity_intent", "equals", "declined"),
                ]},
            ]})),
        _eq("identity_decline", "identity_intent", "declined"),
    ]
    nodes["identity_dob"] = _conversation("identity_dob", dob_prompt, tools=[verify_dob],
                                           extraction=[_enum("identity_intent", ["declined", "answered", "other"],
                                                             f"Classify ONLY the latest caller answer. declined if the caller refuses the security question OR asks what the call is about, why you are calling, or why security is needed before answering—even if they do not say no. answered only if they give {identity_fields[0].replace('_', ' ')}; other otherwise. A call-purpose question must not be other."),
                                                       _enum("identity_purpose_question", ["yes", "no"],
                                                             "yes if the latest caller answer asks what this call is about, why you are calling, or demands the purpose before giving security details. no otherwise. Treat this independently of whether identity_intent is declined or other.")],
                                           transitions=identity_transitions(False), max_turns=8, max_target="finish_identity_failed")
    nodes["identity_phone"] = _conversation("identity_phone", phone_prompt, tools=[verify_phone],
                                             extraction=[_enum("identity_intent", ["declined", "answered", "other"],
                                                               f"Classify ONLY the latest caller answer. declined if the caller refuses the security question OR asks what the call is about, why you are calling, or why security is needed before answering—even if they do not say no. answered only if they give {identity_fields[1].replace('_', ' ')}; other otherwise. A call-purpose question must not be other."),
                                                         _enum("identity_purpose_question", ["yes", "no"],
                                                               "yes if the latest caller answer asks what this call is about, why you are calling, or demands the purpose before giving security details. no otherwise. Treat this independently of whether identity_intent is declined or other.")],
                                             transitions=identity_transitions(True), max_turns=8, max_target="finish_identity_failed")
    nodes["identity_decline"] = _conversation(
        "identity_decline", identity_base +
        f"The one-time security explanation and request for {identity_fields[0].replace('_', ' ')} were spoken on entry. Wait for the answer. "
        "A further refusal or call-purpose question must route to finish_identity_declined so the engine closes the call. "
        "If they answer, return to the pending security question. Never print tool-call syntax or claim the call has already closed.",
        opening=f"The security questions protect your account, so I cannot discuss the call before verifying you. Could you please tell me your {identity_fields[0].replace('_', ' ')}?",
        extraction=[_enum("decline_followup", ["declined", "answer", "other"],
                          f"Latest answer: declined for any further refusal OR question about the call purpose before security, including 'what is this call about?'; answer only if the caller gives {identity_fields[0].replace('_', ' ')}; other otherwise.")],
        transitions=[_eq("finish_identity_declined", "decline_followup", "declined"),
                     _eq("identity_dob", "decline_followup", "answer")],
        max_turns=2, max_target="finish_identity_declined")
    # A verified DOB is never reasked just because the caller declined the phone question.
    nodes["identity_phone"]["transitions"][2]["target_node"] = "phone_decline"
    nodes["identity_phone"]["transitions"][3]["target_node"] = "phone_decline"
    nodes["phone_decline"] = _conversation(
        "phone_decline", identity_base +
        f"The one-time security explanation and request for {identity_fields[1].replace('_', ' ')} were spoken on entry. Wait for the answer. "
        "A second refusal or call-purpose question must route to finish_identity_declined for an engine close. Never print tool-call syntax.",
        opening=("The security questions protect your account, so I cannot discuss the call before verifying you. "
                 + ("Could you please tell me the last four digits of your phone number?" if identity_fields[1] == "phone_last4"
                    else f"Could you please tell me your {identity_fields[1].replace('_', ' ')}?")),
        extraction=[_enum("phone_decline_followup", ["declined", "answer", "other"],
                          f"Latest answer: declined for a further refusal OR question about the call purpose before security; answer only if the caller gives {identity_fields[1].replace('_', ' ')}; other otherwise.")],
        transitions=[_eq("finish_identity_declined", "phone_decline_followup", "declined"),
                     _eq("identity_phone", "phone_decline_followup", "answer")],
        max_turns=2, max_target="finish_identity_declined")

    nodes["load_account"] = {
        "name": "load_account", "node_type": "api_request", "on_enter": {"type": "silent"},
        "passthrough": True, "passthrough_default_transition": "prepare_account",
        "pre_actions": [account], "response_mapping": [{"path": "data." + collection_field, "variable": "account_products"}],
        "transitions": [_always("prepare_account")],
    }
    nodes["prepare_account"] = _code_node("prepare_account", _prepare_code(holidays, weekdays, item_bindings, bands, offer_last_business_day, collection_field), "account_discussion")
    nodes["prepare_account"]["transitions"] = [
        _eq("account_unavailable", "product_count", "0"), _always("account_discussion")]
    nodes["account_unavailable"] = _conversation(
        "account_unavailable", "The account service supplied no usable products. Apologize briefly, do not invent an amount, and say goodbye. No tool call is allowed in this turn.",
        transitions=[_always("finish_service_error")], max_turns=1, max_target="finish_service_error")

    account_prompt = (
        f"Identity is verified. Speak as {bank_name}'s digital assistant, briefly and truthfully. "
        "State the account information and handle each product exactly as directed by the full source policy in the system prompt. "
        "Account facts, in service order:\n{{account_facts}}\n"
        "Date facts calculated from the supplied call variable now: {{date_facts}}. Never use the real clock. "
        "Use each product's own deadline and amount floor when the source policy requires them. Handle proposed amounts, specific or vague dates, invalid dates, refusals, hardship, paid and dispute claims under the source policy. "
        "Honor the source policy's attempt limits and order. Never invent figures, dates, reasons, discounts or concessions. "
        "For a single product, after the initial today question, stop speaking on a flat no with no date or amount; the workflow will make the counted dated offer. Do not improvise a persuasion offer in this node. "
        "For more than one product, state all products before an ask and follow the source policy's product order and combined confirmation rules. "
        "When the caller has proposed specific terms, stop after acknowledging them. The next step will read them back for confirmation. Do not say they are recorded yet. "
        "If a prior read-back was corrected or validation failed, clarify the changed product, amount or date before offering another read-back. On a date validation error, ask for the exact day; a bare yes to the old read-back is not a new proposal. "
        "For a flat refusal, paid claim, dispute, or hardship with no promise, finish the discussion under the source policy. "
        "Do not say goodbye while a payment proposal remains unconfirmed. No tool call is allowed in this node."
    )
    single_intent = lambda intent: _when({"logic": "and", "conditions": [
        _check("product_count", "equals", "1"), _check("single_sensitive_intent", "equals", intent),
    ]})
    transitions = [
        _eq("transfer_human", "discussion_action", "transfer_human"),
        _to("single_hardship", "Hardship takes priority over restructuring", single_intent("hardship")),
        _to("single_paid_notice", "Current past-due amount was paid",
            _when({"logic": "and", "conditions": [
                _check("product_count", "equals", "1"),
                _check("single_current_due_paid_claim", "equals", "yes"),
            ]})),
        _to("transfer_restructuring", "Caller requests lower instalments or a new payment plan",
            _when({"logic": "and", "conditions": [
                _check("product_count", "equals", "1"),
                _check("single_restructuring_request", "equals", "yes"),
            ]})),
        _eq("transfer_restructuring", "discussion_action", "transfer_restructuring"),
        _eq("transfer_support", "discussion_action", "transfer_support"),
        _to("single_robot_route", "Answer a one-product robot question before continuing",
            _when({"logic": "and", "conditions": [
                _check("product_count", "equals", "1"),
                _check("single_robot_question", "equals", "yes"),
            ]})),
    ]
    if fixed_single_floor_offer:
        transitions.append(_to("single_floor_prepare", "Offer the one-product floor on the first policy date",
                               _when({"logic": "and", "conditions": [
                                   _check("product_count", "equals", "1"),
                                   _check("single_amount_intent", "equals", "full_too_much"),
                                   _check("single_floor_stage", "equals", "0"),
                               ]})))
        transitions.append(_to("single_refusal_prepare", "Make the first counted one-product offer",
                               _when({"logic": "and", "conditions": [
                                   _check("product_count", "equals", "1"),
                                   _check("single_refusal_intent", "equals", "yes"),
                                   _check("single_floor_stage", "equals", "0"),
                               ]})))
    if multi_enabled:
        transitions.append(_to("multi_advance", "Begin ordered product discussion after the all-today answer",
                               _when(_check("product_count", "greater_than", 1))))
        single = lambda action: _when({"logic": "and", "conditions": [
            _check("product_count", "equals", "1"), _check("discussion_action", "equals", action),
        ]})
        transitions.extend([
            _to("single_exact_date", "Ask once for a precise single-product payment day", single("vague_date")),
            _to("single_proposal_gate", "Check one-product amount before confirmation", single("proposal_ready")),
            _to("prepare_notes", "Single product final answer", single("done_no_promise")),
        ])
    else:
        transitions.extend([
            _eq("single_exact_date", "discussion_action", "vague_date"),
            _eq("single_proposal_gate", "discussion_action", "proposal_ready"),
            _eq("prepare_notes", "discussion_action", "done_no_promise"),
        ])
    nodes["account_discussion"] = _conversation("account_discussion", account_prompt, transitions=transitions,
                                                 extraction=[
                                                     _enum("discussion_action", ["proposal_ready", "vague_date", "done_no_promise", "transfer_restructuring", "transfer_support", "transfer_human", "continue"],
                                                           "Classify ONLY the latest caller answer using the source policy. proposal_ready when the caller gives a specific payment day, including today or a weekday; for a one-product account use its full past-due amount when the caller names only a date. vague_date when a one-product caller intends to pay but gives only a vague period such as 'the week after next'; ask for an exact day. A bare yes/I confirm to an earlier read-back in this node is continue, never proposal_ready. done_no_promise only when all products have final paid/dispute/hardship/refused reasons and no transfer is wanted. A no to today without another date is continue while the workflow makes a counted first offer. Transfer actions take priority except hardship takes priority over restructuring."),
                                                     _enum("single_sensitive_intent", ["hardship", "paid_current_due", "none"],
                                                           "For a one-product account classify ONLY the latest caller answer. hardship if the caller mentions illness, a death in the family, or job loss, even alongside a request for lower instalments. paid_current_due only if they say this current past-due amount was already paid; a product closed or paid off in the past is a dispute, not this intent. none otherwise. Hardship takes priority over restructuring."),
                                                     _enum("single_current_due_paid_claim", ["yes", "no"],
                                                           "For a one-product account, classify ONLY the LATEST caller answer about the current product and the past-due amount just stated. yes only if the caller reports that this current past-due amount was already paid, including 'I already paid it in the app this morning', 'I paid it in the app this morning', or 'I actually paid that in the app this morning' immediately after the account question. no if they only paid off or closed this product last year, paid last month's instalment while the current amount remains due, have paid this account in the past, promise to pay tomorrow or later, paid a different product, or deny owing or ever having this product. These historical or debt-denial claims are disputes, not payment of the current past-due amount.",
                                                           min_confidence=0.55),
                                                     _enum("single_restructuring_request", ["yes", "no"],
                                                           "For a one-product account, yes if the LATEST caller answer explicitly asks for lower instalments, smaller recurring payments, restructuring, or a new payment plan. Keep yes when they also say the amount is unaffordable. This is independent of discussion_action and single_amount_intent. no otherwise."),
                                                     _enum("single_robot_question", ["yes", "no"],
                                                           "For a one-product account, yes if the LATEST caller answer asks whether they are speaking to a robot, AI, automated agent, or digital assistant. Keep yes even if the same answer also proposes a payment date or amount. no otherwise."),
                                                     _enum("single_amount_intent", ["full_too_much", "none"],
                                                           "For a one-product account, full_too_much only if the LATEST caller answer explicitly says the FULL past-due amount is too high or unaffordable, before any dated persuasion offer. A refusal of a specific dated offer, such as 'I cannot pay Friday' or 'I won't have the money then', is never full_too_much. Do not classify a specific date or amount proposal as this intent. none otherwise."),
                                                     _enum("single_refusal_intent", ["yes", "no"],
                                                           "For one product, yes only when the latest caller answer flatly refuses the INITIAL pay-today question without proposing any amount or day. Say yes for 'No, I cannot pay today.' Say no for a refusal of a dated offer, a specific payment proposal, hardship, paid claim, or transfer request."),
                                                     {"name": "proposed_terms_json", "type": "string", "min_confidence": 0.55,
                                                      "description": "Only when the latest caller answer proposes or accepts payment terms with a date, return a JSON array string with objects product_id, amount, date. Preserve the caller's raw date words verbatim in date, such as 'today', 'this Friday', 'next Friday', '30 October', or an ISO date; NEVER convert a relative date to a year or use the system clock. Code will resolve it from call now {{now}}. Use account facts {{account_facts}} for product ID and numeric full amount. For multiple products include each agreed product with its own raw date phrase. Leave empty for a bare yes/I confirm, no date, or no terms."},
                                                     {"name": "proposed_date_words", "type": "string", "min_confidence": 0.55,
                                                      "description": "For a one-product proposal only, copy the exact date phrase spoken in the LATEST caller answer, verbatim, such as 'this Friday'. Do not convert it to a date. Leave empty if the latest answer contains no date phrase, including a bare yes/I confirm."},
                                                     {"name": "pending_note_reasons_json", "type": "string", "min_confidence": 0.55,
                                                      "description": "For each product the caller has finally refused, disputed, said already paid, or declined after hardship, return a JSON object string mapping product_id to the correct add_note reason from the source policy and API. Leave empty if still discussing."},
                                                     _enum("single_product_reason", note_codes or ["paid", "dispute", "hardship", "refused", "service_error"],
                                                           "For a one-product account only, classify a final paid, dispute, hardship, or refused result; do not fill while negotiating a promise."),
                                                     *([_enum("all_today_status", ["yes", "no", "other"],
                                                              "Only for a multi-product account: yes if the latest caller answer accepts paying ALL products today; no if it rejects paying all today; other otherwise."),
                                                        _enum("multi_disclosure_state", ["incomplete", "hardship", "none"],
                                                              "For a multi-product account, classify ONLY the latest caller answer. hardship only when the caller clearly mentions illness, a death in the family, or job loss. incomplete when the caller trails off mid-disclosure and the missing words could determine whether a hardship rule applies; do not infer hardship or an ordinary refusal from an unfinished subject or clause. none when the answer is complete, including an ordinary no to paying all today or an unrelated statement such as losing a card. Do not treat inability to pay right now alone as hardship.",
                                                              min_confidence=0.55),
                                                        {"name": "multi_common_date_words", "type": "string", "min_confidence": 0.55,
                                                         "description": "For a multi-product account only, copy a date phrase explicitly offered for ALL products, such as 'next Friday'. Leave empty for a date applying to just one product."},
                                                        {"name": "multi_full_too_much_ids_json", "type": "string", "min_confidence": 0.55,
                                                         "description": "For a multi-product account only, return a JSON array of product IDs for which the caller explicitly says that product's FULL past-due amount is too high or unaffordable. 'I cannot pay all of them today' or 'I cannot pay all three of those today' only refuses the timing of the all-today question; neither says any product's full amount is too much, so return [] unless the caller separately names an unaffordable product or amount. Do not infer that every product needs its floor from a no-to-all-today answer."}]
                                                       if multi_enabled else []),
                                                 ],
                                                 max_turns=2 if multi_enabled else 40,
                                                 max_target="multi_advance" if multi_enabled else "prepare_notes")
    nodes["account_discussion"]["on_enter_first_entry_only"] = True

    nodes["single_robot_route"] = _code_node("single_robot_route", _SINGLE_ROBOT_ROUTE_CODE, "single_robot_answer")
    nodes["single_robot_answer"] = _conversation(
        "single_robot_answer", "The truthful digital-assistant disclosure was spoken on entry. Continue to the preserved payment proposal without asking the caller to repeat it.",
        opening=f"Yes, I am {bank_name}'s digital assistant.",
        transitions=[_eq("single_proposal_gate", "single_robot_next", "single_proposal_gate"),
                     _eq("single_floor_prepare", "single_robot_next", "single_floor_prepare"),
                     _eq("single_exact_date", "single_robot_next", "single_exact_date"),
                     _eq("prepare_notes", "single_robot_next", "prepare_notes"),
                     _always("account_discussion")],
        max_turns=1, max_target="account_discussion")
    nodes["single_robot_answer"]["passthrough"] = True
    nodes["single_robot_answer"]["passthrough_default_transition"] = "account_discussion"

    nodes["single_proposal_gate"] = _code_node(
        "single_proposal_gate", _with_amount_parser(_SINGLE_PROPOSAL_GATE_CODE), "prepare_confirmation")
    nodes["single_proposal_gate"]["transitions"] = [
        _eq("single_below_floor_offer", "single_proposal_gate_state", "below_floor"),
        _always("prepare_confirmation")]
    nodes["single_below_floor_offer"] = _conversation(
        "single_below_floor_offer",
        "The customer's proposed amount was below the permitted floor. The exact floor, caller's date, and rest-overdue warning were spoken on entry. Wait for acceptance. Never ask how much they can pay. If they refuse, use the first counted policy offer. No tool call is allowed.",
        opening="{{single_floor_offer_text}}",
        extraction=[_enum("single_floor_action", ["accepted", "proposal", "date_needed", "refused", "unclear"],
                          "accepted if the caller clearly agrees to the offered minimum amount and day. proposal if they independently give a new exact amount or day. date_needed if they accept the minimum but ask for a different day without naming it. refused for no or repeating only the below-floor amount. unclear otherwise."),
                    {"name": "single_floor_reply_terms_json", "type": "string", "min_confidence": 0.55,
                     "description": "For proposal only, return a JSON object with any new amount and raw date words from the latest caller answer; omit unchanged fields. Leave empty otherwise."}],
        transitions=[_eq("single_floor_capture", "single_floor_action", "accepted"),
                     _eq("single_floor_capture", "single_floor_action", "proposal"),
                     _eq("single_floor_capture", "single_floor_action", "date_needed"),
                     _eq("single_refusal_prepare", "single_floor_action", "refused"),
                     _eq("single_refusal_prepare", "single_floor_action", "unclear")],
        max_turns=2, max_target="single_refusal_prepare")
    nodes["single_refusal_prepare"] = _code_node("single_refusal_prepare", _SINGLE_REFUSAL_PREPARE_CODE, "service_unavailable")
    nodes["single_refusal_prepare"]["transitions"] = [
        _eq("single_floor_offer", "single_floor_state", "ready"), _always("service_unavailable")]

    nodes["single_floor_prepare"] = _code_node("single_floor_prepare", _SINGLE_FLOOR_PREPARE_CODE, "service_unavailable")
    nodes["single_floor_prepare"]["transitions"] = [
        _eq("single_floor_offer", "single_floor_state", "ready"), _always("service_unavailable")]
    nodes["single_floor_offer"] = _conversation(
        "single_floor_offer",
        "The fixed amount, date, and rest-overdue statement were spoken on entry. Wait for the caller's answer. Do not repeat the floor offer or ask how much they can pay. A clear yes accepts the exact stated offer. A changed date or amount needs a validated read-back. Refusal or non-answer advances the bounded policy attempt counter. No tool call is allowed.",
        opening="{{single_floor_offer_text}}",
        extraction=[_enum("single_floor_action", ["accepted", "proposal", "date_needed", "refused", "hardship", "paid_current_due", "transfer_restructuring", "transfer_support", "transfer_human", "unclear"],
                          "Classify ONLY the latest answer. accepted means a clear yes to the exact fixed amount and date. proposal means they give a new exact amount or day themselves. date_needed means they accept the floor amount but request a different day without naming it. refused includes 'I cannot do that' and a flat no. hardship means illness, a death in the family, or job loss; inability to afford the amount alone is not hardship. A request for lower instalments or a new payment plan is transfer_restructuring. Current-due paid claims take priority; other transfer requests follow the source policy. unclear is a non-answer. Do not count a caller's own proposed date as refusal."),
                    _enum("single_floor_hardship_signal", ["yes", "no"],
                          "yes only if the LATEST caller answer mentions illness, a death in the family, or job loss, including alongside a request for lower instalments. Inability to afford the amount alone is no."),
                    _enum("single_floor_restructuring_request", ["yes", "no"],
                          "yes if the LATEST caller answer explicitly requests lower instalments, smaller recurring payments, restructuring, or a new payment plan, even if they also say this offer is unaffordable. no otherwise."),
                    {"name": "single_floor_reply_terms_json", "type": "string", "min_confidence": 0.55,
                     "description": "For proposal only, return one JSON object containing any amount and exact RAW date words the caller gave in the latest answer. Omit unchanged amount; leave empty when no exact terms were given. Do not use the system clock."}],
        transitions=[_eq("single_hardship", "single_floor_hardship_signal", "yes"),
                     _eq("single_paid_notice", "single_floor_action", "paid_current_due"),
                     _eq("transfer_human", "single_floor_action", "transfer_human"),
                     _eq("transfer_restructuring", "single_floor_restructuring_request", "yes"),
                     _eq("single_hardship", "single_floor_action", "hardship"),
                     _eq("transfer_support", "single_floor_action", "transfer_support"),
                     _eq("transfer_restructuring", "single_floor_action", "transfer_restructuring"),
                     _eq("single_floor_capture", "single_floor_action", "accepted"),
                     _eq("single_floor_capture", "single_floor_action", "proposal"),
                     _eq("single_floor_capture", "single_floor_action", "date_needed"),
                     _eq("single_floor_next", "single_floor_action", "refused"),
                     _eq("single_floor_next", "single_floor_action", "unclear")],
        max_turns=2, max_target="single_floor_next")
    nodes["single_floor_capture"] = _code_node("single_floor_capture", _SINGLE_FLOOR_CAPTURE_CODE, "single_exact_date")
    nodes["single_floor_capture"]["transitions"] = [
        _eq("prepare_confirmation", "single_floor_capture_state", "ready"),
        _always("single_exact_date")]
    nodes["single_floor_next"] = _code_node(
        "single_floor_next",
        _SINGLE_FLOOR_NEXT_CODE.replace("__REFUSAL_REASON__", repr(reason_names.get("refused") or "refused")),
        "service_unavailable")
    nodes["single_floor_next"]["transitions"] = [
        _eq("single_floor_offer", "single_floor_state", "ready"),
        _eq("prepare_notes", "single_floor_state", "close"),
        _always("service_unavailable")]

    nodes["single_exact_date"] = _conversation(
        "single_exact_date",
        "The caller gave a vague payment period. Ask for one exact day within the permitted window, then wait. Do not claim a promise was recorded. No tool call is allowed.",
        opening="What exact day would you be able to pay?",
        extraction=[{"name": "single_exact_date_words", "type": "string", "min_confidence": 0.55,
                     "description": "Copy only the exact day phrase in the latest answer, verbatim, such as 'Monday the 26th of October'. Leave empty if the reply remains vague or gives no day. Do not convert the date or use the system clock."}],
        transitions=[_always("single_date_capture")], max_turns=2, max_target="single_date_capture")
    nodes["single_date_capture"] = _code_node("single_date_capture", _SINGLE_DATE_CAPTURE_CODE, "service_unavailable")
    nodes["single_date_capture"]["transitions"] = [
        _eq("prepare_confirmation", "single_date_state", "ready"), _always("service_unavailable")]

    nodes["single_hardship"] = _conversation(
        "single_hardship",
        "The caller disclosed hardship. The one short sympathy sentence and support offer were spoken on entry. Wait for the latest answer. Do not persuade, repeat the sympathy sentence, or describe a restructuring plan. If they decline support, close as hardship. If they themselves propose an exact payment day or amount with a day, validate that proposal. No tool call is allowed here.",
        opening="I'm sorry to hear that. Would you like me to connect you with our customer support team?",
        extraction=[_enum("single_support_action", ["accepted", "declined", "proposal", "transfer_human", "unclear"],
                          "Classify the latest answer to the support offer. accepted if they want support; declined if they refuse support or do not want to continue; proposal only if they themselves propose payment with an exact day; transfer_human if they explicitly request a human without accepting support; unclear otherwise. Never turn hardship plus a lower-instalment request into restructuring."),
                    {"name": "proposed_terms_json", "type": "string", "min_confidence": 0.55,
                     "description": "Only if the latest caller answer independently proposes a payment with an exact day, return a one-element JSON array with product_id, amount, and raw date words. Use full past-due amount if only the day is stated. Leave empty otherwise."}],
        transitions=[_eq("transfer_support", "single_support_action", "accepted"),
                     _eq("transfer_human", "single_support_action", "transfer_human"),
                     _eq("prepare_confirmation", "single_support_action", "proposal"),
                     _eq("single_hardship_declined", "single_support_action", "declined")],
        max_turns=2, max_target="single_hardship_declined")
    nodes["single_hardship_declined"] = _code_node(
        "single_hardship_declined",
        _SINGLE_CLOSE_CODE.replace("__REASON__", repr(reason_names.get("hardship") or "hardship")), "prepare_notes")
    paid_notice = ("Payments can take one business day to appear."
                   if "payments can take one business day to appear" in source_policy.lower()
                   else "Thank you for letting me know.")
    nodes["single_paid_notice"] = _conversation(
        "single_paid_notice", "Acknowledge the caller's payment and close. Do not seek a promise or persuade. No tool call is allowed.",
        opening=paid_notice, transitions=[_always("single_paid_mark")],
        max_turns=1, max_target="single_paid_mark")
    nodes["single_paid_notice"]["passthrough"] = True
    nodes["single_paid_notice"]["passthrough_default_transition"] = "single_paid_mark"
    nodes["single_paid_mark"] = _code_node(
        "single_paid_mark", _SINGLE_CLOSE_CODE.replace("__REASON__", repr(reason_names.get("paid") or "paid")),
        "prepare_notes")

    if multi_enabled:
        multi_advance_code = (_MULTI_ADVANCE_CODE
                              .replace("__NOTE_CODES__", repr(note_codes))
                              .replace("__REFUSAL_REASON__", repr(reason_names.get("refused") or "refused"))
                              .replace("__PAID_REASON__", repr(reason_names.get("paid") or "paid"))
                              .replace("__DISPUTE_REASON__", repr(reason_names.get("dispute") or "dispute"))
                              .replace("__HARDSHIP_REASON__", repr(reason_names.get("hardship") or "hardship")))
        nodes["multi_advance"] = _code_node(
            "multi_advance", _with_amount_parser(multi_advance_code), "service_unavailable")
        nodes["multi_advance"]["transitions"] = [
            _eq("prepare_confirmation", "multi_next", "confirm"),
            _eq("prepare_notes", "multi_next", "notes"),
            _eq("multi_select_offer", "multi_next", "offer"),
            _eq("multi_support", "multi_next", "support"),
            _eq("multi_disclosure_clarify", "multi_next", "disclosure"),
            _eq("multi_clarify", "multi_next", "clarify"),
            _eq("multi_identify", "multi_next", "identify"),
            _always("service_unavailable"),
        ]
        nodes["multi_disclosure_clarify"] = _conversation(
            "multi_disclosure_clarify",
            "The caller's last statement was unfinished. The neutral request to finish was spoken on entry. Wait for the caller's completion. Do not infer hardship, make a payment offer, ask for a payment date, or change product while their meaning is unclear. If the answer is still incomplete, briefly ask them to finish again. No tool call is allowed.",
            opening="Sorry, I didn't catch the end of that. Could you finish what you were saying?",
            extraction=[_enum("multi_disclosure_followup", ["hardship", "unrelated", "full_too_much", "proposal", "claim", "incomplete", "transfer_restructuring", "transfer_support", "transfer_human"],
                              "Classify the caller's completion together with the unfinished preceding statement. hardship only if the completed meaning clearly reports illness, death in the family, or job loss. proposal if the caller gives their own payment amount or date, EVEN WHEN the same answer also says another product was paid or disputed; extract every intent separately and do not count a proposal as a refusal. claim if there is a paid or dispute claim but no proposal. full_too_much only if they explicitly say the current product's full past-due amount is too high; do not infer it from inability to pay right now. unrelated if it completes an ordinary payment refusal or another matter without a hardship fact, including a lost card. incomplete if the meaning remains cut off or ambiguous; do not guess hardship from an unfinished fragment. Use transfer actions only for an explicit request."),
                        {**_enum("multi_disclosure_claim_present", ["yes", "no"],
                                 "Independently of any payment proposal, yes if this completed answer explicitly claims the CURRENT past-due amount on any product was already paid or disputes that a product belongs to the caller. A lost card, an old payment, or unfinished words without a completed claim are no. If yes, map every such claim in multi_reply_notes_json; never silently drop it."),
                         "min_confidence": 0.55},
                        {"name": "multi_reply_terms_json", "type": "string", "min_confidence": 0.45,
                         "description": "If the latest completed answer gives a fresh payment proposal, return a JSON array string of product_id, amount, and RAW date words for each product explicitly covered. Extract this independently of paid or dispute claims on other products. Leave empty otherwise."},
                        {"name": "multi_reply_notes_json", "type": "string", "min_confidence": 0.45,
                         "description": "For EACH product explicitly claimed paid or disputed in the completed answer, return a JSON object string mapping its exact product_id or unambiguous product label from {{account_facts}} to paid or dispute. Paid means the CURRENT past-due amount was already paid; a product closed or paid off in the past is a dispute. Extract this independently of any payment proposal on another product. Do not infer a claim from an unfinished phrase or a lost card. Leave empty if there is no completed claim."}],
            transitions=[_eq("transfer_restructuring", "multi_disclosure_followup", "transfer_restructuring"),
                         _eq("transfer_support", "multi_disclosure_followup", "transfer_support"),
                         _eq("transfer_human", "multi_disclosure_followup", "transfer_human"),
                         _always("multi_disclosure_resolve")],
            max_turns=3, max_target="multi_disclosure_resolve")
        nodes["multi_disclosure_resolve"] = _code_node(
            "multi_disclosure_resolve", _MULTI_DISCLOSURE_RESOLVE_CODE, "service_unavailable")
        nodes["multi_disclosure_resolve"]["transitions"] = [
            _eq("multi_support", "multi_next", "support"),
            _eq("multi_select_offer", "multi_next", "offer"),
            _eq("multi_advance", "multi_next", "advance"),
            _eq("multi_disclosure_clarify", "multi_next", "disclosure"),
            _always("service_unavailable"),
        ]
        nodes["multi_select_offer"] = _code_node(
            "multi_select_offer", _MULTI_OFFER_CODE.replace("__FIXED_MULTI_OFFERS__", repr(fixed_multi_offers)),
            "service_unavailable")
        nodes["multi_select_offer"]["transitions"] = [
            _eq("multi_offer", "multi_offer_state", "ready"), _always("service_unavailable"),
        ]
        multi_reply_extraction = [
            _enum("multi_offer_action", ["accepted", "proposal", "refused", "full_too_much", "paid", "dispute", "hardship",
                                         "transfer_restructuring", "transfer_support", "transfer_human", "unclear"],
                  "Classify only the latest answer about the current product. accepted means yes to the exact fixed offer just spoken. proposal means the caller gave a new exact amount or date. refused includes a flat no or non-answer. full_too_much means the full amount is explicitly too high. Paid, dispute, hardship and transfer requests take priority."),
            {"name": "multi_reply_terms_json", "type": "string", "min_confidence": 0.45,
             "description": "On a fresh proposal, return a JSON array string of product_id, amount, and the caller's RAW date words. Include every product the caller explicitly covered; leave empty otherwise. For an accepted model-generated offer, repeat the exact offered terms."},
            {"name": "multi_reply_common_date_words", "type": "string", "min_confidence": 0.55,
             "description": "Copy a date phrase only when the latest answer explicitly applies that ONE date to all remaining products. Otherwise leave empty."},
            {"name": "multi_reply_product_id", "type": "string", "min_confidence": 0.55,
             "description": "For a paid or dispute claim, identify the product the LATEST caller answer refers to using bank facts {{account_facts}}. Return its exact product ID; a known card suffix or exact product label is also accepted. If the caller says only 'that' or 'this' and names no other product, use current focus {{multi_focus_product_id}}. If the target is ambiguous, leave empty. Never assign a named different product's claim to the current focus."},
        ]
        transfer_routes = [
            _eq("transfer_restructuring", "multi_offer_action", "transfer_restructuring"),
            _eq("transfer_support", "multi_offer_action", "transfer_support"),
            _eq("transfer_human", "multi_offer_action", "transfer_human"),
        ]
        nodes["multi_offer"] = _conversation(
            "multi_offer",
            "Discuss only {{multi_focus_label}} at persuasion attempt {{multi_offer_stage}}. Follow the source policy and the supplied product facts. "
            "If the fixed offer was spoken on entry, wait for the caller's answer without making another offer. "
            "If no fixed offer was supplied, make exactly this attempt's source-authorized offer and wait. "
            "Do not claim a promise is recorded. No tool call is allowed in this node.",
            opening="{{multi_offer_text}}" if fixed_multi_offers else None,
            extraction=multi_reply_extraction,
            transitions=transfer_routes + [_always("multi_advance")],
            max_turns=3, max_target="multi_advance")
        nodes["multi_support"] = _conversation(
            "multi_support", "Give one short sentence of sympathy under the source policy, then offer customer support transfer for {{multi_focus_label}}. "
            "If declined, continue to the next product without a persuasion attempt. If the caller proposes a date or amount, capture it. No tool call is allowed in this node.",
            extraction=[_enum("multi_offer_action", ["accepted", "proposal", "refused", "transfer_restructuring", "transfer_support", "transfer_human", "unclear"],
                              "Classify only the latest answer to the support transfer offer for the current product. accepted means yes to the support transfer. proposal means the caller gives their own exact payment amount or date. refused means they decline support. Transfer requests take priority."),
                        *multi_reply_extraction[1:]],
            transitions=[_eq("transfer_support", "multi_offer_action", "accepted")] + transfer_routes + [_always("multi_advance")],
            max_turns=2, max_target="multi_advance")
        nodes["multi_clarify"] = _conversation(
            "multi_clarify", "Ask for one exact amount and day for {{multi_focus_label}} under the source policy. "
            "Do not repeat the previous offer or claim a promise is recorded. No tool call is allowed in this node.",
            extraction=multi_reply_extraction,
            transitions=transfer_routes + [_always("multi_advance")],
            max_turns=2, max_target="multi_advance")
        nodes["multi_identify"] = _conversation(
            "multi_identify", "The question was spoken on entry. Wait for the caller to identify the product for their paid or dispute claim. "
            "Do not make an offer or assume it concerns {{multi_focus_label}}. No tool call is allowed in this node.",
            opening="Which product do you mean?",
            extraction=[_enum("multi_offer_action", ["transfer_restructuring", "transfer_support", "transfer_human", "unclear"],
                              "Transfer requests take priority; otherwise classify as unclear."),
                        {"name": "multi_reply_product_id", "type": "string", "min_confidence": 0.55,
                         "description": "The caller is identifying the product for the paid or dispute claim made immediately before this question. Map their latest answer to the exact product ID in {{account_facts}}. A known card suffix or exact product label is also accepted. Leave empty if the answer does not identify one product."}],
            transitions=transfer_routes + [_always("multi_advance")],
            max_turns=2, max_target="multi_advance")
        nodes["multi_correction"] = _conversation(
            "multi_correction", "The fixed correction request was spoken on entry. Wait for the caller's answer. "
            "Do not speak, suggest, restate, or confirm a payment date in this node; the code must validate it first. "
            "Keep all other proposed terms. No tool call is allowed in this node.",
            opening="Please tell me which product you want to change, its amount, and the exact day and month you want to pay.",
            extraction=[{"name": "multi_corrected_terms_json", "type": "string", "min_confidence": 0.55,
                         "description": "Return a JSON array string containing only corrected product_id, amount and RAW date words from the latest caller answer. Leave empty if none."},
                        _enum("multi_correction_action", ["transfer_restructuring", "transfer_support", "transfer_human", "continue"],
                              "Transfer requests take priority; otherwise continue collecting corrected terms.")],
            transitions=[_eq("transfer_restructuring", "multi_correction_action", "transfer_restructuring"),
                         _eq("transfer_support", "multi_correction_action", "transfer_support"),
                         _eq("transfer_human", "multi_correction_action", "transfer_human"),
                         _always("multi_revise")],
            max_turns=3, max_target="service_unavailable")
        nodes["multi_revise"] = _code_node(
            "multi_revise", _with_amount_parser(
                _MULTI_REVISE_CODE.replace("__PAYMENT_DATE_PARSER__", _PAYMENT_DATE_SOURCE)),
            "multi_correction")
        nodes["multi_revise"]["transitions"] = [
            _eq("prepare_confirmation", "multi_next", "confirm"),
            _eq("service_unavailable", "multi_next", "service_error"),
            _always("multi_correction"),
        ]

    nodes["prepare_confirmation"] = _code_node(
        "prepare_confirmation", _with_amount_parser(
            _PREPARE_CONFIRMATION_CODE
            .replace("__PAYMENT_DATE_PARSER__", _PAYMENT_DATE_SOURCE)
            .replace("__HOLIDAYS__", repr(holidays))
            .replace("__WEEKDAYS__", repr(weekdays))), "account_discussion")
    nodes["prepare_confirmation"]["transitions"] = [
        _eq("confirmation", "proposal_validation_state", "ready"),
        *([_to("service_unavailable", "Correction attempts exhausted without valid terms",
               _when({"logic": "and", "conditions": [
                   _check("product_count", "greater_than", 1),
                   _check("proposal_validation_state", "equals", "invalid"),
                   _check("multi_correction_attempts", "greater_than", 2),
               ]}))] if multi_enabled else []),
        *([_to("multi_correction", "Correct the combined proposal",
               _when({"logic": "and", "conditions": [
                   _check("product_count", "greater_than", 1),
                   _check("proposal_validation_state", "equals", "invalid"),
               ]}))] if multi_enabled else []),
        _to("single_clarify", "Clarify an invalid single-product proposal",
            _when({"logic": "and", "conditions": [
                _check("product_count", "equals", "1"),
                _check("proposal_validation_state", "equals", "invalid"),
            ]})),
        _eq("account_discussion", "proposal_validation_state", "invalid"),
        _always("service_unavailable")]
    nodes["confirmation"] = _conversation(
        "confirmation",
        "The exact validated read-back was spoken on entry: {{confirmation_readback}}. Wait for the caller's answer. Only a clear yes to all stated terms counts as confirmation. If the caller changes a term, acknowledge it and obtain another validated read-back and confirmation. Do not claim a promise is recorded. No tool call is allowed in this node.",
        opening="{{confirmation_readback}} " + _confirmation_question(policy),
        extraction=[
            _enum("confirmation_status", ["confirmed", "changed", "declined", "transfer_restructuring", "transfer_support", "transfer_human", "unclear"],
                  "Classify ONLY the latest answer to the immediately preceding read-back. confirmed requires a clear yes to all stated product, amount and date terms. changed if any amount or date is corrected. declined if caller rejects the read-back. Transfer requests take priority over confirmation. An earlier yes elsewhere in the conversation is not confirmation."),
            {"name": "single_corrected_terms_json", "type": "string", "min_confidence": 0.55,
             "description": "For a one-product read-back correction only, return one JSON object with the changed amount and/or the changed date as the caller's raw words in date. Omit any unchanged field; do not infer consent to the prior amount/date. Examples: {\"date\":\"Monday the 19th of October\"}, {\"amount\":\"minimum payment\",\"date\":\"Monday the 19th of October\"}. Leave empty for a bare yes, decline, or transfer request."},
        ],
        transitions=[
            _eq("transfer_restructuring", "confirmation_status", "transfer_restructuring"),
            _eq("transfer_support", "confirmation_status", "transfer_support"),
            _eq("transfer_human", "confirmation_status", "transfer_human"),
            _eq("validate_confirmation", "confirmation_status", "confirmed"),
            _to("single_revise", "Validate the corrected one-product terms",
                _when({"logic": "and", "conditions": [
                    _check("product_count", "equals", "1"),
                    _check("confirmation_status", "equals", "changed"),
                ]})),
            _eq("clear_confirmation", "confirmation_status", "changed"),
            *([_to("multi_correction", "Correct declined combined read-back",
                   _when({"logic": "and", "conditions": [
                       _check("product_count", "greater_than", 1),
                       _check("confirmation_status", "equals", "declined"),
                   ]}))] if multi_enabled else []),
            _eq("prepare_notes", "confirmation_status", "declined"),
        ],
        max_turns=3, max_target="account_discussion")
    nodes["single_revise"] = _code_node("single_revise", _SINGLE_REVISE_CODE, "single_clarify")
    nodes["single_revise"]["transitions"] = [
        _eq("service_unavailable", "single_revision_state", "exhausted"),
        _eq("prepare_confirmation", "single_revision_state", "ready"),
        _always("single_clarify"),
    ]
    nodes["single_clarify"] = _conversation(
        "single_clarify",
        "The old confirmation is invalid. Ask for the exact corrected amount or day, then wait. Never treat a bare yes as confirmation; another validated read-back must occur. No tool call is allowed.",
        opening="Please tell me the exact amount or day you want to change.",
        extraction=[{"name": "single_corrected_terms_json", "type": "string", "min_confidence": 0.55,
                     "description": "Return one JSON object with the exact new amount and/or raw date phrase from the latest answer. Omit unchanged fields. Leave empty for a bare yes or an unclear answer."}],
        transitions=[_always("single_revise")], max_turns=3, max_target="service_unavailable")
    nodes["clear_confirmation"] = _code_node("clear_confirmation", _CLEAR_CONFIRMATION_CODE, "account_discussion")
    if multi_enabled:
        nodes["clear_confirmation"]["transitions"] = [
            _to("multi_correction", "Correct changed combined terms",
                _when(_check("product_count", "greater_than", 1))),
            _always("account_discussion"),
        ]
    # Validation also selects the first write, so a confirmed proposal needs
    # one sandbox call between the caller's yes and create_promise.
    validate_and_select_code = (_VALIDATE_CONFIRMATION_CODE.replace("__HOLIDAYS__", repr(holidays))
                                .replace("__WEEKDAYS__", repr(weekdays))
                                + "\n" + select_promise_code)
    nodes["validate_confirmation"] = _code_node(
        "validate_confirmation", validate_and_select_code, "account_discussion")
    nodes["validate_confirmation"]["transitions"] = [
        _eq("create_promise_call", "write_state", "promise"),
        *([_to("multi_correction", "Correct invalid combined terms",
               _when({"logic": "and", "conditions": [
                   _check("product_count", "greater_than", 1),
                   _check("write_state", "equals", "invalid"),
               ]}))] if multi_enabled else []),
        _eq("account_discussion", "write_state", "invalid"),
        _always("prepare_notes"),
    ]

    # Every write below is an engine-run action. The model cannot call a promise
    # tool, and the only path to the first promise action is a clear confirmation.
    nodes["record_results"] = _code_node("record_results", select_promise_code, "prepare_notes")
    nodes["record_results"]["transitions"] = [
        _eq("create_promise_call", "write_state", "promise"),
        _eq("account_discussion", "write_state", "invalid"),
        _always("prepare_notes"),
    ]
    promise_engine = _api_tool(openapi, "create_promise", fixed=promise_bindings,
        description="Record the explicitly confirmed and validated current product promise.", post_code=promise_hook)
    promise_engine["config"]["maxRetries"] = 0
    promise_engine["function"]["parameters"]["properties"]["connect_retries"] = {
        "type": "number", "default": 0,
    }
    promise_engine["pre_process_code"] = """try:
    for name in ('create_promise_call', 'retry_promise_call'):
        for suffix in ('response.status', 'response.status_code', 'response.data.__RESULT_FLAG__', 'status_code'):
            vars[name + '_' + suffix] = ''
    vars['pending_promise_product_id'] = ''
    body = config.get('body')
    if isinstance(body, dict):
        vars['pending_promise_product_id'] = str(body.get(__PRODUCT_FIELD__) or '')
except Exception:
    pass
""".replace("__RESULT_FLAG__", promise_result_flag).replace("__PRODUCT_FIELD__", repr(promise_fields["product"]))
    for node_name, negative_target in (
        ("create_promise_call", "prepare_promise_retry"),
        ("retry_promise_call", "mark_promise_error"),
    ):
        nodes[node_name] = {
            "name": node_name, "node_type": "api_request", "on_enter": {"type": "silent"},
            "passthrough": True, "passthrough_default_transition": "promise_write_uncertain",
            "pre_actions": [promise_engine],
            "transitions": [
                _to("mark_promise_success", "Bank confirmed promise write",
                    _bank_response(node_name, promise_success_codes, promise_result_flag, True)),
                *([_to(negative_target, "Bank explicitly confirmed no write",
                       _bank_non_recording(node_name, promise_false_codes, promise_no_write_codes, promise_result_flag))]
                  if promise_false_codes or promise_no_write_codes else []),
                _always("promise_write_uncertain"),
            ],
        }
    nodes["prepare_promise_retry"] = _code_node(
        "prepare_promise_retry", """try:
    for suffix in ('response.status', 'response.status_code', 'response.data.ok', 'status_code'):
        vars['retry_promise_call_' + suffix] = ''
except Exception:
    pass
""".replace("response.data.ok", "response.data." + promise_result_flag), "retry_promise_call")
    # On a successful bank response, update the recorded set and select the
    # next product in the same sandbox call. The notice still follows a
    # source-defined positive bank response.
    nodes["mark_promise_success"] = _code_node(
        "mark_promise_success", _ADVANCE_PROMISE_CODE + "\n" + select_promise_code,
        "promise_success_notice")
    success_sentence = _success_sentence(policy)
    nodes["promise_success_notice"] = {
        "name": "promise_success_notice", "node_type": "conversation",
        "on_enter": {"type": "tts" if success_sentence else "silent", "text": success_sentence},
        "task_message": ("Say the fixed confirmation of a successfully recorded promise. No tool call is allowed in this turn."
                         if success_sentence else "Continue silently. Do not invent a confirmation message or call a tool."),
        "passthrough": True, "passthrough_default_transition": "service_unavailable",
        "transitions": [
            _eq("create_promise_call", "write_state", "promise"),
            _eq("prepare_notes", "write_state", "done"),
            _always("service_unavailable"),
        ],
    }
    nodes["mark_promise_error"] = _code_node(
        "mark_promise_error", _MARK_PROMISE_ERROR_CODE.replace("__SERVICE_ERROR_REASON__", repr(reason_names.get("service_error") or "service_error")),
        "record_results")

    prepare_notes_code = (_PREPARE_NOTES_CODE
                          .replace("__NOTE_CODES__", repr(note_codes))
                          .replace("__REFUSAL_REASON__", repr(reason_names.get("refused") or "refused")))
    nodes["prepare_notes"] = _code_node("prepare_notes", prepare_notes_code, "finalize")
    nodes["prepare_notes"]["transitions"] = [
        _eq("select_note", "note_write_state", "notes"), _always("finalize")]
    nodes["select_note"] = _code_node("select_note", _SELECT_NOTE_CODE, "finalize")
    nodes["select_note"]["transitions"] = [
        _eq("add_note_call", "note_write_state", "notes"), _always("finalize")]
    note_engine = _api_tool(openapi, "add_note", fixed={
        note_fields["product"]: "{{pending_note_product_id}}", note_fields["reason"]: "{{pending_note_reason}}"},
        description="Record one deferred non-promise reason after the discussion has ended and no transfer is pending.", post_code=_NOTE_HOOK)
    note_engine["pre_process_code"] = note["pre_process_code"]
    for node_name, failure_target in (("add_note_call", "retry_note_call"),
                                      ("retry_note_call", "note_write_failed")):
        nodes[node_name] = {
            "name": node_name, "node_type": "api_request", "on_enter": {"type": "silent"},
            "passthrough": True, "passthrough_default_transition": failure_target,
            "pre_actions": [note_engine],
            "transitions": [_bool("advance_note", node_name + "_response.data.ok", "Note API confirmed success"),
                            _always(failure_target)],
        }
    nodes["advance_note"] = _code_node("advance_note", _ADVANCE_NOTE_CODE, "select_note")
    nodes["note_write_failed"] = _code_node("note_write_failed", _SKIP_NOTE_CODE, "select_note")
    for suffix in ("restructuring", "support", "human"):
        queue = _queue_for(openapi, suffix, policy)
        t = _api_tool(openapi, "transfer_to_agent", fixed={transfer_fields["queue"]: queue},
                      description=f"Transfer to {queue} now; no notes on any transfer.", post_code=_TRANSFER_HOOK)
        t["pre_process_code"] = (
            "try:\n"
            f"    vars['pending_transfer_queue'] = {queue!r}\n"
            f"    vars['pending_transfer_reason'] = {suffix!r}\n"
            "except Exception:\n"
            "    pass\n"
        )
        nodes["transfer_" + suffix] = {
            "name": "transfer_" + suffix, "node_type": "api_request", "on_enter": {"type": "silent"},
            "passthrough": True, "passthrough_default_transition": "finalize",
            "pre_actions": [t], "transitions": [_always("finalize")],
        }
    transfer_first = True
    precedence_text = str((policy.get("constraints") or {}).get("outcome_precedence") or "") if isinstance(policy.get("constraints"), dict) else ""
    precedence_text += "\n" + brief
    transfer_matches = list(re.finditer(r"(?:promise|pledge)[^\n.]{0,60}wins?\s+over\s+(?:transfer|handoff)|(?:transfer|handoff)[^\n.]{0,60}wins?\s+over\s+(?:promise|pledge)", precedence_text, flags=re.IGNORECASE))
    if transfer_matches:
        transfer_first = not bool(re.match(r"(?:promise|pledge)", transfer_matches[-1].group(), flags=re.IGNORECASE))
    final_code = (_FINAL_CODE
                  .replace("__OUTCOME_MAP__", repr(outcomes))
                  .replace("__REASON_MAP__", repr(reason_names))
                  .replace("__CLOSE_REASON_MAP__", repr(close_reason_names))
                  .replace("__NOTE_CODES__", repr(note_codes))
                  .replace("__CLOSE_CODES__", repr(close_codes))
                  .replace("__TRANSFER_FIRST__", repr(transfer_first))
                  .replace("__ERROR_OUTCOME__", repr(outcomes["no_promise"]))
                  .replace("__ERROR_REASON__", repr(close_reason_names.get("service_error") or "")))
    nodes["finalize"] = _code_node("finalize", final_code, "farewell")
    nodes["finalize"]["transitions"] = [
        _to("farewell", "Close outcome was computed", _when(_check("close_outcome", "is_set"))),
        _always("service_unavailable"),
    ]

    for label, outcome, reason in (
        ("wrong_number", "not_reached", "wrong_number"),
        ("deceased", "not_reached", "deceased"),
        ("unclear", "not_reached", "unclear"),
        ("no_callback", "not_reached", "no_callback"),
        ("callback", "not_reached", "callback"),
        ("identity_failed", "not_verified", "failed"),
        ("identity_declined", "not_verified", "declined"),
        ("service_error", "no_promise", "service_error"),
    ):
        nodes["finish_" + label] = _finish_node("finish_" + label, outcomes[outcome], close_reason_names.get(reason, reason) if reason else "")
    nodes["finish_deceased"]["passthrough_default_transition"] = "farewell_deceased"
    nodes["finish_deceased"]["transitions"] = [_always("farewell_deceased")]
    nodes["finish_identity_failed"]["passthrough_default_transition"] = "farewell_identity_failed"
    nodes["finish_identity_failed"]["transitions"] = [_always("farewell_identity_failed")]
    nodes["finish_identity_declined"]["passthrough_default_transition"] = "farewell_identity_declined"
    nodes["finish_identity_declined"]["transitions"] = [_always("farewell_identity_declined")]
    for name, sentence in (
        ("farewell_deceased", "I am sorry for your loss."),
        ("farewell_identity_failed", f"For your security, please use the {bank_name} mobile app or visit a branch. Goodbye."),
        ("farewell_identity_declined", "I cannot continue without the security check. Thank you. Goodbye."),
    ):
        nodes[name] = {
            "name": name, "node_type": "conversation",
            "on_enter": {"type": "tts", "text": sentence},
            "task_message": "Say the fixed farewell. No tool call is allowed in this turn.",
            "passthrough": True, "passthrough_default_transition": "close_call",
            "transitions": [_always("close_call")],
        }
    nodes["farewell"] = {
        "name": "farewell", "node_type": "conversation",
        "on_enter": {"type": "tts", "text": "Thank you for your time. Goodbye."},
        "task_message": "Thank the caller and say goodbye. No tool call is allowed in this turn.",
        "passthrough": True, "passthrough_default_transition": "close_call",
        "transitions": [_always("close_call")],
    }
    nodes["close_call"] = {
        "name": "close_call", "node_type": "api_request", "on_enter": {"type": "silent"},
        "passthrough": True, "passthrough_default_transition": "ended",
        "pre_actions": [close], "transitions": [_always("ended")],
    }
    nodes["ended"] = {
        "name": "ended", "node_type": "end_call", "on_enter": {"type": "silent"},
        "disable_inactivity_monitor": True, "transitions": [],
        "terminal": {"type": "function", "function": {
            "name": "freya_call_complete", "handler": "__function__:end_call",
            "parameters": {"type": "object", "properties": {
                "message_mode": {"type": "string", "default": "none"},
            }},
        }},
    }
    nodes["service_unavailable"] = {
        "name": "service_unavailable", "node_type": "conversation",
        "on_enter": {"type": "tts", "text": "I cannot confirm or complete this request right now. Please check the mobile app or contact the bank. Thank you and goodbye."},
        "task_message": "A bank service is unavailable. Say only the fixed message. Do not claim a promise, callback, transfer, or note was recorded. No tool call is allowed in this turn.",
        "passthrough": True, "passthrough_default_transition": "close_call",
        "transitions": [_always("close_call")],
    }
    nodes["promise_write_uncertain"] = {
        "name": "promise_write_uncertain", "node_type": "conversation",
        "on_enter": {"type": "tts", "text": "I cannot confirm whether your payment promise was recorded. Please check the mobile app or contact the bank. Thank you and goodbye."},
        "task_message": "The bank write result is unknown. Say only the fixed message. Do not retry or claim the promise was recorded. No tool call is allowed in this turn.",
        "passthrough": True, "passthrough_default_transition": "close_call",
        "transitions": [_always("close_call")],
    }
    # A failed code_exec must never continue on the node's normal fallback.
    # Use both the failure counter and the passthrough default because Freya
    # may skip condition evaluation when its sandbox cannot be reached.
    code_names = []
    for code_name, code_node in nodes.items():
        if code_node.get("node_type") == "code":
            code_names.append(code_name)
            # If the engine skips transition evaluation after a tool failure,
            # the passthrough default must still lead to the truthful fallback.
            code_node["passthrough_default_transition"] = "service_unavailable"
            code_node["transitions"].insert(0, _to(
                "service_unavailable", "Code execution failed; no result may be assumed",
                _when(_check(code_name + "_failure_count", "greater_than", 0)),
            ))
    close_code = [
        "try:\n", "    import json\n", "    body = config.get('body')\n",
        "    if isinstance(body, dict):\n",
        f"        failed_nodes = {code_names!r}\n",
        "        failed = any(int(vars.get(name + '_failure_count') or 0) > 0 for name in failed_nodes)\n",
        "        if failed or not vars.get('close_outcome'):\n",
        "            recorded = vars.get('recorded_ids') or '[]'\n",
        "            try:\n",
        "                recorded = json.loads(recorded) if isinstance(recorded, str) else recorded\n",
        "            except Exception:\n", "                recorded = []\n",
        "            if recorded:\n",
        f"                body[{close_fields['outcome']!r}] = {outcomes['promise']!r}\n",
        f"                body.pop({close_fields['reason']!r}, None)\n",
        "            else:\n",
        f"                body[{close_fields['outcome']!r}] = {outcomes['no_promise']!r}\n",
    ]
    if close_fields["reason"]:
        close_code.append(
            f"                body[{close_fields['reason']!r}] = {(close_reason_names.get('service_error') or 'service_error')!r}\n")
    close_code.extend([
        f"        elif {close_fields['reason']!r} and not body.get({close_fields['reason']!r}):\n",
        f"            body.pop({close_fields['reason']!r}, None)\n",
        "except Exception:\n", "    pass\n",
    ])
    close["pre_process_code"] = "".join(close_code)
    system_prompt = (
        f"You are {bank_name}'s digital assistant. Follow the source policy below in full. "
        "Follow the current node's task exactly. Never disclose account information before the required identity checks pass. "
        "Treat failed or missing bank responses as failures; never claim an action succeeded until the bank explicitly confirms it. "
        "Never invent facts or offer an unapproved concession. Keep each reply concise. "
        "\n\nSOURCE POLICY:\n" + source_policy
    )
    return {"initial_node": "opening", "system_prompt": system_prompt, "nodes": nodes}

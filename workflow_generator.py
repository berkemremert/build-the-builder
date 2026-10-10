"""Build a Freya collections workflow from a parsed policy and public OpenAPI.

The returned object is the workflow value, not the REST ``name/workflow`` wrapper.
All HTTP calls are derived from the supplied OpenAPI document.
"""

from __future__ import annotations

import re
from typing import Any


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


def _enum(name: str, choices: list[str], description: str) -> dict:
    return {"name": name, "type": "string", "enum_values": choices, "description": description, "min_confidence": 0.65}


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


def _url(openapi: dict, path: str) -> str:
    servers = openapi.get("servers") or []
    if not servers or not servers[0].get("url"):
        raise ValueError("OpenAPI must contain a server URL")
    base = servers[0]["url"].rstrip("/")
    # A server URL may already contain the version path.
    if base.endswith(path):
        return base
    return base + "/" + path.lstrip("/")


def _api_tool(openapi: dict, name: str, *, fixed: dict | None = None,
              description: str = "", post_code: str = "") -> dict:
    path, method, schema = _api_spec(openapi, name)
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
        "url": {"type": "string", "default": _url(openapi, path)},
        "method": {"type": "string", "default": method},
        "headers": {"type": "object", "properties": {
            "Content-Type": {"type": "string", "default": "application/json"}}},
        "body": {"type": "object", "properties": properties, "required": schema.get("required") or []},
    }}
    tool = {"type": "function", "config": {"timeoutMs": 12000, "maxRetries": 0},
            "function": {"name": name, "handler": "__function__:api_call",
                         "description": description or f"Call {name} when its policy condition is met.",
                         "parameters": params}}
    if post_code:
        tool["post_process_code"] = post_code
    return tool


def _holiday_dates(policy: dict) -> list[str]:
    """Read only dates explicitly identified as bank holidays in the input."""
    dates: set[str] = set()
    constraints = policy.get("constraints") or {}
    if isinstance(constraints, dict):
        for key, value in constraints.items():
            if "holiday" in str(key).lower():
                dates.update(re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", str(value)))
    for key in ("raw_brief", "global_rules", "verified_call", "opening"):
        source = str(policy.get(key) or "")
        for line in source.splitlines():
            if "holiday" in line.lower():
                dates.update(re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", line))
    return sorted(dates)


def _queue_for(openapi: dict, role: str) -> str:
    schema = _api_spec(openapi, "transfer_to_agent")[2]
    queue_spec = (schema.get("properties") or {}).get("queue") or {}
    values = [str(value) for value in queue_spec.get("enum") or []]
    if not values:
        return role
    hints = {
        "restructuring": ("restructur", "repayment", "payment_plan", "workout", "arrangement"),
        "support": ("support", "care", "hardship", "assist"),
        "human": ("general", "human", "agent", "service"),
    }
    for hint in hints[role]:
        for value in values:
            if hint in value.lower():
                return value
    return values[0]


def _identity_fields(openapi: dict) -> list[str]:
    schema = _api_spec(openapi, "verify_identity")[2]
    field = (schema.get("properties") or {}).get("field") or {}
    values = [str(value) for value in field.get("enum") or []]
    if len(values) < 2:
        raise ValueError("verify_identity.field must offer at least two fields")
    return values[:2]


def _outcome_names(openapi: dict, policy: dict | None = None) -> dict[str, str]:
    schema = _api_spec(openapi, "close_call")[2]
    values = [str(value) for value in ((schema.get("properties") or {}).get("outcome") or {}).get("enum") or []]
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
    for role, hints in aliases.items():
        replacements = re.findall(r"replace\s+(?:outcome\s+)?" + re.escape(role) +
                                  r"\s+with\s+([a-z_]+)", source, flags=re.IGNORECASE)
        if replacements and replacements[-1] in values:
            result[role] = replacements[-1]
        elif role in values:
            result[role] = role
        else:
            candidates = [value for value in values if value in policy_keys]
            result[role] = next((value for hint in hints for value in candidates + values
                                 if hint in value.lower() and not (role == "promise" and value.lower().startswith("no_"))), values[0])
    return result


def _reason_names(openapi: dict, policy: dict) -> tuple[dict[str, str], dict[str, str], list[str], list[str]]:
    close_schema = _api_spec(openapi, "close_call")[2]
    close_enum = [str(value) for value in ((close_schema.get("properties") or {}).get("reason") or {}).get("enum") or []]
    note_schema = _api_spec(openapi, "add_note")[2]
    note_enum = [str(value) for value in ((note_schema.get("properties") or {}).get("reason") or {}).get("enum") or []]
    source = str(policy.get("raw_brief") or "") + "\n" + str(policy.get("tool_policy") or "")
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
                                  r"\s+with\s+([a-z_]+)", source, flags=re.IGNORECASE)
        for allowed, target in ((note_enum, result), (close_enum, close_result)):
            if not allowed:
                target[role] = role
            elif replacements and replacements[-1] in allowed:
                target[role] = replacements[-1]
            elif role in allowed:
                target[role] = role
            else:
                target[role] = next((value for hint in hints for value in allowed if hint in value.lower()), "")
    return result, close_result, note_enum, close_enum


def _bank_name(policy: dict) -> str:
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
    for line in (raw + "\n" + opening_source).splitlines():
        if not re.search(r"\b(?:first sentence|opening(?: sentence| line)?|start(?: the)? call|begin(?: the)? call)\b", line, re.I):
            continue
        if not re.search(r"\b(?:exact(?:ly)?|verbatim|word.for.word|must say|required wording)\b", line, re.I):
            continue
        quoted_parts = re.findall(r'["“]([^"”]+)["”]', line)
        quoted_parts += re.findall(r"(?<!\w)'((?:[^']|'(?=\w))*)'", line)
        for quoted in quoted_parts:
            if bank_name.lower() in quoted.lower() or "digital assistant" in quoted.lower():
                exact = re.sub(r"(?<!\{)\{(title|last_name)\}(?!\})", r"{{\1}}", quoted.strip())
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
    source = "\n".join(str(policy.get(key) or "") for key in ("raw_brief", "verified_call", "global_rules"))
    bands = []
    pattern = re.compile(
        r"\|\s*(\d+)\s+to\s+(\d+)\s*\|\s*([^|]+)\|\s*the\s+(\d+)(?:st|nd|rd|th)\s+business day[^|]*\|\s*(\d+)\s*\|",
        re.IGNORECASE,
    )
    for low, high, label, days, attempts in pattern.findall(source):
        bands.append({"low": int(low), "high": int(high), "label": label.strip(),
                      "business_days": int(days), "attempts": int(attempts)})
    if not bands:
        words = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
                 "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
                 "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
                 "fifteenth": 15}
        cardinal = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
        sentence = re.compile(
            r"(\d+)\s*(?:to|[-–])\s*(\d+)\s+days?\s+late[^.]*?deadline\s+is\s+(?:the\s+)?"
            r"([a-z]+|\d+(?:st|nd|rd|th)?)\s+business\s+day[^.]*?with\s+"
            r"([a-z]+|\d+)\s+(?:persuasion\s+)?attempt",
            re.IGNORECASE,
        )
        for low, high, ordinal, attempt in sentence.findall(source):
            days = words.get(ordinal.lower()) or int(re.match(r"\d+", ordinal).group()) if re.match(r"\d+", ordinal) else words.get(ordinal.lower())
            count = cardinal.get(attempt.lower()) or int(attempt) if attempt.isdigit() else cardinal.get(attempt.lower())
            if days and count:
                bands.append({"low": int(low), "high": int(high), "label": "",
                              "business_days": days, "attempts": count})
    if not bands:
        words = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
                 "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
                 "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
                 "fifteenth": 15}
        prose = re.compile(
            r"(\d+)\s*(?:to|[-–])\s*(\d+)(?:\s+days?\s+late)?\s*(?:=|:|is|gets?|means?)\s*(?:the\s+)?(\d+)(?:st|nd|rd|th)?|"
            r"(\d+)\s*(?:to|[-–])\s*(\d+)(?:\s+days?\s+late)?\s*(?:=|:|is|gets?|means?)\s*(?:the\s+)?(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth|thirteenth|fourteenth|fifteenth)\s+business day",
            re.IGNORECASE,
        )
        for match in prose.finditer(source):
            low, high, ordinal = (match.group(1), match.group(2), match.group(3)) if match.group(1) else (match.group(4), match.group(5), match.group(6))
            if match.group(1):
                following = source[match.end(): match.end() + 35].lower()
                if "business day" not in following:
                    continue
                days = int(ordinal)
            else:
                days = words[ordinal.lower()]
            bands.append({"low": int(low), "high": int(high), "label": "",
                          "business_days": days, "attempts": 0})
    return bands


def _success_sentence(policy: dict) -> str:
    source = str(policy.get("raw_brief") or "")
    matches = re.findall(r"(?:after a recorded promise|promise is recorded)[^\n]{0,100}(?:say this sentence|say exactly)\s*:\s*[\"“]([^\"”]+)[\"”]", source, flags=re.IGNORECASE)
    return matches[-1] if matches else "You will receive a text message confirming this promise."


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
        if data.get('all_verified') is True:
            data['what_to_do_next'] = 'Identity is fully verified. Continue to account lookup.'
        elif data.get('verified') is True:
            data['what_to_do_next'] = 'This answer passed. Ask only the next security question.'
        elif data.get('hint'):
            data['what_to_do_next'] = 'The service returned a hint. Apply the exact hint-handling rule in the policy before retrying.'
        elif data.get('wrong_answers_left') == 0:
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


def _prepare_code(holidays: list[str], item_fields: dict, bands: list[dict], offer_last_business_day: bool) -> str:
    amount_field = "past_due" if "past_due" in item_fields else next(
        (k for k in item_fields if "due" in k or "balance" in k), "past_due")
    days_field = "days_late" if "days_late" in item_fields else next(
        (k for k in item_fields if "late" in k or "overdue_days" in k), "days_late")
    floor_fields = [k for k in item_fields if any(x in k for x in ("minimum", "instalment", "installment", "floor"))]
    if not floor_fields:
        floor_fields = ["minimum_payment", "instalment_amount"]
    return f"""import json
import datetime
try:
    raw = vars.get('account_products') or vars.get('load_account_response.data.products') or vars.get('get_account_response.data.products') or []
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, list):
        raw = []
    if not raw:
        groups = dict()
        for prefix in ('load_account_response.data.products.', 'get_account_response.data.products.'):
            for key, value in vars.items():
                if str(key).startswith(prefix):
                    tail = str(key)[len(prefix):].split('.', 1)
                    if len(tail) == 2 and tail[0].isdigit():
                        groups.setdefault(int(tail[0]), dict())[tail[1]] = value
        raw = [groups[i] for i in sorted(groups)]
    now = datetime.datetime.fromisoformat(str(vars.get('now') or '').replace('Z', '+00:00'))
    today = now.date()
    holidays = set({holidays!r})
    def business(day):
        return day.weekday() < 5 and day.isoformat() not in holidays
    def nth(day, count):
        for ignored in range(count):
            day += datetime.timedelta(days=1)
            while not business(day):
                day += datetime.timedelta(days=1)
        return day
    friday = today + datetime.timedelta(days=4 - today.weekday())
    while not business(friday):
        friday -= datetime.timedelta(days=1)
    if friday <= today:
        friday = today + datetime.timedelta(days=11 - today.weekday())
        while not business(friday):
            friday -= datetime.timedelta(days=1)
    result = []
    lines = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        kind = str(item.get('type') or 'product')
        label = kind.replace('_', ' ')
        if 'card' in kind and item.get('card_last4'):
            label += ' ending ' + str(item.get('card_last4'))
        amount = float(item.get({amount_field!r}) or 0)
        days = int(float(item.get({days_field!r}) or 0))
        floors = [float(item.get(field) or 0) for field in {floor_fields!r}]
        floor = next((value for value in floors if value > 0), amount)
        band = next((rule for rule in {bands!r} if rule['low'] <= days <= rule['high']), None)
        deadline = nth(today, band['business_days']) if band else None
        entry = dict(product_id=str(item.get('product_id') or ''), label=label,
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
    def business(day):
        return day.weekday() < 5 and day.isoformat() not in holidays
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
            amount = float(str(amount_raw).replace(',', ''))
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
        if date_raw in ('today', 'now'):
            day = today
        elif date_raw == 'tomorrow':
            day = today + datetime.timedelta(days=1)
        else:
            match = re.search(r'\\d{4}-\\d{2}-\\d{2}', date_raw)
            if match:
                day = datetime.date.fromisoformat(match.group())
            else:
                months = ('january','february','march','april','may','june','july','august','september','october','november','december')
                month = next((index + 1 for index, word in enumerate(months) if word in date_raw), None)
                number = re.search(r'(?<!\\d)([0-3]?\\d)(?:st|nd|rd|th)?(?!\\d)', date_raw)
                named_days = dict((name, index + 1) for index, name in enumerate((
                    'first', 'second', 'third', 'fourth', 'fifth', 'sixth', 'seventh', 'eighth', 'ninth', 'tenth',
                    'eleventh', 'twelfth', 'thirteenth', 'fourteenth', 'fifteenth', 'sixteenth', 'seventeenth',
                    'eighteenth', 'nineteenth', 'twentieth', 'twenty first', 'twenty second', 'twenty third',
                    'twenty fourth', 'twenty fifth', 'twenty sixth', 'twenty seventh', 'twenty eighth',
                    'twenty ninth', 'thirtieth', 'thirty first')))
                spoken_day = None
                if month:
                    normalized = re.sub(r'[-\\s]+', ' ', date_raw)
                    day_pattern = '|'.join(re.escape(name) for name in sorted(named_days, key=len, reverse=True))
                    month_pattern = '|'.join(months)
                    before = re.search(r'\\b(?:the\\s+)?(' + day_pattern + r')(?:\\s+of)?\\s+(?:' + month_pattern + r')\\b', normalized)
                    after = re.search(r'\\b(?:' + month_pattern + r')\\s+(?:the\\s+)?(' + day_pattern + r')\\b', normalized)
                    spoken = before or after
                    if spoken:
                        spoken_day = named_days[spoken.group(1)]
                weekdays = ('monday','tuesday','wednesday','thursday','friday','saturday','sunday')
                weekday = next((index for index, word in enumerate(weekdays) if word in date_raw), None)
                if (number or spoken_day) and (month or weekday is not None):
                    day_number = spoken_day or int(number.group(1))
                    chosen_month = month or today.month
                    chosen_year = today.year
                    day = datetime.date(chosen_year, chosen_month, day_number)
                    if day < today and not month:
                        chosen_month = 1 if today.month == 12 else today.month + 1
                        chosen_year += 1 if today.month == 12 else 0
                        day = datetime.date(chosen_year, chosen_month, day_number)
                    if weekday is not None and day.weekday() != weekday:
                        raise ValueError('Day and weekday disagree')
                elif weekday is not None:
                    if date_raw.startswith('next '):
                        day = today + datetime.timedelta(days=(7 - today.weekday()) + weekday)
                    elif date_raw.startswith('this '):
                        day = today + datetime.timedelta(days=weekday - today.weekday())
                    else:
                        day = today + datetime.timedelta(days=(weekday - today.weekday()) % 7)
                else:
                    raise ValueError('Payment date missing')
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
        if day < today or day.weekday() >= 5 or day.isoformat() in holidays or (deadline and day > datetime.date.fromisoformat(deadline)):
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
try:
    products = json.loads(str(vars.get('product_json') or '[]'))
    product_by_id = dict((str(item.get('product_id') or ''), item) for item in products)
    terms = json.loads(str(vars.get('multi_terms_json') or '{}'))
    notes = json.loads(str(vars.get('multi_notes_json') or '{}'))
    floor_first = set(json.loads(str(vars.get('multi_floor_first_json') or '[]')))
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
            pid = str(item.get('product_id') or fallback_pid)
            if pid not in product_by_id:
                continue
            date = str(item.get('date_phrase') or item.get('date_words') or item.get('raw_date') or item.get('date') or '').strip()
            if not date:
                continue
            amount = item.get('amount')
            if amount is None:
                amount = product_by_id[pid].get('past_due')
            terms[pid] = dict(product_id=pid, amount=amount, date=date)
            notes.pop(pid, None)
            added = True
        return added
    phase = str(vars.get('multi_phase') or 'initial')
    next_step = ''
    if phase == 'initial':
        if str(vars.get('all_today_status') or '') == 'yes':
            today = str(vars.get('now') or '')[:10]
            for pid, item in product_by_id.items():
                terms[pid] = dict(product_id=pid, amount=item.get('past_due'), date=today)
        else:
            common = str(vars.get('multi_common_date_words') or '').strip()
            if common:
                for pid, item in product_by_id.items():
                    terms[pid] = dict(product_id=pid, amount=item.get('past_due'), date=common)
            add_terms(vars.get('proposed_terms_json') or '')
            raw_notes = vars.get('pending_note_reasons_json') or '{}'
            raw_notes = json.loads(raw_notes) if isinstance(raw_notes, str) else raw_notes
            if isinstance(raw_notes, dict):
                for pid, reason in raw_notes.items():
                    pid = str(pid)
                    if pid in product_by_id and pid not in terms and str(reason) in __NOTE_CODES__ and str(reason) != __REFUSAL_REASON__:
                        notes[pid] = str(reason)
            for pid in parsed(vars.get('multi_full_too_much_ids_json') or '[]'):
                if str(pid) in product_by_id:
                    floor_first.add(str(pid))
        vars['multi_offer_stage'] = '1'
    else:
        pid = str(vars.get('multi_focus_product_id') or '')
        action = str(vars.get('multi_offer_action') or 'unclear')
        if phase == 'support':
            if action == 'proposal':
                add_terms(vars.get('multi_reply_terms_json') or '', pid)
            if pid not in terms:
                notes[pid] = __HARDSHIP_REASON__
        elif action == 'accepted' and phase == 'offer':
            if str(vars.get('multi_fixed_offer') or '') == 'yes':
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
            if pid not in terms:
                next_step = 'clarify'
        elif action == 'paid':
            notes[pid] = __PAID_REASON__
        elif action == 'dispute':
            notes[pid] = __DISPUTE_REASON__
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
        vars['multi_reply_common_date_words'] = ''
    unresolved = [(index, str(item.get('product_id') or '')) for index, item in enumerate(products)
                  if str(item.get('product_id') or '') not in terms and str(item.get('product_id') or '') not in notes]
    if next_step in ('support', 'clarify'):
        vars['multi_phase'] = next_step
        vars['multi_next'] = next_step
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
try:
    attempts = int(vars.get('multi_correction_attempts') or 0) + 1
except Exception:
    attempts = 1
vars['multi_correction_attempts'] = str(attempts)
try:
    terms = json.loads(str(vars.get('multi_terms_json') or '{}'))
    products = json.loads(str(vars.get('product_json') or '[]'))
    known = set(str(item.get('product_id') or '') for item in products)
    raw = vars.get('multi_corrected_terms_json') or ''
    raw = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
    raw = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
    changed = False
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid = str(item.get('product_id') or '')
        date = str(item.get('date_phrase') or item.get('date_words') or item.get('date') or '').strip()
        if pid in known and date:
            previous = terms.get(pid) or dict()
            terms[pid] = dict(product_id=pid, amount=item.get('amount', previous.get('amount')), date=date)
            changed = True
    vars['multi_terms_json'] = json.dumps(terms)
    vars['proposed_terms_json'] = json.dumps([terms[str(item.get('product_id') or '')] for item in products
                                              if str(item.get('product_id') or '') in terms])
    vars['multi_corrected_terms_json'] = ''
    vars['multi_next'] = 'confirm' if changed else 'service_error' if attempts >= 3 else 'clarify'
except Exception:
    vars['multi_next'] = 'service_error' if attempts >= 3 else 'clarify'
"""


def build_workflow(policy: dict, openapi: dict) -> dict:
    """Return a runnable workflow using the supplied policy and OpenAPI schema.

    Extra policy keys are ignored. Missing prose sections fall back to ``raw_brief``.
    """
    policy = policy or {}
    brief = str(policy.get("raw_brief") or "")
    prose = "\n".join(str(policy.get(key) or "") for key in
                      ("global_rules", "opening", "identity", "verified_call"))
    tool_policy = policy.get("tool_policy") or {}
    if not isinstance(tool_policy, dict):
        tool_policy = {}
    account_path, account_method, _ = _api_spec(openapi, "get_account")
    response_schema = (openapi.get("paths") or {}).get(account_path, {}).get(account_method.lower(), {}).get("responses", {}).get("200", {}).get("content", {}).get("application/json", {}).get("schema", {})
    item_fields = response_schema.get("properties", {}).get("products", {}).get("items", {}).get("properties", {})
    holidays = _holiday_dates(policy)
    bands = _deadline_bands(policy)
    identity_fields = _identity_fields(openapi)
    outcomes = _outcome_names(openapi, policy)
    reason_names, close_reason_names, note_codes, close_codes = _reason_names(openapi, policy)
    promise_success_codes, promise_result_flag, promise_false_codes, promise_no_write_codes = _promise_response_contract(openapi)
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
    multi_constraints = policy.get("constraints") or {}
    product_limits = str(multi_constraints.get("product_limits") or "") if isinstance(multi_constraints, dict) else ""
    multi_enabled = bool(re.search(
        r"\b(?:all|multiple|several|up to (?:three|3)|one to (?:three|3)) products\b|"
        r"\bproducts? one by one\b|\beach product.{0,35}\border\b",
        str(policy.get("verified_call") or "") + "\n" + product_limits, re.I,
    ))
    offer_source = str(policy.get("verified_call") or "") or source_policy
    fixed_multi_offers = ("last business day of this week" in offer_source.lower() and
                          bool(re.search(r"attempt\s*2.{0,100}floor.{0,100}deadline", offer_source, re.I)))

    verify_dob = _api_tool(openapi, "verify_identity", fixed={"field": identity_fields[0]},
                           description=f"Check the customer's {identity_fields[0].replace('_', ' ')} after their own answer. Never use a third party's answer.", post_code=_VERIFY_HOOK)
    verify_phone = _api_tool(openapi, "verify_identity", fixed={"field": identity_fields[1]},
                             description=f"Check the customer's {identity_fields[1].replace('_', ' ')} after their own answer. Never use a third party's answer.", post_code=_VERIFY_HOOK)
    callback = _api_tool(openapi, "schedule_callback", description="Book a valid callback after a third party gives an exact day and time.", post_code=_CALLBACK_HOOK)
    account = _api_tool(openapi, "get_account", description="Load late products only after both security answers have passed.")
    promise = _api_tool(openapi, "create_promise", description="Record one confirmed, valid promise for one product. Retry once only after an explicit bank response says nothing was recorded.", post_code=promise_hook)
    note = _api_tool(openapi, "add_note", description="For a multi-product account only, record each product without a promise as refused, hardship, dispute, paid or service_error. Never add a note on a transfer.", post_code=_NOTE_HOOK)
    transfer = _api_tool(openapi, "transfer_to_agent", description="Transfer only for accepted support, persistent human request, or restructuring. Never add notes on a transfer.", post_code=_TRANSFER_HOOK)
    promise["pre_process_code"] = """try:
    body = config.get('body')
    if isinstance(body, dict):
        vars['pending_promise_product_id'] = str(body.get('product_id') or '')
except Exception:
    pass
"""
    note["pre_process_code"] = """try:
    body = config.get('body')
    if isinstance(body, dict):
        vars['pending_note_product_id'] = str(body.get('product_id') or '')
        vars['pending_note_reason'] = str(body.get('reason') or '')
except Exception:
    pass
"""
    transfer["pre_process_code"] = """try:
    body = config.get('body')
    if isinstance(body, dict):
        vars['pending_transfer_queue'] = str(body.get('queue') or '')
except Exception:
    pass
"""
    close = _api_tool(openapi, "close_call", fixed={"outcome": "{{close_outcome}}", "reason": "{{close_reason}}"},
                      description="Close the call exactly once, after farewell. This is the last bank API call.")
    close["pre_process_code"] = """try:
    body = config.get('body')
    if isinstance(body, dict) and not body.get('reason'):
        body.pop('reason', None)
except Exception:
    pass
"""

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
                     _eq("third_party", "opening_role", "third_party")],
        max_turns=2, max_target="finish_unclear")

    nodes["third_party"] = _conversation(
        "third_party", identity_base +
        "A third party answered. Ask when the named customer can be reached. Apply the exact callback dates, hours, and retry limit in the policy. "
        "Use {{now}} and these bank holidays from the policy: " + (", ".join(holidays) if holidays else "none specified") + ". "
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
                                  "If they decline or ask why before answering, explain once that security protects their account, then close if they decline again. "
                                  "Do not reveal the account. After tool result, speak or call the next tool.")
    phone_prompt = identity_base + (f"The first security answer passed. Ask only for the customer's {identity_fields[1].replace('_', ' ')}. "
                                    f"Call verify_identity with each answer. Do not ask {identity_fields[0].replace('_', ' ')} again. "
                                    "Use the service's remaining-attempt count and the policy's failure rule. "
                                    "A refusal or demand to know the call purpose gets one explanation that this protects their account, then a second refusal ends the call. "
                                    "After tool result, speak or call the next tool.")
    identity_transitions = lambda success: [
        _bool("load_account" if success else "identity_phone", "verify_identity_response.data.all_verified" if success else "verify_identity_response.data.verified", "Security answer passed"),
        _to("finish_identity_failed", "Three wrong answers", _when(_check("verify_identity_response.data.wrong_answers_left", "equals", "0"))),
        _eq("identity_decline", "identity_intent", "declined"),
    ]
    nodes["identity_dob"] = _conversation("identity_dob", dob_prompt, tools=[verify_dob],
                                           extraction=[_enum("identity_intent", ["declined", "answered", "other"],
                                                             f"Latest caller answer: declined if refusing security under the source policy; answered for {identity_fields[0].replace('_', ' ')}; other otherwise.")],
                                           transitions=identity_transitions(False), max_turns=8, max_target="finish_identity_failed")
    nodes["identity_phone"] = _conversation("identity_phone", phone_prompt, tools=[verify_phone],
                                             extraction=[_enum("identity_intent", ["declined", "answered", "other"],
                                                               f"Latest caller answer: declined if refusing security under the source policy; answered for {identity_fields[1].replace('_', ' ')}; other otherwise.")],
                                             transitions=identity_transitions(True), max_turns=8, max_target="finish_identity_failed")
    nodes["identity_decline"] = _conversation(
        "identity_decline", identity_base +
        "Explain once that the two security questions protect the customer's account, then ask for the currently pending security answer. "
        "If they decline or ask what the call is about again, say you cannot continue and close. "
        "If they answer, return to the pending question. No tool call is allowed in this turn.",
        extraction=[_enum("decline_followup", ["declined", "answer", "other"],
                          f"Latest answer: declined if refusal under the source policy; answer for {identity_fields[0].replace('_', ' ')}; other otherwise.")],
        transitions=[_eq("finish_identity_declined", "decline_followup", "declined"),
                     _eq("identity_dob", "decline_followup", "answer")],
        max_turns=2, max_target="finish_identity_declined")
    # A verified DOB is never reasked just because the caller declined the phone question.
    nodes["identity_phone"]["transitions"][2]["target_node"] = "phone_decline"
    nodes["phone_decline"] = _conversation(
        "phone_decline", identity_base +
        f"Explain the security reason under the policy and ask only for {identity_fields[1].replace('_', ' ')}. "
        "A second refusal or call-purpose question ends the call. No tool call is allowed in this turn.",
        extraction=[_enum("phone_decline_followup", ["declined", "answer", "other"],
                          f"Latest answer: declined for a further refusal under the policy; answer for {identity_fields[1].replace('_', ' ')}; other otherwise.")],
        transitions=[_eq("finish_identity_declined", "phone_decline_followup", "declined"),
                     _eq("identity_phone", "phone_decline_followup", "answer")],
        max_turns=2, max_target="finish_identity_declined")

    nodes["load_account"] = {
        "name": "load_account", "node_type": "api_request", "on_enter": {"type": "silent"},
        "passthrough": True, "passthrough_default_transition": "prepare_account",
        "pre_actions": [account], "response_mapping": [{"path": "data.products", "variable": "account_products"}],
        "transitions": [_always("prepare_account")],
    }
    nodes["prepare_account"] = _code_node("prepare_account", _prepare_code(holidays, item_fields, bands, offer_last_business_day), "account_discussion")
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
        "For more than one product, state all products before an ask and follow the source policy's product order and combined confirmation rules. "
        "When the caller has proposed specific terms, stop after acknowledging them. The next step will read them back for confirmation. Do not say they are recorded yet. "
        "If a prior read-back was corrected or validation failed, clarify the changed product, amount or date before offering another read-back. On a date validation error, ask for the exact day; a bare yes to the old read-back is not a new proposal. "
        "For a flat refusal, paid claim, dispute, or hardship with no promise, finish the discussion under the source policy. "
        "Do not say goodbye while a payment proposal remains unconfirmed. No tool call is allowed in this node."
    )
    transitions = [
        _eq("transfer_restructuring", "discussion_action", "transfer_restructuring"),
        _eq("transfer_support", "discussion_action", "transfer_support"),
        _eq("transfer_human", "discussion_action", "transfer_human"),
    ]
    if multi_enabled:
        transitions.append(_to("multi_advance", "Begin ordered product discussion after the all-today answer",
                               _when(_check("product_count", "greater_than", 1))))
        single = lambda action: _when({"logic": "and", "conditions": [
            _check("product_count", "equals", "1"), _check("discussion_action", "equals", action),
        ]})
        transitions.extend([
            _to("prepare_confirmation", "Single product proposal", single("proposal_ready")),
            _to("prepare_notes", "Single product final answer", single("done_no_promise")),
        ])
    else:
        transitions.extend([
            _eq("prepare_confirmation", "discussion_action", "proposal_ready"),
            _eq("prepare_notes", "discussion_action", "done_no_promise"),
        ])
    nodes["account_discussion"] = _conversation("account_discussion", account_prompt, transitions=transitions,
                                                 extraction=[
                                                     _enum("discussion_action", ["proposal_ready", "done_no_promise", "transfer_restructuring", "transfer_support", "transfer_human", "continue"],
                                                           "Classify ONLY the latest caller answer using the source policy. proposal_ready only when this answer proposes or accepts specific payment terms with a fresh date phrase, including today or a weekday. A bare yes/I confirm to an earlier read-back in this node is continue, never proposal_ready. done_no_promise only when all products have final paid/dispute/hardship/refused reasons and no transfer is wanted. A no to today without another date is continue while persuasion remains. Transfer actions take priority."),
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
                                                        {"name": "multi_common_date_words", "type": "string", "min_confidence": 0.55,
                                                         "description": "For a multi-product account only, copy a date phrase explicitly offered for ALL products, such as 'next Friday'. Leave empty for a date applying to just one product."},
                                                        {"name": "multi_full_too_much_ids_json", "type": "string", "min_confidence": 0.55,
                                                         "description": "For a multi-product account only, return a JSON array of product IDs for which the caller explicitly said the full amount is too much. Otherwise return []."}]
                                                       if multi_enabled else []),
                                                 ],
                                                 max_turns=2 if multi_enabled else 40,
                                                 max_target="multi_advance" if multi_enabled else "prepare_notes")
    nodes["account_discussion"]["on_enter_first_entry_only"] = True

    if multi_enabled:
        multi_advance_code = (_MULTI_ADVANCE_CODE
                              .replace("__NOTE_CODES__", repr(note_codes))
                              .replace("__REFUSAL_REASON__", repr(reason_names.get("refused") or "refused"))
                              .replace("__PAID_REASON__", repr(reason_names.get("paid") or "paid"))
                              .replace("__DISPUTE_REASON__", repr(reason_names.get("dispute") or "dispute"))
                              .replace("__HARDSHIP_REASON__", repr(reason_names.get("hardship") or "hardship")))
        nodes["multi_advance"] = _code_node("multi_advance", multi_advance_code, "service_unavailable")
        nodes["multi_advance"]["transitions"] = [
            _eq("prepare_confirmation", "multi_next", "confirm"),
            _eq("prepare_notes", "multi_next", "notes"),
            _eq("multi_select_offer", "multi_next", "offer"),
            _eq("multi_support", "multi_next", "support"),
            _eq("multi_clarify", "multi_next", "clarify"),
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
            {"name": "multi_reply_terms_json", "type": "string", "min_confidence": 0.55,
             "description": "On a fresh proposal, return a JSON array string of product_id, amount, and the caller's RAW date words. Include every product the caller explicitly covered; leave empty otherwise. For an accepted model-generated offer, repeat the exact offered terms."},
            {"name": "multi_reply_common_date_words", "type": "string", "min_confidence": 0.55,
             "description": "Copy a date phrase only when the latest answer explicitly applies that ONE date to all remaining products. Otherwise leave empty."},
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
        nodes["multi_revise"] = _code_node("multi_revise", _MULTI_REVISE_CODE, "multi_correction")
        nodes["multi_revise"]["transitions"] = [
            _eq("prepare_confirmation", "multi_next", "confirm"),
            _eq("service_unavailable", "multi_next", "service_error"),
            _always("multi_correction"),
        ]

    nodes["prepare_confirmation"] = _code_node(
        "prepare_confirmation", _PREPARE_CONFIRMATION_CODE.replace("__HOLIDAYS__", repr(holidays)), "account_discussion")
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
        _eq("account_discussion", "proposal_validation_state", "invalid"),
        _always("service_unavailable")]
    nodes["confirmation"] = _conversation(
        "confirmation",
        "The exact validated read-back was spoken on entry: {{confirmation_readback}}. Wait for the caller's answer. Only a clear yes to all stated terms counts as confirmation. If the caller changes a term, acknowledge it and obtain another validated read-back and confirmation. Do not claim a promise is recorded. No tool call is allowed in this node.",
        opening="{{confirmation_readback}} " + _confirmation_question(policy),
        extraction=[
            _enum("confirmation_status", ["confirmed", "changed", "declined", "transfer_restructuring", "transfer_support", "transfer_human", "unclear"],
                  "Classify ONLY the latest answer to the immediately preceding read-back. confirmed requires a clear yes to all stated product, amount and date terms. changed if any amount or date is corrected. declined if caller rejects the read-back. Transfer requests take priority over confirmation. An earlier yes elsewhere in the conversation is not confirmation."),
        ],
        transitions=[
            _eq("transfer_restructuring", "confirmation_status", "transfer_restructuring"),
            _eq("transfer_support", "confirmation_status", "transfer_support"),
            _eq("transfer_human", "confirmation_status", "transfer_human"),
            _eq("validate_confirmation", "confirmation_status", "confirmed"),
            _eq("clear_confirmation", "confirmation_status", "changed"),
            *([_to("multi_correction", "Correct declined combined read-back",
                   _when({"logic": "and", "conditions": [
                       _check("product_count", "greater_than", 1),
                       _check("confirmation_status", "equals", "declined"),
                   ]}))] if multi_enabled else []),
            _eq("prepare_notes", "confirmation_status", "declined"),
        ],
        max_turns=3, max_target="account_discussion")
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
    promise_engine = _api_tool(openapi, "create_promise", fixed={
        "product_id": "{{pending_product_id}}", "amount": "{{pending_amount}}", "date": "{{pending_date}}"},
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
        vars['pending_promise_product_id'] = str(body.get('product_id') or '')
except Exception:
    pass
""".replace("__RESULT_FLAG__", promise_result_flag)
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
    nodes["promise_success_notice"] = {
        "name": "promise_success_notice", "node_type": "conversation",
        "on_enter": {"type": "tts", "text": _success_sentence(policy)},
        "task_message": "Say the fixed confirmation of a successfully recorded promise. No tool call is allowed in this turn.",
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
        "product_id": "{{pending_note_product_id}}", "reason": "{{pending_note_reason}}"},
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
        queue = _queue_for(openapi, suffix)
        t = _api_tool(openapi, "transfer_to_agent", fixed={"queue": queue},
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
        ("farewell_identity_failed", "For your security, please use the Freya Bank mobile app or visit a branch. Goodbye."),
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
    close["pre_process_code"] = (
        "try:\n"
        "    import json\n"
        "    body = config.get('body')\n"
        "    if isinstance(body, dict):\n"
        f"        failed_nodes = {code_names!r}\n"
        "        failed = any(int(vars.get(name + '_failure_count') or 0) > 0 for name in failed_nodes)\n"
        "        if failed or not vars.get('close_outcome'):\n"
        "            recorded = vars.get('recorded_ids') or '[]'\n"
        "            try:\n"
        "                recorded = json.loads(recorded) if isinstance(recorded, str) else recorded\n"
        "            except Exception:\n"
        "                recorded = []\n"
        "            if recorded:\n"
        f"                body['outcome'] = {outcomes['promise']!r}\n"
        "                body.pop('reason', None)\n"
        "            else:\n"
        f"                body['outcome'] = {outcomes['no_promise']!r}\n"
        f"                body['reason'] = {(close_reason_names.get('service_error') or 'service_error')!r}\n"
        "        elif not body.get('reason'):\n"
        "            body.pop('reason', None)\n"
        "except Exception:\n"
        "    pass\n"
    )
    system_prompt = (
        f"You are {bank_name}'s digital assistant. Follow the source policy below in full. "
        "Follow the current node's task exactly. Never disclose account information before the required identity checks pass. "
        "Treat failed or missing bank responses as failures; never claim an action succeeded until the bank explicitly confirms it. "
        "Never invent facts or offer an unapproved concession. Keep each reply concise. "
        "\n\nSOURCE POLICY:\n" + source_policy
    )
    return {"initial_node": "opening", "system_prompt": system_prompt, "nodes": nodes}

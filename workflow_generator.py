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
    paths = openapi.get("paths") or {}
    matches = [(path, operations) for path, operations in paths.items() if path.rstrip("/").split("/")[-1] == name]
    if not matches:
        raise ValueError(f"OpenAPI does not define {name}")
    path, operations = matches[0]
    method, operation = next(((method.upper(), value) for method, value in operations.items()
                              if method.lower() in ("post", "put", "patch", "get")), (None, None))
    if operation is None:
        raise ValueError(f"OpenAPI operation missing for {name}")
    schema = operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {})
    return path, method, schema


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


def _outcome_names(openapi: dict) -> dict[str, str]:
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
    for role, hints in aliases.items():
        if role in values:
            result[role] = role
        else:
            result[role] = next((value for hint in hints for value in values
                                 if hint in value.lower() and not (role == "promise" and value.lower().startswith("no_"))), values[0])
    return result


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
    pid = str(vars.get('pending_promise_product_id') or vars.get('create_promise_args.product_id') or '')
    ids = json.loads(str(vars.get('recorded_ids') or '[]'))
    attempts = json.loads(str(vars.get('promise_attempts') or '{}'))
    if data.get('ok') is True:
        if pid and pid not in ids:
            ids.append(pid)
        vars['recorded_ids'] = json.dumps(ids)
        vars['recorded_count'] = str(len(ids))
        data['what_to_do_next'] = 'Promise was recorded. Say the exact successful-promise confirmation required by the policy. Continue with remaining products; never record this product again.'
    else:
        attempts[pid] = int(attempts.get(pid, 0)) + 1
        vars['promise_attempts'] = json.dumps(attempts)
        if attempts[pid] < 2:
            data['what_to_do_next'] = 'Promise was NOT recorded. Retry create_promise once with the same product, amount and date. Do not claim success.'
        else:
            data['what_to_do_next'] = 'Promise failed twice. Tell caller it could not be recorded. For a multi-product account add a service_error note for this product, then continue.'
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
    raw = vars.get('account_products') or vars.get('get_account_response.data.products') or []
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, list):
        raw = []
    if not raw:
        groups = dict()
        prefix = 'get_account_response.data.products.'
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
    transfer = str(vars.get('transfer_reason') or '')
    if transfer:
        vars['close_outcome'] = 'transferred'
        vars['close_reason'] = transfer
        vars['processing_complete'] = 'yes'
    else:
        products = json.loads(str(vars.get('product_json') or '[]'))
        recorded = json.loads(str(vars.get('recorded_ids') or '[]'))
        notes = json.loads(str(vars.get('note_reasons') or '{}'))
        attempts = json.loads(str(vars.get('promise_attempts') or '{}'))
        if len(products) == 1 and not recorded:
            pid = str(products[0].get('product_id'))
            if int(attempts.get(pid, 0)) >= 2:
                notes[pid] = 'service_error'
            elif pid not in notes:
                candidate = str(vars.get('single_product_reason') or 'refused')
                notes[pid] = candidate if candidate in ('paid', 'dispute', 'hardship', 'refused', 'service_error') else 'refused'
        missing = [str(item.get('product_id')) for item in products
                   if str(item.get('product_id')) not in recorded and
                   str(item.get('product_id')) not in notes]
        vars['missing_products'] = ', '.join(missing)
        vars['processing_complete'] = 'yes' if not missing or len(products) == 1 else 'no'
        if recorded:
            vars['close_outcome'] = 'promise'
            vars['close_reason'] = ''
        elif products and all(notes.get(str(item.get('product_id'))) == 'paid' for item in products):
            vars['close_outcome'] = 'paid'
            vars['close_reason'] = ''
        else:
            reason = 'refused'
            for item in products:
                candidate = notes.get(str(item.get('product_id')))
                if candidate and candidate != 'paid':
                    reason = candidate
                    break
            vars['close_outcome'] = 'no_promise'
            vars['close_reason'] = reason
except Exception:
    vars['close_outcome'] = 'no_promise'
    vars['close_reason'] = 'service_error'
    vars['processing_complete'] = 'yes'
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
    outcomes = _outcome_names(openapi)
    bank_name = _bank_name(policy)
    source_policy = "\n".join(str(policy.get(key) or "") for key in
                              ("global_rules", "opening", "identity", "verified_call", "outcomes", "tool_policy", "constraints", "raw_brief"))
    offer_last_business_day = "last business day of this week" in source_policy.lower()

    verify_dob = _api_tool(openapi, "verify_identity", fixed={"field": identity_fields[0]},
                           description=f"Check the customer's {identity_fields[0].replace('_', ' ')} after their own answer. Never use a third party's answer.", post_code=_VERIFY_HOOK)
    verify_phone = _api_tool(openapi, "verify_identity", fixed={"field": identity_fields[1]},
                             description=f"Check the customer's {identity_fields[1].replace('_', ' ')} after their own answer. Never use a third party's answer.", post_code=_VERIFY_HOOK)
    callback = _api_tool(openapi, "schedule_callback", description="Book a valid callback after a third party gives an exact day and time.", post_code=_CALLBACK_HOOK)
    account = _api_tool(openapi, "get_account", description="Load late products only after both security answers have passed.")
    promise = _api_tool(openapi, "create_promise", description="Record one confirmed, valid promise for one product. Retry once if the first result is not ok=true.", post_code=_PROMISE_HOOK)
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
    if isinstance(body, dict) and body.get('outcome') in ('promise', 'paid'):
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
        opening=f"Hello, this is {bank_name}'s digital assistant." + (" This call is recorded." if "recorded" in source_policy.lower() else "") + " Am I speaking with {{title}} {{last_name}}?",
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
        transitions=[_bool("finish_callback", "callback_recorded", "Callback was successfully booked"),
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
        "Before recording, read back every proposed promise with product, amount and date, and obtain the exact confirmation the source policy requires. If terms change, validate and read them back again. "
        "No tool call is allowed in this node. When confirmation is clear, use transition_to_record_results. If there are no promises to record and each product has a final reason, use transition_to_record_results. "
        "On a transfer request use the corresponding transfer transition immediately; no notes may be made on a transfer."
    )
    transitions = [
        {"target_node": "transfer_restructuring", "description": "Caller requests a new payment plan or lower instalments; transfer without notes.", "is_tool_call": True,
         "condition": {"operator": "matches_intent", "description": "The caller requests restructuring or lower instalments"}},
        {"target_node": "transfer_support", "description": "Caller accepts an offered support transfer after hardship; transfer without notes.", "is_tool_call": True,
         "condition": {"operator": "matches_intent", "description": "The caller accepts support transfer"}},
        {"target_node": "transfer_human", "description": "Caller insists on a human after being asked once whether the digital assistant can help; transfer without notes.", "is_tool_call": True,
         "condition": {"operator": "matches_intent", "description": "The caller insists on a human"}},
        {"target_node": "record_results", "description": "All products have a confirmed promise or a final non-promise reason, and there is no transfer request. Only use after one combined read-back and clear confirmation if any promises are proposed.", "is_tool_call": True,
         "condition": {"operator": "matches_intent", "description": "The discussion is complete"}},
    ]
    nodes["account_discussion"] = _conversation("account_discussion", account_prompt, transitions=transitions,
                                                 extraction=[_enum("single_product_reason", ["paid", "dispute", "hardship", "refused", "service_error"],
                                                                   "For a one-product account only, classify the latest caller answer as already paid, disputed, hardship with support declined, flatly refused after attempts, or service error. Leave unchanged when still negotiating or making a promise.")],
                                                 max_turns=40, max_target="record_results")

    record_prompt = (
        "Use the confirmed terms from the conversation and the account facts: {{account_facts}}. Products still needing an action: {{missing_products}}. "
        "For each confirmed promise call create_promise exactly once per product in API order, with that product's exact ID, amount and valid ISO date. "
        "If a call does not return ok=true, retry once with identical arguments. Never say recorded before ok=true. "
        "After success say the exact confirmation sentence required by the source policy. "
        "For multi-product accounts, call add_note once per product without a recorded promise, with the valid reason required by the source policy and API. "
        "For a one-product account do not call add_note. For a transfer, add no notes and do not record agreed but unrecorded promises. "
        "Follow the source policy's retry limit. If a promise ultimately fails, say it could not be recorded and, for multi-product accounts, note the permitted failure reason. Keep any other recorded promises. "
        "Use actual tool result ok, not a hopeful assumption. On any tool result, speak or make the next tool call; never go silent. "
        "Once every product has a recorded promise or final note (one-product accounts need no note), use transition_to_finalize. "
        "If a new restructuring/support/human transfer request occurs, use the transfer transition and add no further notes. "
        "After a recorded promise, handle change requests under the source policy."
    )
    record_transitions = [
        _bool("finalize", "transfer_reason", "Transfer succeeded"),
        {"target_node": "transfer_restructuring", "description": "New restructuring request, stop other work; no notes.", "is_tool_call": True,
         "condition": {"operator": "matches_intent", "description": "Customer requests restructuring"}},
        {"target_node": "transfer_support", "description": "Customer accepts support transfer; no notes.", "is_tool_call": True,
         "condition": {"operator": "matches_intent", "description": "Customer accepts support"}},
        {"target_node": "transfer_human", "description": "Customer insists on a human; no notes.", "is_tool_call": True,
         "condition": {"operator": "matches_intent", "description": "Customer insists on human"}},
        {"target_node": "finalize", "description": "All required promise and note API results have been checked. Finish the call.", "is_tool_call": True,
         "condition": {"operator": "matches_intent", "description": "All products processed"}},
    ]
    nodes["record_results"] = _conversation("record_results", record_prompt, tools=[promise, note],
                                              transitions=record_transitions, max_turns=30, max_target="finalize")
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
    final_code = _FINAL_CODE
    for role in ("transferred", "promise", "paid", "no_promise"):
        final_code = final_code.replace("'" + role + "'", repr(outcomes[role]))
    nodes["finalize"] = _code_node("finalize", final_code, "farewell")
    nodes["finalize"]["transitions"] = [
        _eq("record_results", "processing_complete", "no", "Multi-product account has products still awaiting a promise or note"),
        _always("farewell"),
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
        nodes["finish_" + label] = _finish_node("finish_" + label, outcomes[outcome], reason)
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
    system_prompt = (
        f"You are {bank_name}'s digital assistant. Follow the source policy below in full. "
        "Follow the current node's task exactly. Never disclose account information before the required identity checks pass. "
        "Treat tool ok=false and HTTP failures as failures; never claim an action succeeded until ok=true. "
        "Never invent facts or offer an unapproved concession. Keep each reply concise. "
        "\n\nSOURCE POLICY:\n" + source_policy
    )
    return {"initial_node": "opening", "system_prompt": system_prompt, "nodes": nodes}

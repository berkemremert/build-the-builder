"""Conservative Freya workflow for an unfamiliar bank API contract.

This is an emergency fallback, not a replacement for the contract-specific
generator. It derives every callable API tool from OpenAPI and keeps account
access and writes behind an explicit, bank-confirmed identity result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from api_parameters import required_header_bindings
from api_servers import operation_url
from identity_order import latest_explicit_identity_order
from openapi_resolver import normalize_openapi
from source_code_mappings import explicit_code_mappings


_METHODS = {"get", "post", "put", "patch", "delete"}


@dataclass(frozen=True)
class _Operation:
    name: str
    path: str
    method: str
    source: dict
    role: str


def _source_policy(policy: dict) -> str:
    sections = [policy.get(key) for key in
                ("global_rules", "opening", "identity", "verified_call", "outcomes",
                 "tool_policy", "constraints", "raw_brief")]
    return "\n\n".join(str(part) for part in sections if part)


def _declared_call_variables(source: str) -> set[str]:
    """Find variables explicitly supplied with each call in source prose."""
    names: set[str] = set()
    lines = source.splitlines()
    for index, line in enumerate(lines):
        if not re.search(r"\b(?:every|each|per) call\b.{0,100}\b(?:variables?|inputs?)\b|"
                         r"\b(?:call variables?|call inputs?)\b", line, re.I):
            continue
        for candidate in lines[index + 1:index + 12]:
            if re.match(r"\s*#{1,6}\s", candidate) or candidate.startswith("===== SOURCE:"):
                break
            match = re.match(r"\s*[-*]\s*`([A-Za-z_][A-Za-z0-9_]*)`\s*:", candidate)
            if match:
                names.add(match.group(1))
    return names


def _upstream_identity_confirmed(source: str) -> bool:
    """Recognize an explicit prior IVR identity check, never infer one from tools."""
    matches = re.finditer(
        r"\b(?:IVR|phone menu)\b.{0,180}\b(?:verified|authenticated|checked (?:identity|who they are))\b|"
        r"\b(?:verified|authenticated)\b.{0,80}\b(?:by|through|in)\s+(?:the\s+)?(?:IVR|phone menu)\b",
        source, re.I | re.S)
    return any(not re.search(r"\b(?:not|never|without|unable)\b", match.group(0), re.I)
               for match in matches)


def _role(name: str, method: str, operation: dict, tool_rule: str) -> str:
    label = re.sub(r"[_./-]+", " ", re.sub(r"([a-z])([A-Z])", r"\1 \2", name)).lower()
    summary = str(operation.get("summary") or "").lower()
    description = str(operation.get("description") or "").lower()
    detail = " ".join((label, summary, description))
    if re.search(r"verif|identit|authenticat|security.check|security.question", label):
        return "verify"
    if re.search(r"close|finish|end.call|finali[sz]|disposition", label):
        return "close"
    if re.search(r"verif|identit|authenticat|security.check", summary):
        return "verify"
    if re.search(r"close.call|finish.call|end.call|finali[sz].call|disposition", summary):
        return "close"
    if method == "get":
        return "read"
    if re.search(r"\b(?:send|change|update|modify|save|submit|create|record|set|write|"
                 r"g[oö]nder|de[gğ]i[sş]tir|g[uü]ncelle|kaydet|olu[sş]tur|tan[iı]mla)\b", detail):
        return "write"
    if re.search(r"\b(?:get|fetch|retrieve|read|list|lookup|query|search|show|view|"
                 r"sorgula|listele|g[oö]r[uü]nt[uü]le|getir|oku)\b", detail):
        return "read"
    if re.search(r"\b(?:read.only|retrieve|lookup|query|list|sorgula|listele)\b", tool_rule, re.I):
        return "read"
    return "write"


def _operations(openapi: dict, policy: dict) -> list[_Operation]:
    rules = policy.get("tool_policy") or {}
    rules = rules if isinstance(rules, dict) else {}
    operations = []
    names = set()
    for path, path_item in openapi["paths"].items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _METHODS or not isinstance(operation, dict):
                continue
            name = operation.get("operationId") or path.rstrip("/").split("/")[-1]
            if not isinstance(name, str) or not name or name in names:
                raise ValueError(f"OpenAPI operation name {name!r} is missing or ambiguous")
            names.add(name)
            rule = str(rules.get(name) or "")
            operations.append(_Operation(name, path, method.upper(), operation,
                                         _role(name, method.lower(), operation, rule)))
    if not operations:
        raise ValueError("OpenAPI has no callable operations")
    return operations


def describe_contract(openapi: dict, policy: dict | None = None) -> list[dict]:
    """Expose the exact OpenAPI operation names, methods, paths, and URLs.

    This is intended for rehearsal diagnostics; names are never synthesized or
    silently mapped to a familiar bank operation.
    """
    document = normalize_openapi(openapi)
    result = []
    for operation in _operations(document, policy or {}):
        item = {"operation_id": operation.source.get("operationId"), "tool_name": operation.name,
                "method": operation.method,
                "path": operation.path, "role": operation.role}
        try:
            item["url"] = operation_url(document, operation.path, operation.method)
            required_header_bindings(document, operation.path, operation.method)
            item["callable"] = True
        except ValueError as error:
            item["url"] = item.get("url")
            item["callable"] = False
            item["reason"] = str(error)
        result.append(item)
    return result


def _boolean_result(operation: _Operation, terms: str) -> str | None:
    fields = set()
    for status, response in (operation.source.get("responses") or {}).items():
        if not str(status).startswith("2") or not isinstance(response, dict):
            continue
        schema = ((response.get("content") or {}).get("application/json") or {}).get("schema") or {}
        for name, spec in (schema.get("properties") or {}).items():
            if (isinstance(spec, dict) and spec.get("type") == "boolean" and
                    re.search(terms, name + " " + str(spec.get("description") or ""), re.I)):
                fields.add(name)
    return next(iter(fields)) if len(fields) == 1 else None


def _identity_completion_field(operation: _Operation) -> str | None:
    """Prefer a field explicitly denoting the whole identity check.

    A per-question ``verified`` result is insufficient when the response also
    exposes ``all_verified`` or an equivalent completion field.
    """
    candidates = set()
    full = set()
    for status, response in (operation.source.get("responses") or {}).items():
        if not str(status).startswith("2") or not isinstance(response, dict):
            continue
        schema = ((response.get("content") or {}).get("application/json") or {}).get("schema") or {}
        for name, spec in (schema.get("properties") or {}).items():
            if not isinstance(spec, dict) or spec.get("type") != "boolean":
                continue
            description = str(spec.get("description") or "")
            if re.search(r"verif|identit|authenticat|complete|passed", name + " " + description, re.I):
                candidates.add(name)
            if (re.search(r"(?:all|fully|entire|both|complete|completed).*"
                          r"(?:verif|identit|authenticat|check|question)|"
                          r"(?:verif|identit|authenticat|check).*"
                          r"(?:complete|completed|all|fully|both)", name + " " + description, re.I)
                    or name.lower() in {"all_verified", "fully_verified", "identity_complete",
                                       "verification_complete", "security_complete"}):
                full.add(name)
    if len(full) == 1:
        return next(iter(full))
    return next(iter(candidates)) if not full and len(candidates) == 1 else None


def _tool(openapi: dict, operation: _Operation, rule: str, result_field: str | None = None,
          fixed: dict | None = None, call_variables: set[str] | None = None) -> dict:
    url = operation_url(openapi, operation.path, operation.method)
    headers = {"Content-Type": {"type": "string", "default": "application/json"}}
    headers.update(required_header_bindings(openapi, operation.path, operation.method))
    schema = (((operation.source.get("requestBody") or {}).get("content") or {})
              .get("application/json") or {}).get("schema") or {}
    if schema and schema.get("type", "object") != "object":
        raise ValueError(f"{operation.name} request must be a JSON object")
    body_properties = {}
    for name, spec in (schema.get("properties") or {}).items():
        if not isinstance(spec, dict):
            raise ValueError(f"{operation.name} request field {name!r} is invalid")
        leaf = {"type": spec.get("type", "string"),
                "description": spec.get("description") or f"{name} for {operation.name}."}
        if "enum" in spec:
            leaf["enum"] = spec["enum"]
        if name in (fixed or {}):
            if ("enum" in spec and fixed[name] not in spec["enum"] and
                    not re.fullmatch(r"\{\{[A-Za-z_][A-Za-z0-9_]*\}\}", str(fixed[name]))):
                raise ValueError(f"{operation.name} fixed value for {name!r} is outside OpenAPI enum")
            leaf["default"] = fixed[name]
        elif "const" in spec:
            leaf["default"] = spec["const"]
        elif "default" in spec:
            leaf["default"] = spec["default"]
        elif isinstance(spec.get("enum"), list) and len(spec["enum"]) == 1:
            leaf["default"] = spec["enum"][0]
        elif name in (call_variables or set()):
            leaf["default"] = "{{" + name + "}}"
        elif re.fullmatch(r"(?:customer|client|party|person)_?id", name, re.I):
            leaf["default"] = "{{customer_id}}"
        body_properties[name] = leaf
    result = {
        "type": "function", "config": {"timeoutMs": 12000, "maxRetries": 0},
        "function": {"name": operation.name, "handler": "__function__:api_call",
                     "description": rule or operation.source.get("description") or operation.source.get("summary")
                     or f"Call {operation.name} only under the source policy conditions.",
                     "parameters": {"type": "object", "properties": {
                         "url": {"type": "string", "default": url},
                         "method": {"type": "string", "default": operation.method},
                         "headers": {"type": "object", "properties": headers},
                         "body": {"type": "object", "properties": body_properties,
                                  "required": schema.get("required") or []},
                     }}},
    }
    if result_field and operation.role == "write":
        result["post_process_code"] = (
            "try:\n"
            "    result.pop('header', None)\n"
            "    data = result.get('data') if isinstance(result, dict) else None\n"
            "    if not isinstance(data, dict):\n"
            "        data = {}\n"
            "        result['data'] = data\n"
            f"    ok = str(result.get('status_code') or '').startswith('2') and data.get({result_field!r}) is True\n"
            "    vars['generic_write_result'] = 'confirmed' if ok else 'unknown'\n"
            f"    vars['generic_recorded_action'] = {operation.name!r} if ok else ''\n"
            "    data['what_to_do_next'] = ('The bank confirmed the action. Follow the exact source policy for confirmation.' "
            "if ok else 'The action is unconfirmed. Do not retry or claim success.')\n"
            "except Exception:\n"
            "    pass\n"
        )
    elif result_field and operation.role == "verify":
        result["post_process_code"] = (
            "try:\n"
            "    data = result.get('data') if isinstance(result, dict) else None\n"
            f"    if str(result.get('status_code') or '').startswith('2') and isinstance(data, dict) and data.get({result_field!r}) is True:\n"
            "        vars['generic_identity_verified'] = 'true'\n"
            "except Exception:\n"
            "    pass\n"
        )
    return result


def _outcome_codes(source: str, close: _Operation) -> tuple[str, dict[str, str], str | None, dict[str, set[str]]]:
    schema = (((close.source.get("requestBody") or {}).get("content") or {})
              .get("application/json") or {}).get("schema") or {}
    properties = schema.get("properties") or {}
    outcome_fields = [(name, spec) for name, spec in properties.items()
                      if re.search(r"outcome|disposition|call_result", name, re.I)
                      and isinstance(spec, dict) and isinstance(spec.get("enum"), list)]
    if len(outcome_fields) != 1:
        raise ValueError("Close operation needs one outcome enum to close deterministically")
    outcome_field, outcome_spec = outcome_fields[0]
    values = [value for value in outcome_spec["enum"] if isinstance(value, str)]
    explicit = explicit_code_mappings(source, "outcome", values)
    codes = {}
    for role in ("promise", "no_promise", "not_verified", "not_reached", "transferred"):
        if role in explicit:
            codes[role] = explicit[role]
        elif role in values:
            codes[role] = role
        else:
            matches = [value for value in values if re.search(role.replace("_", r"[_ -]?"), value, re.I)]
            if len(matches) == 1:
                codes[role] = matches[0]
    required = ("promise", "no_promise", "not_verified", "not_reached")
    if any(role not in codes for role in required):
        raise ValueError(f"Close outcome enum cannot map required roles {required!r} from source/OpenAPI")
    reason_fields = [name for name in properties if re.search(r"reason", name, re.I)]
    reason_field = reason_fields[0] if len(reason_fields) == 1 else None
    if len(reason_fields) > 1:
        raise ValueError("Close operation has ambiguous reason fields")
    reasons: dict[str, set[str]] = {}
    headers = None
    for line in source.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            headers = None
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        normalized = [cell.strip(" `*").lower() for cell in cells]
        if "outcome" in normalized and "reason" in normalized:
            headers = (normalized.index("outcome"), normalized.index("reason"))
            continue
        if headers is None or max(headers) >= len(cells) or all(re.fullmatch(r":?-{3,}:?", c) for c in normalized):
            continue
        outcome_tokens = re.findall(r"`([^`]+)`", cells[headers[0]])
        reason_tokens = re.findall(r"`([^`]+)`", cells[headers[1]])
        if len(outcome_tokens) == 1 and reason_tokens:
            reasons.setdefault(outcome_tokens[0], set()).update(reason_tokens)
    if reason_field and re.search(r"required|must", str(properties[reason_field].get("description") or ""), re.I):
        for role in ("no_promise", "not_verified", "not_reached"):
            if not reasons.get(codes[role]):
                raise ValueError(f"Source does not define close reasons for {codes[role]!r}")
    return outcome_field, codes, reason_field, reasons


def _close_tool(openapi: dict, close: _Operation, source: str, rule: str,
                write_operations: list[_Operation]) -> dict:
    outcome_field, codes, reason_field, reasons = _outcome_codes(source, close)
    action_outcomes = {}
    for operation in write_operations:
        label = operation.name.lower()
        role = ("promise" if re.search(r"promise|arrangement|pledge", label) else
                "transferred" if re.search(r"transfer|handoff", label) else
                "not_reached" if re.search(r"callback|schedule", label) else
                "no_promise" if re.search(r"note|record.reason", label) else None)
        if role in codes:
            action_outcomes[operation.name] = codes[role]
    tool = _tool(openapi, close, rule, fixed={outcome_field: "{{generic_close_outcome}}"})
    tool["pre_process_code"] = (
        "def deny(message):\n"
        "    config['url'] = ''\n"
        "    config['body'] = {}\n"
        "    raise ValueError(message)\n"
        "body = config.get('body')\n"
        "if not isinstance(body, dict):\n"
        "    deny('Close request body is unavailable')\n"
        "if vars.get('generic_close_attempted') == 'true':\n"
        "    deny('Close already attempted')\n"
        f"codes = {codes!r}\n"
        f"action_outcomes = {action_outcomes!r}\n"
        f"allowed_reasons = dict((key, set(values)) for key, values in {[(key, sorted(values)) for key, values in reasons.items()]!r})\n"
        "if vars.get('generic_write_result') == 'confirmed':\n"
        "    outcome = action_outcomes.get(str(vars.get('generic_recorded_action') or ''))\n"
        "elif vars.get('generic_identity_verified') == 'true':\n"
        "    outcome = codes['no_promise']\n"
        "elif vars.get('caller_role') == 'customer':\n"
        "    outcome = codes['not_verified']\n"
        "else:\n"
        "    outcome = codes['not_reached']\n"
        "if not outcome:\n"
        "    deny('Cannot determine truthful close outcome')\n"
        "reason = str(vars.get('generic_close_reason') or '')\n"
        "if not reason and outcome == codes['not_reached']:\n"
        "    role = str(vars.get('caller_role') or '')\n"
        "    candidates = [entry for entry in allowed_reasons.get(outcome, set()) "
        "if (role == 'wrong_number' and 'wrong' in entry.lower()) or "
        "(role == 'third_party' and 'no_callback' in entry.lower()) or "
        "(role == 'unknown' and 'unclear' in entry.lower())]\n"
        "    if len(candidates) == 1:\n"
        "        reason = candidates[0]\n"
        "if outcome not in allowed_reasons:\n"
        "    reason = ''\n"
        "if outcome in allowed_reasons and reason not in allowed_reasons[outcome]:\n"
        "    deny('Cannot determine source-approved close reason')\n"
        f"body[{outcome_field!r}] = outcome\n"
        + (f"if reason:\n    body[{reason_field!r}] = reason\nelse:\n    body.pop({reason_field!r}, None)\n"
           if reason_field else "") +
        "vars['generic_close_attempted'] = 'true'\n"
    )
    return tool


def _identity_steps(operation: _Operation, source: str) -> tuple[str, list[str]] | None:
    """Find the source-ordered security selectors when the API declares them."""
    schema = (((operation.source.get("requestBody") or {}).get("content") or {})
              .get("application/json") or {}).get("schema") or {}
    choices = []
    for name, spec in (schema.get("properties") or {}).items():
        values = spec.get("enum") if isinstance(spec, dict) else None
        if isinstance(values, list) and len(values) >= 2 and all(isinstance(value, str) for value in values):
            choices.append((name, values))
    if len(choices) != 1:
        return None
    field, values = choices[0]
    explicit = latest_explicit_identity_order(source, values)
    return field, explicit or values


def _question_result_field(operation: _Operation, complete_field: str) -> str | None:
    fields = set()
    for status, response in (operation.source.get("responses") or {}).items():
        if not str(status).startswith("2") or not isinstance(response, dict):
            continue
        schema = ((response.get("content") or {}).get("application/json") or {}).get("schema") or {}
        for name, spec in (schema.get("properties") or {}).items():
            if (name != complete_field and isinstance(spec, dict) and spec.get("type") == "boolean"
                    and re.search(r"verif|passed|correct|accepted", name, re.I)):
                fields.add(name)
    return next(iter(fields)) if len(fields) == 1 else None


def _when(variable: str, value: str, target: str, description: str) -> dict:
    return {"target_node": target, "description": description,
            "condition": {"logic": "or", "conditions": [
                {"variable": variable, "operator": "equals", "operand": value}]}}


def _always(target: str) -> dict:
    return {"target_node": target, "description": "Continue",
            "condition": {"operator": "always"}}


def _conversation(name: str, prompt: str, *, tools: list[dict] | None = None,
                  extraction: list[dict] | None = None, transitions: list[dict] | None = None,
                  turns: int = 8, timeout: str = "farewell") -> dict:
    node = {"name": name, "node_type": "conversation", "on_enter": {"type": "llm"},
            "task_message": prompt, "transitions": transitions or [],
            "max_node_turns": turns, "max_turn_transition": timeout}
    if tools:
        node["tools"] = tools
    if extraction:
        node["extraction_schema"] = extraction
    return node


def _end_node() -> dict:
    return {"name": "ended", "node_type": "end_call", "on_enter": {"type": "silent"},
            "disable_inactivity_monitor": True, "transitions": [],
            "terminal": {"type": "function", "function": {
                "name": "freya_call_complete", "handler": "__function__:end_call",
                "parameters": {"type": "object", "properties": {
                    "message_mode": {"type": "string", "default": "none"}}}}}}


def build_generic_workflow(policy: dict, openapi: dict) -> dict:
    """Build a source-grounded, fail-closed workflow from a changed contract.

    Unsupported operation bindings are omitted. If no unique identity success
    field exists, no account or write operation is exposed to the conversation.
    """
    openapi = normalize_openapi(openapi)
    policy = policy or {}
    source = _source_policy(policy)
    call_variables = _declared_call_variables(str(policy.get("raw_brief") or source))
    operations = _operations(openapi, policy)
    rules = policy.get("tool_policy") or {}
    rules = rules if isinstance(rules, dict) else {}
    tools = {}
    proof = {}
    for operation in operations:
        terms = (r"verif|identit|authenticat|complete|passed" if operation.role == "verify"
                 else r"\bok\b|success|saved|recorded|created|completed|closed|transferred|scheduled")
        proof[operation.name] = (_identity_completion_field(operation) if operation.role == "verify"
                                 else _boolean_result(operation, terms))
        try:
            tools[operation.name] = _tool(openapi, operation, str(rules.get(operation.name) or ""),
                                          proof[operation.name], call_variables=call_variables)
        except ValueError:
            # The API transport cannot safely bind some OpenAPI parameters.
            # Omission keeps the workflow runnable without fabricating values.
            continue
    verify = [op for op in operations if op.role == "verify" and op.name in tools and proof[op.name]]
    upstream_verified = len(verify) == 0 and _upstream_identity_confirmed(source)
    if len(verify) != 1 and not upstream_verified and any(op.role in {"verify", "read", "write"} for op in operations):
        raise ValueError("Cannot identify exactly one callable identity operation with an unambiguous "
                         "bank-confirmed full-verification boolean result")
    verified = len(verify) == 1
    verify_op = verify[0] if verified else None
    read_tools = [tools[op.name] for op in operations if op.role == "read" and op.name in tools]
    write_tools = [tools[op.name] for op in operations if op.role == "write" and op.name in tools and proof[op.name]]
    close_operations = [op for op in operations if op.role == "close" and op.name in tools]
    if len(close_operations) > 1:
        raise ValueError("OpenAPI has more than one callable close operation")
    close_operation = close_operations[0] if close_operations else None
    close_tool = (_close_tool(openapi, close_operation, source,
                              str(rules.get(close_operation.name) or ""),
                              [op for op in operations if op.role == "write" and op.name in tools])
                  if close_operation else None)
    close_reasons = _outcome_codes(source, close_operation)[3] if close_operation else {}
    close_codes = _outcome_codes(source, close_operation)[1] if close_operation else {}
    service_errors = [value for value in close_reasons.get(close_codes.get("no_promise", ""), set())
                      if re.search(r"service.*error|system.*error|technical.*error", value, re.I)]
    if len(service_errors) == 1:
        for write_tool in write_tools:
            write_tool["post_process_code"] += (
                "try:\n"
                "    if vars.get('generic_write_result') == 'unknown':\n"
                f"        vars['generic_close_reason'] = {service_errors[0]!r}\n"
                "except Exception:\n"
                "    pass\n"
            )
    def reason_extraction(role: str) -> list[dict]:
        values = sorted(close_reasons.get(close_codes.get(role, ""), set()))
        return ([{"name": "generic_close_reason", "type": "string", "enum_values": values,
                 "description": "Choose only the source-approved reason explicitly supported by the caller or bank result.",
                 "min_confidence": 0.9}] if values else [])
    call_context = ("Call inputs supplied by the platform: " +
                    ", ".join(f"{name}={{{{{name}}}}}" for name in sorted(call_variables)) + ". "
                    if call_variables else "")
    identity_rule = ("The source says the upstream IVR confirmed identity. Follow any additional identity or "
                     "second-factor requirement in the source before disclosing protected details. "
                     if upstream_verified else
                     "Never disclose account or debt details before the bank explicitly confirms identity. ")
    system_prompt = (
        "Follow the supplied bank policy exactly. " + identity_rule + call_context +
        "Never invent account facts, terms, tool results, or a successful write. "
        "If a required tool is unavailable or its result is ambiguous, explain that the action cannot be confirmed. "
        "Never retry an uncertain write. Use only tools available in the current stage.\n\nSOURCE POLICY:\n" + source
    )
    nodes = {
        "farewell": {"name": "farewell", "node_type": "conversation",
                     "on_enter": {"type": "tts", "text": "Thank you. Goodbye."},
                     "task_message": "End the call. Do not claim an action succeeded unless the bank confirmed it.",
                     "passthrough": True,
                     "passthrough_default_transition": "closing" if close_tool else "ended",
                     "transitions": [_always("closing" if close_tool else "ended")]},
        "ended": _end_node(),
    }
    if not upstream_verified:
        nodes["opening"] = _conversation(
            "opening", "Follow the source opening and confidentiality rules. Establish whether the speaker is "
            "the customer before requesting any account information. Do not mention debt or account details.",
            extraction=[{"name": "caller_role", "type": "string",
                         "enum_values": ["customer", "third_party", "wrong_number", "unknown"],
                         "description": "Confirmed role of the person speaking.", "min_confidence": 0.75}]
            + reason_extraction("not_reached"),
            transitions=[_when("caller_role", "customer", "verification" if verified else "farewell",
                               "Customer is on the line"), _always("farewell")],
            turns=4)
    if verified:
        assert verify_op is not None
        steps = _identity_steps(verify_op, source)
        per_question = _question_result_field(verify_op, proof[verify_op.name])
        if steps and len(steps[1]) > 1 and per_question:
            field, values = steps
            for index, value in enumerate(values):
                node_name = "verification" if index == 0 else f"verification_{index + 1}"
                next_node = f"verification_{index + 2}" if index + 1 < len(values) else None
                edges = [_when(f"{verify_op.name}_response.data.{proof[verify_op.name]}", "true",
                               "service", "Bank confirmed full identity"),
                         _when(f"{verify_op.name}_response.data.{proof[verify_op.name]}", "True",
                               "service", "Bank confirmed full identity")]
                if next_node:
                    edges.extend([
                        _when(f"{verify_op.name}_response.data.{per_question}", "true", next_node,
                              "Current security answer passed"),
                        _when(f"{verify_op.name}_response.data.{per_question}", "True", next_node,
                              "Current security answer passed"),
                    ])
                nodes[node_name] = _conversation(
                    node_name, f"Ask only for the customer's {value.replace('_', ' ')} under the source policy. "
                    "Use the bank identity tool for their own answer. Do not disclose account information until "
                    "the bank explicitly confirms every required check. Respect the source attempt limit.",
                    tools=[_tool(openapi, verify_op, str(rules.get(verify_op.name) or ""),
                                 proof[verify_op.name], fixed={field: value},
                                 call_variables=call_variables)],
                    extraction=reason_extraction("not_verified"), transitions=edges, turns=6)
        else:
            nodes["verification"] = _conversation(
                "verification", "Follow every identity step and attempt limit in the source policy. Ask for the "
                "speaker's own answers. Use the identity tool only for those answers. Continue only when the bank "
                "response explicitly confirms verification. If verification fails, end without disclosing account details.",
                tools=[tools[verify_op.name]], extraction=reason_extraction("not_verified"),
                transitions=[_when(f"{verify_op.name}_response.data.{proof[verify_op.name]}", "true", "service",
                                   "Bank confirmed identity"),
                             _when(f"{verify_op.name}_response.data.{proof[verify_op.name]}", "True", "service",
                                   "Bank confirmed identity")],
                turns=6)
    if verified or upstream_verified:
        nodes["service"] = _conversation(
            "service", "Use the source-confirmed identity context. Follow the source policy for account discussion, calculations, "
            "offers, and exceptions. Use read tools only when their policy conditions hold. Before a bank write, "
            "state the exact proposed action and obtain the customer's explicit confirmation. If no action is "
            "authorized, end the call. Do not imply any write was recorded here.",
            tools=read_tools,
            extraction=[{"name": "explicit_action_confirmation", "type": "string",
                         "enum_values": ["yes", "no", "unknown"],
                         "description": "Yes only when the customer explicitly confirms the exact proposed bank action.",
                         "min_confidence": 0.9}] + reason_extraction("no_promise"),
            transitions=([_when("explicit_action_confirmation", "yes", "commit",
                                "Customer explicitly confirmed the action")] if write_tools else [])
            + [_when("explicit_action_confirmation", "no", "farewell", "No action authorized")],
            turns=20)
        if write_tools:
            nodes["commit"] = _conversation(
                "commit", "The customer explicitly confirmed an exact action. Call only the bank write tool "
                "authorized by that confirmation and the source policy. The bank must return an explicit success "
                "flag before you say it succeeded. Never retry an ambiguous response. End after one confirmed or "
                "uncertain result.", tools=write_tools,
                transitions=[_when("generic_write_result", "confirmed", "farewell", "Write confirmed"),
                             _when("generic_write_result", "unknown", "farewell", "Write unconfirmed")],
                turns=3)
    if close_tool:
        nodes["closing"] = {
            "name": "closing", "node_type": "api_request", "on_enter": {"type": "silent"},
            "passthrough": True, "passthrough_default_transition": "ended",
            "pre_actions": [close_tool], "transitions": [_always("ended")],
        }
    return {"initial_node": "service" if upstream_verified else "opening",
            "system_prompt": system_prompt, "nodes": nodes}

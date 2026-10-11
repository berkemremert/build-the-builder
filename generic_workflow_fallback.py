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
from openapi_resolver import normalize_openapi


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


def _role(name: str, method: str, operation: dict, tool_rule: str) -> str:
    label = re.sub(r"([a-z])([A-Z])", r"\1 \2", name).lower()
    summary = str(operation.get("summary") or "").lower()
    if re.search(r"verif|identit|authenticat|security.check|security.question", label):
        return "verify"
    if re.search(r"close|finish|end.call|finali[sz]|disposition", label):
        return "close"
    if method == "get" or re.search(r"fetch|retrieve|read|list|lookup|get.account", label):
        return "read"
    if re.search(r"verif|identit|authenticat|security.check", summary):
        return "verify"
    if re.search(r"close.call|finish.call|end.call|finali[sz].call|disposition", summary):
        return "close"
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


def _tool(openapi: dict, operation: _Operation, rule: str, result_field: str | None = None) -> dict:
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
        if "const" in spec:
            leaf["default"] = spec["const"]
        elif "default" in spec:
            leaf["default"] = spec["default"]
        elif isinstance(spec.get("enum"), list) and len(spec["enum"]) == 1:
            leaf["default"] = spec["enum"][0]
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
            "    data['what_to_do_next'] = ('The bank confirmed the action. Follow the exact source policy for confirmation.' "
            "if ok else 'The action is unconfirmed. Do not retry or claim success.')\n"
            "except Exception:\n"
            "    pass\n"
        )
    return result


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
                                          proof[operation.name])
        except ValueError:
            # The API transport cannot safely bind some OpenAPI parameters.
            # Omission keeps the workflow runnable without fabricating values.
            continue
    verify = [op for op in operations if op.role == "verify" and op.name in tools and proof[op.name]]
    if len(verify) != 1 and any(op.role in {"verify", "read", "write"} for op in operations):
        raise ValueError("Cannot identify exactly one callable identity operation with an unambiguous "
                         "bank-confirmed full-verification boolean result")
    verified = len(verify) == 1
    verify_op = verify[0] if verified else None
    read_tools = [tools[op.name] for op in operations if op.role == "read" and op.name in tools]
    write_tools = [tools[op.name] for op in operations if op.role == "write" and op.name in tools and proof[op.name]]
    close_tools = [tools[op.name] for op in operations if op.role == "close" and op.name in tools]
    system_prompt = (
        "Follow the supplied bank policy exactly. Never disclose account or debt details before the bank explicitly "
        "confirms identity. Never invent account facts, terms, tool results, or a successful write. "
        "If a required tool is unavailable or its result is ambiguous, explain that the action cannot be confirmed. "
        "Never retry an uncertain write. Use only tools available in the current stage.\n\nSOURCE POLICY:\n" + source
    )
    nodes = {
        "opening": _conversation(
            "opening", "Follow the source opening and confidentiality rules. Establish whether the speaker is "
            "the customer before requesting any account information. Do not mention debt or account details.",
            extraction=[{"name": "caller_role", "type": "string",
                         "enum_values": ["customer", "third_party", "wrong_number", "unknown"],
                         "description": "Confirmed role of the person speaking.", "min_confidence": 0.75}],
            transitions=[_when("caller_role", "customer", "verification" if verified else "farewell",
                               "Customer is on the line"), _always("farewell")],
            turns=4),
        "farewell": {"name": "farewell", "node_type": "conversation",
                     "on_enter": {"type": "tts", "text": "Thank you. Goodbye."},
                     "task_message": "End the call. Do not claim an action succeeded unless the bank confirmed it.",
                     "passthrough": True,
                     "passthrough_default_transition": "closing" if close_tools else "ended",
                     "transitions": [_always("closing" if close_tools else "ended")]},
        "ended": _end_node(),
    }
    if verified:
        assert verify_op is not None
        nodes["verification"] = _conversation(
            "verification", "Follow every identity step and attempt limit in the source policy. Ask for the "
            "speaker's own answers. Use the identity tool only for those answers. Continue only when the bank "
            "response explicitly confirms verification. If verification fails, end without disclosing account details.",
            tools=[tools[verify_op.name]],
            transitions=[_when(f"{verify_op.name}_response.data.{proof[verify_op.name]}", "true", "service",
                               "Bank confirmed identity"),
                         _when(f"{verify_op.name}_response.data.{proof[verify_op.name]}", "True", "service",
                               "Bank confirmed identity")],
            turns=6)
        nodes["service"] = _conversation(
            "service", "The bank confirmed identity. Follow the source policy for account discussion, calculations, "
            "offers, and exceptions. Use read tools only when their policy conditions hold. Before a bank write, "
            "state the exact proposed action and obtain the customer's explicit confirmation. If no action is "
            "authorized, end the call. Do not imply any write was recorded here.",
            tools=read_tools,
            extraction=[{"name": "explicit_action_confirmation", "type": "string",
                         "enum_values": ["yes", "no", "unknown"],
                         "description": "Yes only when the customer explicitly confirms the exact proposed bank action.",
                         "min_confidence": 0.9}],
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
    if close_tools:
        nodes["closing"] = _conversation(
            "closing", "Use the source policy and actual call result to record a truthful call disposition. "
            "Call the closing tool once, then end. Never claim it succeeded without an explicit bank response.",
            tools=close_tools, transitions=[], turns=2, timeout="ended")
    return {"initial_node": "opening", "system_prompt": system_prompt, "nodes": nodes}

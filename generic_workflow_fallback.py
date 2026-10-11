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
        r"\b(?:IVR|phone menu)\b.{0,180}?\b(?:verified|authenticated|checked (?:identity|who they are))\b|"
        r"\b(?:verified|authenticated)\b.{0,80}\b(?:by|through|in)\s+(?:the\s+)?(?:IVR|phone menu)\b",
        source, re.I | re.S)
    return any(not re.search(r"\b(?:not|never|without|unable)\b", match.group(0), re.I)
               for match in matches)


def _role(name: str, method: str, operation: dict, tool_rule: str) -> str:
    label = re.sub(r"[_./-]+", " ", re.sub(r"([a-z])([A-Z])", r"\1 \2", name)).lower()
    summary = str(operation.get("summary") or "").lower()
    description = str(operation.get("description") or "").lower()
    detail = " ".join((label, summary, description))
    if (label in {"token", "access token", "refresh token"} or
            re.search(r"\b(?:oauth\d*\s+token|access\s+token|refresh\s+token|token\s+grant)\b", label)):
        return "auth"
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
    if re.search(r"\b(?:get|fetch|retrieve|read|list|lookup|query|search|view|balance|"
                 r"preview|decide|estimate|quote|calculate|inspect)\b", label):
        return "read"
    if re.search(r"\b(?:send|change|update|modify|save|submit|create|record|set|write|"
                 r"g[oö]nder|de[gğ]i[sş]tir|g[uü]ncelle|kaydet|olu[sş]tur|tan[iı]mla)\b", detail):
        return "write"
    if re.search(r"\b(?:get|fetch|retrieve|read|list|lookup|query|search|show|view|balance|"
                 r"preview|decide|estimate|quote|calculate|inspect|"
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


def _required_exit_state(rule: str) -> str | None:
    """Extract one exact exit state from an operation's positive success clause."""
    values = set()
    for match in re.finditer(r"\bdata\.exitState\s*(?:=|is|equal\s+to)?\s*[`\"']([A-Za-z0-9_-]+)[`\"']", rule, re.I):
        clause = rule[max(0, match.start() - 180):match.start()]
        if re.search(r"\b(?:success|requires?|required|only\s+on|confirmed)\b", clause, re.I):
            values.add(match.group(1))
    if len(values) > 1:
        raise ValueError("Source gives conflicting success exitState values")
    return next(iter(values)) if values else None


def _result_checks(operation: _Operation, rule: str = "", source: str = "") -> list[tuple[tuple[str, ...], object]]:
    """Read explicit success markers from a 2xx JSON response schema."""
    checks: list[tuple[tuple[str, ...], object]] = []

    def walk(schema: dict, path: tuple[str, ...] = ()) -> None:
        if not isinstance(schema, dict):
            return
        description = str(schema.get("description") or "")
        properties = schema.get("properties") or {}
        if isinstance(properties, dict):
            for name, child in properties.items():
                if not isinstance(child, dict):
                    continue
                child_path = (*path, name)
                walk(child, child_path)
                nested_description = str(child.get("description") or "")
                if child.get("type") == "boolean" and re.search(
                        r"\bok\b|success|saved|recorded|created|completed|sent|changed", name + " " + nested_description, re.I):
                    checks.append((child_path, True))
                elif re.search(r"\b(?:on|means?|indicates?)\s+success\b", nested_description, re.I):
                    value = re.search(r"\b([A-Za-z0-9_-]+)\s+(?:on|means?|indicates?)\s+success\b",
                                      nested_description, re.I)
                    if value:
                        raw = value.group(1)
                        checks.append((child_path, int(raw) if child.get("type") == "integer" and raw.isdecimal()
                                       else raw))
                elif re.search(r"\b(?:on|for)\s+(?:a\s+)?successful\b", nested_description, re.I):
                    value = re.search(r"\b([A-Za-z0-9_-]+)\s+(?:on|for)\s+(?:a\s+)?successful\b",
                                      nested_description, re.I)
                    if value:
                        raw = value.group(1)
                        checks.append((child_path, int(raw) if child.get("type") == "integer" and raw.isdecimal()
                                       else raw))
                elif (len(child.get("enum") or []) == 1 and
                      re.search(r"\b(?:exit.?state|response.?code|result.?status|success.?code)\b", name, re.I)):
                    checks.append((child_path, child["enum"][0]))
        if path and isinstance(properties, dict) and description:
            for name in properties:
                match = re.search(r"\b" + re.escape(name) + r"\s*[`\"']([^`\"']+)[`\"']\s+"
                                  r"(?:indicates?|means?)\s+success\b", description, re.I)
                if not match:
                    match = re.search(r"\bsuccess(?:ful)?\b[^.;\n]{0,100}?\b" + re.escape(name)
                                      + r"\s*[`\"']([^`\"']+)[`\"']", description, re.I)
                if match:
                    checks.append(((*path, name), match.group(1)))

    for status, response in (operation.source.get("responses") or {}).items():
        if not str(status).startswith("2") or not isinstance(response, dict):
            continue
        schema = ((response.get("content") or {}).get("application/json") or {}).get("schema") or {}
        walk(schema)
        description = str(response.get("description") or "")
        for path in _schema_paths(schema):
            if "*" in path:
                continue
            name = path[-1]
            match = re.search(r"\b" + re.escape(name) +
                              r"\s*[:=]?\s*[`\"']?([A-Za-z0-9_-]+)[`\"']?\s+"
                              r"(?:means?|indicates?)\s+(?:the\s+)?(?:preview\s+is\s+)?"
                              r"(?:fine|ok|successful?|accepted|completed?)\b",
                              description, re.I)
            if match:
                field = _schema_at_path(schema, path)
                raw = match.group(1)
                checks.append((path, int(raw) if field.get("type") == "integer" and raw.isdecimal()
                               else raw))
    response_schema = _json_schema(operation, response=True)
    response_descriptions = " ".join(str(response.get("description") or "") for status, response
                                     in (operation.source.get("responses") or {}).items()
                                     if str(status).startswith("2") and isinstance(response, dict))
    success_text = " ".join((source, rule, response_descriptions,
                             str(response_schema.get("description") or ""),
                             str((response_schema.get("properties") or {}).get("result", {}).get("description") or "")))
    if (('result', 'returnCode') in _schema_paths(response_schema) and
            (re.search(r"\bsuccess\b.{0,100}\breturnCode\b.{0,35}[`\"']1[`\"']", success_text, re.I | re.S)
             or re.search(r"\breturnCode\b.{0,35}[`\"']1[`\"'].{0,45}\b(?:is\s+)?success\b", success_text, re.I)
             or re.search(r"\bHTTP\s*200\b.{0,45}\breturnCode\b.{0,35}[`\"']1[`\"']", success_text, re.I))):
        checks.append((('result', 'returnCode'), '1'))
    # A write can require a second response marker in addition to the global
    # envelope code. Keep these checks conjunctive; HTTP 200 alone is insufficient.
    if (('data', 'responseCode') in _schema_paths(response_schema) and
            re.search(r"\bresponseCode\b\s*(?:[`\"']?0[`\"']?|(?:equal\s+to|=|is)\s*[`\"']?0[`\"']?)", rule, re.I)):
        checks.append((('data', 'responseCode'), 0))
    exit_value = _required_exit_state(rule)
    if ('data', 'exitState') in _schema_paths(response_schema) and exit_value:
        checks.append((('data', 'exitState'), exit_value))
    unique = dict(checks)
    if operation.role in {"read", "write"}:
        global_code = re.search(r"\bsuccess\s*=\s*HTTP\s*200\b.{0,80}\breturnCode\b.{0,30}[`\"']1[`\"']",
                                source, re.I | re.S)
        scoped_code = re.search(r"\breturnCode\b\s*(?:=|is|equal\s+to|[\"'`])\s*[`\"']?1\b", rule, re.I)
        if (global_code or scoped_code) and unique.get(('result', 'returnCode')) != '1':
            raise ValueError(f"{operation.name}: source requires result.returnCode '1' but response schema cannot prove it")
        if exit_value and unique.get(('data', 'exitState')) != exit_value:
            raise ValueError(f"{operation.name}: source requires data.exitState {exit_value!r} but response schema cannot prove it")
        response_zero = re.search(r"\bresponseCode\b\s*(?:[`\"']?0[`\"']?|(?:equal\s+to|=|is)\s*[`\"']?0[`\"']?)", rule, re.I)
        if response_zero and unique.get(('data', 'responseCode')) != 0:
            raise ValueError(f"{operation.name}: source requires data.responseCode 0 but response schema cannot prove it")
    return sorted(unique.items())


def _call_variable_for(name: str, variables: set[str]) -> str | None:
    lowered = name.lower().replace("_", "")
    exact = [value for value in variables if value.lower().replace("_", "") == lowered]
    if len(exact) == 1:
        return exact[0]
    if lowered.startswith("customer"):
        aliases = [value for value in variables if value.lower().replace("_", "")
                   in {"customerid", "customerno", "customernumber", "customeridentifier"}]
        if len(aliases) == 1:
            return aliases[0]
    return None


def _body_property(name: str, spec: dict, call_variables: set[str],
                   fixed: dict, *, location: str) -> dict:
    if not isinstance(spec, dict):
        raise ValueError(f"{location} request field {name!r} is invalid")
    kind = spec.get("type", "string")
    if kind == "object":
        properties = spec.get("properties") or {}
        if not isinstance(properties, dict):
            raise ValueError(f"{location} request object {name!r} has invalid properties")
        return {"type": "object", "properties": {
            key: _body_property(key, child, call_variables, fixed, location=location + "." + name)
            for key, child in properties.items()}, "required": spec.get("required") or []}
    leaf = {"type": kind, "description": spec.get("description") or f"{name} for {location}."}
    if "enum" in spec:
        leaf["enum"] = spec["enum"]
    if name in fixed:
        value = fixed[name]
        if ("enum" in spec and value not in spec["enum"] and
                not re.fullmatch(r"\{\{[A-Za-z_][A-Za-z0-9_]*\}\}", str(value))):
            raise ValueError(f"{location} fixed value for {name!r} is outside OpenAPI enum")
        leaf["default"] = value
    elif "const" in spec:
        leaf["default"] = spec["const"]
    elif "default" in spec:
        leaf["default"] = spec["default"]
    elif isinstance(spec.get("enum"), list) and len(spec["enum"]) == 1:
        leaf["default"] = spec["enum"][0]
    elif variable := _call_variable_for(name, call_variables):
        leaf["default"] = "{{" + variable + "}}"
    elif re.fullmatch(r"(?:customer|client|party|person)_?id", name, re.I):
        leaf["default"] = "{{customer_id}}"
    return leaf


def _schema_paths(schema: dict, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Return JSON leaf paths, using * for array members."""
    if not isinstance(schema, dict):
        return []
    if schema.get("type") == "array":
        return _schema_paths(schema.get("items") or {}, (*path, "*"))
    properties = schema.get("properties") or {}
    if isinstance(properties, dict) and properties:
        return [leaf for name, child in properties.items()
                for leaf in _schema_paths(child, (*path, name))]
    return [path] if path else []


def _json_schema(operation: _Operation, *, response: bool) -> dict:
    if not response:
        return (((operation.source.get("requestBody") or {}).get("content") or {})
                .get("application/json") or {}).get("schema") or {}
    for status, item in (operation.source.get("responses") or {}).items():
        if str(status).startswith("2"):
            return (((item.get("content") or {}).get("application/json") or {})
                    .get("schema") or {})
    return {}


def _unique_field_path(operation: _Operation, field: str, *, response: bool) -> tuple[str, ...] | None:
    paths = [path for path in _schema_paths(_json_schema(operation, response=response))
             if path[-1] == field]
    return paths[0] if len(paths) == 1 else None


def _schema_at_path(schema: dict, path: tuple[str, ...]) -> dict:
    current = schema
    for part in path:
        current = (current.get("items") if part == "*" else
                   (current.get("properties") or {}).get(part)) if isinstance(current, dict) else None
    return current if isinstance(current, dict) else {}


def _explicit_fixed_fields(schema: dict, rule: str) -> dict[str, str]:
    """Bind literal data assignments stated in the operation's POST instruction."""
    paths = _schema_paths(schema)
    fixed: dict[str, str] = {}
    for path in paths:
        if len(path) != 2 or path[0] != "data" or sum(p[-1] == path[-1] for p in paths) != 1:
            continue
        name = path[-1]
        field = _schema_at_path(schema, path)
        enum = field.get("enum") if isinstance(field.get("enum"), list) else []
        pattern = r"\bdata\." + re.escape(name) + r"\s*=\s*([`\"'])([^`\"']+)\1"
        values = []
        for match in re.finditer(pattern, rule, re.I):
            prior = rule[max(0, match.start() - 300):match.start()]
            post = prior.rfind("POST")
            if (not (enum and match.group(2) in enum) and (post < 0 or ";" in prior[post:])) or re.search(r"\b(?:do not|never)\b.{0,40}$", prior, re.I):
                continue
            values.append(match.group(2))
        if enum:
            plain = r"\bdata\." + re.escape(name) + r"\s*=\s*([A-Za-z0-9_-]+)\b"
            for match in re.finditer(plain, rule, re.I):
                prior = rule[max(0, match.start() - 45):match.start()]
                if match.group(1) in enum and not re.search(r"\b(?:do not|never)\b.{0,40}$", prior, re.I):
                    values.append(match.group(1))
        if len(set(values)) != 1 or not values:
            continue
        value = values[0]
        if field.get("type") not in (None, "string") or (field.get("enum") and value not in field["enum"]):
            raise ValueError(f"Fixed request value for {name!r} contradicts OpenAPI")
        fixed[name] = value
    return fixed


def _provenance_plan(operation: _Operation, operations: list[_Operation], rule: str,
                     call_variables: set[str]) -> list[dict]:
    """Derive exact request bindings from explicit source rules and OpenAPI shapes."""
    request_paths = [path for path in _schema_paths(_json_schema(operation, response=False))
                     if "*" not in path]
    data_paths = [path for path in request_paths if path[0] == "data"]
    reads = [op for op in operations if op.role == "read" and op.name != operation.name]
    plan: list[dict] = []

    for path in request_paths:
        variable = _call_variable_for(path[-1], call_variables)
        if variable:
            plan.append({"kind": "call_variable", "field": path, "variable": variable})

    for path in data_paths:
        field = path[-1]
        listed = re.search(r"\b(?:listed|list-returned)\s+" +
                           re.escape(field) + r"\b|\b" + re.escape(field) +
                           r"\b.{0,12}\b(?:listed|list-returned)\b", rule, re.I)
        if not listed and re.search(r"(?:number|identifier|key)$", field, re.I):
            listed = re.search(r"\bdata\." + re.escape(field) +
                               r"\s*=.{0,30}\blisted\s+(?:number|identifier|key)\b", rule, re.I)
        candidates = [(op, source_path) for op in reads
                      if (source_path := _unique_field_path(op, field, response=True))
                      and "*" in source_path and (op.name in rule or
                          listed)]
        if len(candidates) == 1:
            op, source_path = candidates[0]
            plan.append({"kind": "response_member", "field": path,
                         "source": op.name, "source_path": source_path})
        elif listed or any(op.name in rule and _unique_field_path(op, field, response=True)
                           and "*" in _unique_field_path(op, field, response=True)
                           for op in reads):
            raise ValueError(f"{operation.name}: cannot prove listed {field} provenance")

    assignments = list(re.finditer(r"\bdata\.([A-Za-z_][A-Za-z0-9_]*)\s*=", rule))
    fixed_assignments = _explicit_fixed_fields(_json_schema(operation, response=False), rule)
    for match in assignments:
        if match.group(1) in fixed_assignments:
            continue
        targets = [path for path in data_paths if path[-1] == match.group(1)]
        if len(targets) != 1:
            continue
        # The next assignment or sentence ends this source clause; operation IDs
        # contain periods, so a period alone is not a sentence boundary.
        tail = rule[match.end():match.end() + 220]
        tail = re.split(r",\s*(?:and\s+)?data\.|[.;]\s+[A-Z]", tail, maxsplit=1)[0]
        source_ops = [op for op in reads if op.name in tail]
        field_tokens = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", tail))
        matches = []
        for op in source_ops or reads:
            for token in field_tokens | {match.group(1)}:
                source_path = _unique_field_path(op, token, response=True)
                if source_path and (op.name in tail or token != match.group(1)):
                    matches.append((op, source_path))
        # Prefer the field named by the source; otherwise the request field.
        exact = [(op, source_path) for op, source_path in matches
                 if source_path[-1] != match.group(1)]
        if len(exact) == 1:
            matches = exact
        elif len(exact) > 1:
            matches = exact
        else:
            matches = [(op, source_path) for op, source_path in matches
                       if source_path[-1] == match.group(1)]
        matches = list({(op.name, source_path): (op, source_path)
                        for op, source_path in matches}.values())
        if len(matches) == 1 and "*" not in matches[0][1]:
            op, source_path = matches[0]
            scope = [(target, source) for target in data_paths
                     if target != targets[0] and (source := _unique_field_path(op, target[-1], response=False))
                     and source[0] == "data"]
            plan.append({"kind": "response_equal", "field": targets[0],
                         "source": op.name, "source_path": source_path, "scope": scope})
        if (source_ops or re.search(r"\b(?:supplied|registered|current|listed)\b", tail, re.I)):
            covered = any(item.get("field") == targets[0] for item in plan)
            if not covered:
                raise ValueError(f"{operation.name}: cannot prove source binding for {match.group(1)}")

    if re.search(r"\bdifferent\b", rule, re.I):
        for path in data_paths:
            if not path[-1].startswith("new") or len(path[-1]) <= 3:
                continue
            previous_field = path[-1][3].lower() + path[-1][4:]
            candidates = [(op, source_path) for op in reads if op.name in rule
                          if (source_path := _unique_field_path(op, previous_field, response=True))]
            if len(candidates) == 1:
                op, source_path = candidates[0]
                scope = [(target, source) for target in data_paths
                         if target != path and (source := _unique_field_path(op, target[-1], response=False))
                         and source[0] == "data"]
                plan.append({"kind": "response_not_equal", "field": path,
                             "source": op.name, "source_path": source_path, "scope": scope})

    if (re.search(r"multi[- ]currency", rule, re.I) and
            re.search(r"choice\s+is\s+established|do\s+not\s+guess|choice\s+is\s+unresolved", rule, re.I)):
        for path in data_paths:
            field_schema = _schema_at_path(_json_schema(operation, response=False), path)
            values = field_schema.get("enum") or []
            if not isinstance(values, list) or len(values) < 2:
                continue
            candidates = []
            for op in reads:
                if op.name not in rule:
                    continue
                leaves = _schema_paths(_json_schema(op, response=True))
                nodes = {leaf[:index] for leaf in leaves for index in range(1, len(leaf) + 1)}
                pairs = [(value, node) for value in values for node in nodes
                         if isinstance(value, str) and node[0] == "data"
                         and node[-1].lower().endswith(value.lower())]
                if len(pairs) == len(values) and len({value for value, _ in pairs}) == len(values):
                    scope = [(target, source) for target in data_paths
                             if target != path and (source := _unique_field_path(op, target[-1], response=False))
                             and source[0] == "data"]
                    candidates.append((op, pairs, scope))
            if len(candidates) == 1:
                op, pairs, scope = candidates[0]
                plan.append({"kind": "single_enum_block", "field": path,
                             "source": op.name, "pairs": pairs, "scope": scope})
            elif field_schema.get("enum") and re.search(r"currency|type", path[-1], re.I):
                raise ValueError(f"{operation.name}: cannot prove source choice for {path[-1]}")

    if operation.role == "write" and re.search(r"(?:successful|approved)\s+preview|preview", rule, re.I):
        candidates = []
        for op in reads:
            detail = " ".join((op.name, str(op.source.get("summary") or ""),
                               str(op.source.get("description") or "")))
            if not re.search(r"preview|decide|estimate|quote", detail, re.I):
                continue
            shared = [(path, source) for path in data_paths
                      if (source := _unique_field_path(op, path[-1], response=False))
                      and source[0] == "data"]
            if len(shared) >= 2:
                candidates.append((op, shared))
        if len(candidates) != 1:
            raise ValueError(f"{operation.name}: cannot prove unique successful preview provenance")
        preview, shared = candidates[0]
        plan.append({"kind": "request_match", "source": preview.name, "fields": shared})
        date_paths = [path for path in _schema_paths(_json_schema(preview, response=True))
                      if re.search(r"date$", path[-1], re.I) and path[0] == "data"]
        if re.search(r"previewed\s+dates?|preview\s+statementDate", rule, re.I):
            if len(date_paths) < 2:
                raise ValueError(f"{operation.name}: preview dates cannot be proved")
            plan.append({"kind": "response_present", "source": preview.name,
                         "paths": date_paths})

    return plan


def _pre_provenance_code(operation: _Operation, plan: list[dict]) -> str:
    return (
        "import json\n"
        "def reject(reason):\n"
        "    config['url'] = ''\n"
        "    config['body'] = {}\n"
        "    raise ValueError(reason)\n"
        "def values(value, path):\n"
        "    found = [value]\n"
        "    for part in path:\n"
        "        found = ([member for item in found if isinstance(item, list) for member in item] "
        "if part == '*' else [item[part] for item in found if isinstance(item, dict) and part in item])\n"
        "    return found\n"
        "try:\n"
        "    evidence = json.loads(str(vars.get('generic_read_evidence') or '{}'))\n"
        "except (ValueError, TypeError):\n"
        "    evidence = {}\n"
        f"for item in {plan!r}:\n"
        "    actual = values(config.get('body'), item['field']) if 'field' in item else []\n"
        "    kind = item['kind']\n"
        "    if kind == 'call_variable':\n"
        "        if len(actual) != 1 or not str(actual[0]) or str(actual[0]) != str(vars.get(item['variable']) or ''):\n"
        "            reject('Request does not match the supplied call identity')\n"
        "        continue\n"
        "    source = evidence.get(item['source'])\n"
        "    if not isinstance(source, dict):\n"
        "        reject('Required successful bank read is unavailable')\n"
        "    if kind in ('response_member', 'response_equal', 'response_not_equal'):\n"
        "        expected = values(source.get('response'), item['source_path'])\n"
        "        if len(actual) != 1 or not str(actual[0]) or not expected:\n"
        "            reject('Write input has no bank provenance')\n"
        "        if kind == 'response_member' and actual[0] not in expected:\n"
        "            reject('Requested item was not returned by the bank list')\n"
        "        if kind == 'response_equal' and (len(expected) != 1 or str(actual[0]) != str(expected[0])):\n"
        "            reject('Write input differs from the bank result')\n"
        "        if kind == 'response_not_equal' and (len(expected) != 1 or str(actual[0]) == str(expected[0])):\n"
        "            reject('Requested value must differ from the current bank value')\n"
        "        if kind in ('response_equal', 'response_not_equal'):\n"
        "            for target_path, source_path in item.get('scope', []):\n"
        "                target = values(config.get('body'), target_path)\n"
        "                prior = values(source.get('request'), source_path)\n"
        "                if len(target) != 1 or len(prior) != 1 or str(target[0]) != str(prior[0]):\n"
        "                    reject('Bank read belongs to another selected item')\n"
        "    elif kind == 'request_match':\n"
        "        for target_path, source_path in item['fields']:\n"
        "            target = values(config.get('body'), target_path)\n"
        "            prior = values(source.get('request'), source_path)\n"
        "            if len(target) != 1 or len(prior) != 1 or str(target[0]) != str(prior[0]):\n"
        "                reject('Write differs from the successful preview request')\n"
        "    elif kind == 'response_present':\n"
        "        if any(not any(str(value) for value in values(source.get('response'), path)) for path in item['paths']):\n"
        "            reject('Successful preview is missing required details')\n"
        "    elif kind == 'single_enum_block':\n"
        "        available = [code for code, path in item['pairs'] if any(value is not None for value in values(source.get('response'), path))]\n"
        "        if len(actual) != 1 or len(available) != 1 or str(actual[0]) != str(available[0]):\n"
        "            reject('Requested choice is not uniquely established by the bank')\n"
        "        for target_path, source_path in item.get('scope', []):\n"
        "            target = values(config.get('body'), target_path)\n"
        "            prior = values(source.get('request'), source_path)\n"
        "            if len(target) != 1 or len(prior) != 1 or str(target[0]) != str(prior[0]):\n"
        "                reject('Bank choice belongs to another selected item')\n"
        + (f"evidence.pop({operation.name!r}, None)\n"
           "vars['generic_read_evidence'] = json.dumps(evidence)\n"
           f"vars[{('generic_pending_' + operation.name)!r}] = json.dumps(config.get('body') or {{}})\n"
           if operation.role == "read" else "")
    )


def _normalize_request_code(operation: _Operation, rule: str) -> str:
    """Accept Freya's flat tool arguments only when the contract has one data envelope."""
    schema = _json_schema(operation, response=False)
    top = schema.get("properties") or {}
    data = top.get("data") if isinstance(top, dict) else None
    if not isinstance(data, dict) or data.get("type") != "object":
        return ""
    data_fields = data.get("properties") or {}
    if not isinstance(data_fields, dict):
        return ""
    fixed = _explicit_fixed_fields(schema, rule)
    for name, field in data_fields.items():
        if not isinstance(field, dict):
            continue
        if "const" in field:
            fixed[name] = field["const"]
        elif "default" in field:
            fixed[name] = field["default"]
        elif isinstance(field.get("enum"), list) and len(field["enum"]) == 1:
            fixed[name] = field["enum"][0]
    return (
        "def reject_request_shape(reason):\n"
        "    config['url'] = ''\n"
        "    config['body'] = {}\n"
        "    raise ValueError(reason)\n"
        "body = config.get('body')\n"
        "if not isinstance(body, dict):\n"
        "    body = {}\n"
        f"data_fields = {tuple(sorted(data_fields))!r}\n"
        f"top_fields = {tuple(sorted(top))!r}\n"
        f"args_prefix = {operation.name!r} + '_args.body.'\n"
        "for key in data_fields:\n"
        "    if key in body or (isinstance(body.get('data'), dict) and key in body['data']):\n"
        "        continue\n"
        "    flat_key = args_prefix + key\n"
        "    nested_key = args_prefix + 'data.' + key\n"
        "    if flat_key in vars and nested_key in vars and vars[flat_key] != vars[nested_key]:\n"
        "        reject_request_shape('Conflicting model request fields')\n"
        "    if nested_key in vars or flat_key in vars:\n"
        "        body[key] = vars[nested_key] if nested_key in vars else vars[flat_key]\n"
        "if not body:\n"
        "    reject_request_shape('Bank request body is unavailable')\n"
        "flat = {key: value for key, value in body.items() if key not in top_fields}\n"
        "if any(key not in data_fields for key in flat):\n"
        "    reject_request_shape('Request field is absent from the bank contract')\n"
        "nested = body.get('data', {})\n"
        "if not isinstance(nested, dict):\n"
        "    reject_request_shape('Bank data envelope must be an object')\n"
        "if any(key in nested and nested[key] != value for key, value in flat.items()):\n"
        "    reject_request_shape('Conflicting bank request fields')\n"
        "nested = {**nested, **flat}\n"
        "if any(key not in data_fields for key in nested):\n"
        "    reject_request_shape('Request field is absent from the bank contract')\n"
        f"for key, value in {fixed!r}.items():\n"
        "    if key in nested and nested[key] != value:\n"
        "        reject_request_shape('Fixed bank request value differs from the contract')\n"
        "    nested[key] = value\n"
        "config['body'] = {key: value for key, value in body.items() if key in top_fields and key != 'data'}\n"
        "config['body']['data'] = nested\n"
    )


def _post_read_evidence_code(operation: _Operation,
                             checks: list[tuple[tuple[str, ...], object]],
                             prereqs_map: dict[str, list[str]] | None = None) -> str:
    prereq_checks_code = ""
    if prereqs_map:
        for write_name, required_reads in sorted(prereqs_map.items()):
            if operation.name in required_reads:
                req_list = repr(sorted(required_reads))
                prereq_checks_code += (
                    f"        required = {req_list}\n"
                    f"        if all(k in evidence for k in required):\n"
                    f"            vars['generic_action_prereq_ready'] = 'yes'\n"
                    f"            vars[{('generic_prereq_ready_' + write_name)!r}] = 'yes'\n"
                )
    return (
        "import json\n"
        "try:\n"
        "    payload = result.get('data') if isinstance(result, dict) else None\n"
        "    def field(path):\n"
        "        value = payload\n"
        "        for part in path:\n"
        "            value = value.get(part) if isinstance(value, dict) else None\n"
        "        return value\n"
        f"    checks = {checks!r}\n"
        "    ok = (isinstance(payload, dict) and str(result.get('status_code') or '').startswith('2') "
        "and bool(checks) and all(field(path) == expected for path, expected in checks))\n"
        "    evidence = json.loads(str(vars.get('generic_read_evidence') or '{}'))\n"
        f"    pending = json.loads(str(vars.pop({('generic_pending_' + operation.name)!r}, '') or 'null'))\n"
        "    if ok and isinstance(pending, dict):\n"
        f"        evidence[{operation.name!r}] = {{'request': pending, 'response': payload}}\n"
        + prereq_checks_code +
        "    vars['generic_read_evidence'] = json.dumps(evidence)\n"
        "except (ValueError, TypeError, AttributeError):\n"
        "    pass\n"
    )


def _tool(openapi: dict, operation: _Operation, rule: str, result_field: str | None = None,
          fixed: dict | None = None, call_variables: set[str] | None = None,
          result_checks: list[tuple[tuple[str, ...], object]] | None = None) -> dict:
    url = operation_url(openapi, operation.path, operation.method)
    headers = {"Content-Type": {"type": "string", "default": "application/json"}}
    headers.update(required_header_bindings(openapi, operation.path, operation.method))
    schema = (((operation.source.get("requestBody") or {}).get("content") or {})
              .get("application/json") or {}).get("schema") or {}
    if schema and schema.get("type", "object") != "object":
        raise ValueError(f"{operation.name} request must be a JSON object")
    fixed_values = _explicit_fixed_fields(schema, rule)
    fixed_values.update(fixed or {})
    body_schema = schema
    outer = schema.get("properties") or {}
    data_schema = outer.get("data") if isinstance(outer, dict) else None
    if (isinstance(data_schema, dict) and data_schema.get("type") == "object" and
            set(outer) <= {"data", "call"} and isinstance(data_schema.get("properties"), dict)):
        # Freya exposes model-filled body arguments only from direct body leaves.
        # The pre-hook restores the bank's nested data envelope before HTTP.
        body_schema = data_schema
    body_properties = {}
    for name, spec in (body_schema.get("properties") or {}).items():
        leaf = _body_property(name, spec, call_variables or set(), fixed_values, location=operation.name)
        if body_schema is not schema and "default" not in leaf and leaf.get("type") != "object":
            leaf["default"] = "{{" + name + "}}"
        body_properties[name] = leaf
    result = {
        "type": "function", "config": {"timeoutMs": 20000, "maxRetries": 0},
        "function": {"name": operation.name, "handler": "__function__:api_call",
                     "description": rule or operation.source.get("description") or operation.source.get("summary")
                     or f"Call {operation.name} only under the source policy conditions.",
                     "parameters": {"type": "object", "properties": {
                         "url": {"type": "string", "default": url},
                         "method": {"type": "string", "default": operation.method},
                         "headers": {"type": "object", "properties": headers},
                         "body": {"type": "object", "properties": body_properties,
                                  "required": body_schema.get("required") or []},
                     }}},
    }
    checks = result_checks or ([( (result_field,), True)] if result_field else [])
    if checks and operation.role == "write":
        check_source = (
            "    checks = " + repr(checks) + "\n"
            "    def field(path):\n"
            "        value = data\n"
            "        for part in path:\n"
            "            value = value.get(part) if isinstance(value, dict) else None\n"
            "        return value\n"
            "    ok = str(result.get('status_code') or '').startswith('2') and all(field(path) == expected for path, expected in checks)\n"
        )
        result["post_process_code"] = (
            "try:\n"
            "    result.pop('header', None)\n"
            "    data = result.get('data') if isinstance(result, dict) else None\n"
            "    if not isinstance(data, dict):\n"
            "        data = {}\n"
            "        result['data'] = data\n"
            + check_source +
            "    vars['generic_write_result'] = 'confirmed' if ok else 'unknown'\n"
            f"    vars['generic_recorded_action'] = {operation.name!r} if ok else ''\n"
            "    vars['explicit_action_confirmation'] = 'unknown'\n"
            "    vars['confirmed_write_action'] = ''\n"
            "    vars['generic_action_prereq_ready'] = 'no'\n"
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
    if operation.role == "write":
        result["pre_process_code"] = (
            "if (str(vars.get('explicit_action_confirmation') or '').lower() != 'yes' or\n"
            f"        vars.get('confirmed_write_action') != {operation.name!r}):\n"
            "    config['url'] = ''\n"
            "    config['body'] = {}\n"
            "    raise ValueError('Bank write requires confirmation of this exact action')\n"
            "if vars.get('generic_write_attempted_action'):\n"
            "    config['url'] = ''\n"
            "    config['body'] = {}\n"
            "    raise ValueError('Bank write was already attempted in this call')\n"
            f"vars['generic_write_attempted_action'] = {operation.name!r}\n"
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


def _and_when(conditions: list[tuple[str, str]], target: str, description: str) -> dict:
    return {
        "target_node": target,
        "description": description,
        "condition": {
            "logic": "or",
            "conditions": [
                {
                    "logic": "and",
                    "conditions": [
                        {"variable": var, "operator": "equals", "operand": val}
                        for var, val in conditions
                    ]
                }
            ]
        }
    }


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


def _end_node(spoken_message: str) -> dict:
    return {"name": "ended", "node_type": "end_call", "on_enter": {"type": "silent"},
            "disable_inactivity_monitor": True, "transitions": [],
            "terminal": {"type": "function", "function": {
                "name": "freya_call_complete", "handler": "__function__:end_call",
                "parameters": {"type": "object", "properties": {
                    "message_mode": {"type": "string", "default": "custom"},
                    "spoken_message": {"type": "string", "default": spoken_message}}}}}}


def _normal_completion_utterance(policy: dict) -> str:
    """Use the source's normal return phrase when it explicitly supplies one."""
    outcomes = policy.get("outcomes") or {}
    menu = outcomes.get("main_menu") if isinstance(outcomes, dict) else None
    if isinstance(menu, str):
        for pattern in (
            r"\bnormal\s+(?:completion|goodbye|end)\s*:\s*[“\"]([^”\"]+)[”\"]",
            r"\b(?:normal\s+(?:goodbye|end)|nothing\s+else|thanks)\b[^:]{0,150}:\s*[“\"]([^”\"]+)[”\"]",
            r"\b(?:caller\s+finishes|says\s+thanks|nothing\s+else|normal\s+(?:completion|goodbye|end))\b[^“”\"]{0,100}[“\"]([^”\"]+)[”\"]",
        ):
            match = re.search(pattern, menu, re.I | re.S)
            if match:
                return match.group(1).strip()
    return "Thank you. Goodbye."


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
    success_checks = {}
    for operation in operations:
        terms = (r"verif|identit|authenticat|complete|passed" if operation.role == "verify"
                 else r"\bok\b|success|saved|recorded|created|completed|closed|transferred|scheduled")
        proof[operation.name] = (_identity_completion_field(operation) if operation.role == "verify"
                                 else _boolean_result(operation, terms))
        success_checks[operation.name] = _result_checks(operation,
            str(rules.get(operation.name) or ""), str(policy.get("raw_brief") or ""))
        try:
            tools[operation.name] = _tool(openapi, operation, str(rules.get(operation.name) or ""),
                                          proof[operation.name], call_variables=call_variables,
                                          result_checks=success_checks[operation.name])
        except ValueError:
            # The API transport cannot safely bind some OpenAPI parameters.
            # Omission keeps the workflow runnable without fabricating values.
            continue
    write_prereqs: dict[str, list[str]] = {}
    for operation in operations:
        if operation.name in tools and operation.role == "write":
            try:
                w_plan = _provenance_plan(operation, operations,
                                          str(rules.get(operation.name) or ""), call_variables)
                w_sources = sorted({step["source"] for step in w_plan if "source" in step})
                if w_sources:
                    write_prereqs[operation.name] = w_sources
            except Exception:
                pass
    for operation in operations:
        if operation.name not in tools or operation.role not in {"read", "write"}:
            continue
        plan = _provenance_plan(operation, operations,
                                str(rules.get(operation.name) or ""), call_variables)
        tool = tools[operation.name]
        tool["pre_process_code"] = (_normalize_request_code(operation, str(rules.get(operation.name) or ""))
                                    + _pre_provenance_code(operation, plan)
                                    + tool.get("pre_process_code", ""))
        if operation.role == "read":
            tool["post_process_code"] = (tool.get("post_process_code", "")
                                         + _post_read_evidence_code(operation,
                                                                    success_checks[operation.name],
                                                                    write_prereqs))
    verify = [op for op in operations if op.role == "verify" and op.name in tools and proof[op.name]]
    upstream_verified = len(verify) == 0 and _upstream_identity_confirmed(source)
    if len(verify) != 1 and not upstream_verified and any(op.role in {"verify", "read", "write"} for op in operations):
        raise ValueError("Cannot identify exactly one callable identity operation with an unambiguous "
                         "bank-confirmed full-verification boolean result")
    verified = len(verify) == 1
    verify_op = verify[0] if verified else None
    read_tools = [tools[op.name] for op in operations if op.role == "read" and op.name in tools]
    write_tools = [tools[op.name] for op in operations if op.role == "write" and op.name in tools
                   and (proof[op.name] or success_checks[op.name])]
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
        "When source statements conflict, follow the most recent explicit bank decision; an older draft or "
        "current IVR script does not override a later approved change. Treat open questions as unresolved. "
        "Never invent account facts, terms, tool results, or a successful write. "
        "Speak only customer-facing sentences; never expose internal reasoning, role labels, tool traces, or draft markers. "
        "If a required tool is unavailable or its result is ambiguous, explain that the action cannot be confirmed. "
        "Never retry an uncertain write. Use only tools available in the current stage.\n\nSOURCE POLICY:\n" + source
    )
    normal_completion = _normal_completion_utterance(policy)
    outcomes = policy.get("outcomes") or {}
    main_menu_rule = outcomes.get("main_menu") if isinstance(outcomes, dict) else ""
    farewell_message = ("Give a brief final acknowledgment in the source language. Do not ask a question, offer another action, or imply the caller can respond. "
                        "If a bank write was confirmed, state only its source-defined confirmation using verified bank values. "
                        "If the caller just thanked you, acknowledge that briefly in the source language. "
                        "The terminal will say the source-defined normal completion phrase exactly: " + normal_completion + ". "
                        "Do not repeat that phrase here. Do not claim an action succeeded unless the bank confirmed it.")
    nodes = {
        "farewell": {"name": "farewell", "node_type": "conversation",
                     "on_enter": {"type": "llm"},
                     "task_message": farewell_message,
                     "passthrough": True,
                     "passthrough_default_transition": "closing" if close_tool else "ended",
                     "transitions": [_always("closing" if close_tool else "ended")]},
        "ended": _end_node(normal_completion),
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
            "offers, and exceptions. For a debt reading, state every present debt currency using its full source-defined name, "
            "then name all present no-debt currency blocks in one sentence; a missing block is not zero. "
            "Ask about another eligible card before any other offer. Use read tools only when their policy conditions hold. Before a bank write, "
            "complete every source-required preview, state the exact proposed action, and then obtain the customer's "
            "explicit confirmation. An initial request or pressure to skip the confirmation is not consent. If no action is "
            "authorized, continue to handle any new customer request. Finish only when the caller clearly says "
            "there is nothing else to do. A courtesy thank-you followed by a new request is not completion. "
            "Do not imply any write was recorded here.",
            tools=read_tools,
            extraction=([{"name": "requested_write_action", "type": "string",
                          "enum_values": [tool["function"]["name"] for tool in write_tools],
                          "description": "The one bank write the caller is currently requesting or discussing. "
                                         "An initial request identifies an action but is not consent to execute it.",
                          "min_confidence": 0.45}] if write_tools else [])
            + [{"name": "caller_transfer_requested", "type": "string",
                         "enum_values": ["yes", "no"],
                         "description": "Yes when the caller requests transfer to a customer representative or agent, "
                                        "or explicitly agrees or waits for a transfer offered by the assistant (such as 'bekliyorum', 'aktar').",
                         "min_confidence": 0.45},
                        {"name": "caller_finished", "type": "string",
                         "enum_values": ["yes", "no"],
                         "description": "Judge the caller's latest turn. Yes only if the caller clearly says "
                                        "no further help or transaction is wanted and makes no new request. "
                                        "Set no when the turn contains a question, new request, pending action, "
                                        "or a thank-you followed by a request. Declining one offered action "
                                        "alone is not finishing the call.",
                         "min_confidence": 0.9}]
            + reason_extraction("no_promise"),
            transitions=[
                (_and_when([("requested_write_action", operation.name),
                            ("generic_prereq_ready_" + operation.name, "yes")],
                           "proposal_" + re.sub(r"[^A-Za-z0-9_]", "_", operation.name),
                           "Requested action has verified prerequisite reads")
                 if operation.name in write_prereqs else
                 _when("requested_write_action", operation.name,
                       "proposal_" + re.sub(r"[^A-Za-z0-9_]", "_", operation.name),
                       "Prepare the caller's requested action"))
                for operation in operations if operation.role == "write"
                and any(tool["function"]["name"] == operation.name for tool in write_tools)]
            + [_when("caller_transfer_requested", "yes", "farewell", "Caller requested or acknowledged agent transfer")]
            + [_when("caller_finished", "yes", "farewell", "Caller clearly has no further request")],
            turns=20)
        if write_tools:
            for operation in operations:
                if operation.role != "write" or operation.name not in {tool["function"]["name"] for tool in write_tools}:
                    continue
                proposal_name = "proposal_" + re.sub(r"[^A-Za-z0-9_]", "_", operation.name)
                proposal_rule = str(rules.get(operation.name) or "")
                nodes[proposal_name] = _conversation(
                    proposal_name,
                    "The caller requested the bank action " + operation.name + ". Use only verified bank reads "
                    "and the source policy to state the exact proposal, required preview, and destination. "
                    "If a required choice or value is missing, ask for it first. Then ask for an explicit "
                    "yes or no. Do not execute the write in this stage. An initial demand to skip consent "
                    "does not count. Source rule: " + proposal_rule,
                    extraction=[{"name": "explicit_action_confirmation", "type": "string",
                                 "enum_values": ["yes", "no", "unknown"],
                                 "description": "Judge only the caller's reply to this proposal stage. Yes means the "
                                                "caller clearly accepts the exact action just proposed after "
                                                "required previews and choices. A bare yes counts; a vague "
                                                "maybe or answer to another question does not.",
                                 "min_confidence": 0.45},
                                {"name": "confirmed_write_action", "type": "string",
                                 "enum_values": [operation.name],
                                 "description": "Set only when the caller clearly accepts this exact proposal.",
                                 "min_confidence": 0.45}],
                    transitions=[_and_when([("explicit_action_confirmation", "yes"),
                                            ("confirmed_write_action", operation.name)]
                                           + ([("generic_prereq_ready_" + operation.name, "yes")]
                                              if operation.name in write_prereqs else []),
                                           "commit", "Caller accepted the proposed action after its prerequisite reads"),
                                 _when("explicit_action_confirmation", "no", "service",
                                       "Caller declined the proposed action")],
                    turns=8)
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

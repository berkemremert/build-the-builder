"""Smoke-test a deployed bank mock against a supplied OpenAPI contract.

The case file is JSON with ``variables`` and ordered ``scenarios``. Each step
names an OpenAPI ``operationId`` (or ``method`` and ``path``), supplies a JSON
``request``, and may set ``expect_status`` and ``expect`` dotted response paths.
``{{variable}}`` and ``{{steps.0.body.field}}`` references are resolved inside
request values. Every contract operation needs a successful scenario step.

No request, response, seed value, URL, or exception detail is printed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from openapi_resolver import normalize_openapi  # noqa: E402
from api_servers import operation_url  # noqa: E402


METHODS = frozenset({"get", "post", "put", "patch", "delete"})
REFERENCE = re.compile(r"^\{\{([A-Za-z0-9_.-]+)\}\}$")


class SmokeError(Exception):
    """A safe-to-print validation failure without seed or response values."""


@dataclass(frozen=True)
class Operation:
    path: str
    method: str
    operation_id: str
    spec: dict


def _load_json(path: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SmokeError("cannot read JSON input") from exc
    if not isinstance(value, dict):
        raise SmokeError("JSON input must be an object")
    return value


def _operations(contract: dict) -> dict[tuple[str, str], Operation]:
    found = {}
    for path, item in contract["paths"].items():
        if not isinstance(path, str) or not isinstance(item, dict):
            continue
        for method, spec in item.items():
            if method.lower() not in METHODS or not isinstance(spec, dict):
                continue
            key = (method.lower(), path)
            found[key] = Operation(path, method.lower(),
                                   str(spec.get("operationId") or path.rstrip("/").split("/")[-1]), spec)
    if not found:
        raise SmokeError("contract has no HTTP operations")
    return found


def _lookup(context: dict, dotted: str) -> Any:
    value: Any = context
    for part in dotted.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdecimal() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise SmokeError("case references an unavailable value")
    return value


def _resolve(value: Any, context: dict) -> Any:
    if isinstance(value, str):
        match = REFERENCE.fullmatch(value)
        return _lookup(context, match.group(1)) if match else value
    if isinstance(value, list):
        return [_resolve(item, context) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, context) for key, item in value.items()}
    return value


def _schema_error(value: Any, schema: dict, where: str = "$") -> str | None:
    if not isinstance(schema, dict):
        return f"{where}: invalid contract schema"
    if value is None and schema.get("nullable"):
        return None
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool,
             "integer": int, "number": (int, float), "null": type(None)}
    if kind is not None:
        if kind not in types:
            return f"{where}: unsupported contract type"
        if not isinstance(value, types[kind]) or (kind in ("integer", "number") and isinstance(value, bool)):
            return f"{where}: expected {kind}"
    if "const" in schema and value != schema["const"]:
        return f"{where}: constant mismatch"
    if "enum" in schema and value not in schema["enum"]:
        return f"{where}: enum mismatch"
    if isinstance(value, dict):
        required = schema.get("required") or []
        if not isinstance(required, list):
            return f"{where}: invalid required list"
        for field in required:
            if field not in value:
                return f"{where}.{field}: missing required field"
        properties = schema.get("properties") or {}
        if not isinstance(properties, dict):
            return f"{where}: invalid properties"
        for field, item in value.items():
            child = properties.get(field)
            if child is None:
                extra = schema.get("additionalProperties", True)
                if extra is False:
                    return f"{where}.{field}: unexpected field"
                child = extra if isinstance(extra, dict) else None
            if child is not None:
                error = _schema_error(item, child, f"{where}.{field}")
                if error:
                    return error
    elif isinstance(value, list):
        for boundary, name in (("minItems", "too few items"), ("maxItems", "too many items")):
            if boundary in schema and (len(value) < schema[boundary] if boundary == "minItems" else len(value) > schema[boundary]):
                return f"{where}: {name}"
        child = schema.get("items")
        if isinstance(child, dict):
            for index, item in enumerate(value):
                error = _schema_error(item, child, f"{where}[{index}]")
                if error:
                    return error
    elif isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            return f"{where}: string too short"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{where}: string too long"
        if "pattern" in schema:
            try:
                if not re.search(schema["pattern"], value):
                    return f"{where}: pattern mismatch"
            except re.error:
                return f"{where}: invalid contract pattern"
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{where}: below minimum"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{where}: above maximum"
    return None


def _choose_operation(step: dict, operations: dict[tuple[str, str], Operation]) -> Operation:
    operation_id = step.get("operationId") or step.get("operation")
    if operation_id:
        matches = [operation for operation in operations.values() if operation.operation_id == operation_id]
        if len(matches) != 1:
            raise SmokeError("case operationId is missing or ambiguous in contract")
        return matches[0]
    key = (str(step.get("method") or "").lower(), step.get("path"))
    if key not in operations:
        raise SmokeError("case method and path are absent from contract")
    return operations[key]


def _target_url(contract: dict, operation: Operation, base_url: str, path_params: dict) -> str:
    parsed = urlsplit(base_url)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or
            parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise SmokeError("deployed URL must be an absolute HTTP URL without query or fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SmokeError("remote deployed URL must use HTTPS")
    full = urlsplit(operation_url(contract, operation.path, operation.method))
    server_prefix = full.path.removesuffix(operation.path)
    if (parsed.scheme != full.scheme or parsed.netloc != full.netloc or
            parsed.path.rstrip("/") != server_prefix.rstrip("/")):
        raise SmokeError("contract server URL does not match deployed base URL")
    path = operation.path
    for name in re.findall(r"\{([^{}]+)\}", path):
        if name not in path_params:
            raise SmokeError("case lacks a required path parameter")
        path = path.replace("{" + name + "}", quote(str(path_params[name]), safe=""))
    target_path = parsed.path.rstrip("/") + "/" + path.lstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, target_path, "", ""))


def _response_schema(operation: Operation, status: int) -> dict | None:
    responses = operation.spec.get("responses") or {}
    response = responses.get(str(status)) or responses.get("default")
    if not isinstance(response, dict):
        raise SmokeError("response status is absent from contract")
    content = response.get("content") or {}
    if not content:
        return None
    media = content.get("application/json")
    if not isinstance(media, dict) or not isinstance(media.get("schema"), dict):
        raise SmokeError("response lacks a JSON schema")
    return media["schema"]


def _request_schema(operation: Operation) -> tuple[dict | None, bool]:
    request_body = operation.spec.get("requestBody") or {}
    if not isinstance(request_body, dict):
        raise SmokeError("contract request body is invalid")
    content = request_body.get("content") or {}
    if not content:
        return None, bool(request_body.get("required"))
    media = content.get("application/json")
    if not isinstance(media, dict) or not isinstance(media.get("schema"), dict):
        raise SmokeError("request lacks a JSON schema")
    return media["schema"], bool(request_body.get("required"))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _send(url: str, method: str, body: Any, headers: dict, timeout: float, max_bytes: int) -> tuple[int, Any, str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(url, data=data, method=method.upper(), headers={"Accept": "application/json",
                      **({"Content-Type": "application/json"} if data is not None else {}), **headers})
    try:
        response = build_opener(_NoRedirect).open(request, timeout=timeout)
    except HTTPError as exc:
        response = exc
    except (OSError, URLError) as exc:
        raise SmokeError("network request failed") from exc
    with response:
        raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise SmokeError("response exceeds size limit")
        status = response.status
        content_type = response.headers.get("Content-Type", "")
    if not raw:
        return status, None, ""
    if "json" not in content_type.lower():
        raise SmokeError("response is not JSON")
    try:
        return status, json.loads(raw), raw.decode("utf-8")
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SmokeError("response contains invalid JSON") from exc


def run(contract: dict, cases: dict, base_url: str, *, timeout: float = 10,
        max_bytes: int = 1_000_000) -> list[str]:
    """Execute supplied scenarios; return safe messages with no input values."""
    try:
        contract = normalize_openapi(contract)
        operations = _operations(contract)
    except (KeyError, TypeError, ValueError) as exc:
        raise SmokeError("contract is not supported OpenAPI JSON") from exc
    scenarios = cases.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise SmokeError("cases need at least one scenario")
    covered = set()
    planned = []
    for scenario in scenarios:
        if not isinstance(scenario, dict) or not isinstance(scenario.get("steps"), list) or not scenario["steps"]:
            raise SmokeError("every scenario needs ordered steps")
        steps = []
        for step in scenario["steps"]:
            if not isinstance(step, dict):
                raise SmokeError("scenario step must be an object")
            operation = _choose_operation(step, operations)
            expected = step.get("expect_status", 200)
            if isinstance(expected, int):
                expected = [expected]
            if (not isinstance(expected, list) or not expected or
                    any(type(code) is not int or not 100 <= code <= 599 for code in expected)):
                raise SmokeError("expect_status must be one or more integer codes")
            if any(200 <= code < 300 for code in expected):
                covered.add((operation.method, operation.path))
            steps.append((step, operation, expected))
        planned.append((scenario, steps))
    if covered != set(operations):
        raise SmokeError("cases lack a successful step for every contract operation")
    messages = []
    global_vars = cases.get("variables") or {}
    if not isinstance(global_vars, dict):
        raise SmokeError("case variables must be an object")
    for scenario_index, (scenario, steps) in enumerate(planned, 1):
        local_vars = scenario.get("variables") or {}
        if not isinstance(local_vars, dict):
            raise SmokeError("scenario variables must be an object")
        context = {**global_vars, **local_vars, "steps": []}
        for step_index, (step, operation, expected) in enumerate(steps, 1):
            label = f"scenario {scenario_index} step {step_index}"
            try:
                body = _resolve(step.get("request", step.get("body")), context)
                headers = _resolve(step.get("headers") or {}, context)
                query = _resolve(step.get("query") or {}, context)
                path_params = _resolve(step.get("path_params") or {}, context)
                if not isinstance(headers, dict) or not isinstance(query, dict) or not isinstance(path_params, dict):
                    raise SmokeError("case headers, query, and path_params must be objects")
                request_schema, required = _request_schema(operation)
                if body is None and required:
                    raise SmokeError("required request body is missing")
                if body is not None and request_schema is not None and _schema_error(body, request_schema):
                    raise SmokeError("request schema mismatch")
                url = _target_url(contract, operation, base_url, path_params)
                if query:
                    url += "?" + urlencode(query, doseq=True)
                status, response_body, raw = _send(url, operation.method, body, headers, timeout, max_bytes)
                if status not in expected:
                    raise SmokeError("unexpected HTTP status")
                schema = _response_schema(operation, status)
                if schema is None:
                    if response_body is not None:
                        raise SmokeError("response body is not declared in contract")
                else:
                    error = _schema_error(response_body, schema)
                    if error:
                        raise SmokeError("response schema mismatch")
                assertions = step.get("expect") or {}
                if not isinstance(assertions, dict):
                    raise SmokeError("expect must be an object of response paths")
                for path, value in assertions.items():
                    if _lookup({"body": response_body, "status": status}, path) != _resolve(value, context):
                        raise SmokeError("response assertion mismatch")
                global_sensitive = cases.get("sensitive_values") or []
                local_sensitive = scenario.get("sensitive_values") or []
                if not isinstance(global_sensitive, list) or not isinstance(local_sensitive, list):
                    raise SmokeError("sensitive_values must be a list")
                sensitive = _resolve([*global_sensitive, *local_sensitive], context)
                if any(isinstance(value, str) and value and value in raw for value in sensitive):
                    raise SmokeError("response echoed a protected value")
                context["steps"].append({"status": status, "body": response_body})
                messages.append(label + ": PASS")
            except SmokeError as exc:
                messages.append(label + ": FAIL " + str(exc))
                return messages
    return messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a deployed mock against OpenAPI and seeded scenarios")
    parser.add_argument("--contract", required=True, help="OpenAPI JSON file")
    parser.add_argument("--cases", required=True, help="JSON file with seeded scenarios")
    parser.add_argument("--base-url", required=True, help="Deployed HTTPS mock URL; include API prefix if overriding it")
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)
    try:
        messages = run(_load_json(args.contract), _load_json(args.cases), args.base_url, timeout=args.timeout)
    except SmokeError as exc:
        print("FAIL " + str(exc))
        return 1
    except Exception:
        print("FAIL unexpected validator error")
        return 1
    for message in messages:
        print(message)
    print("PASS" if messages and all(message.endswith(": PASS") for message in messages) else "FAIL")
    return 0 if messages and all(message.endswith(": PASS") for message in messages) else 1


if __name__ == "__main__":
    raise SystemExit(main())

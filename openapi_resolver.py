"""Resolve local OpenAPI references and conjunctive object schemas.

The builder only consumes concrete schemas.  Unresolved or conflicting schema
parts must stop generation instead of silently dropping required API fields.
"""

from __future__ import annotations

import copy
from typing import Any


def _pointer(document: dict, reference: str) -> Any:
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise ValueError(f"Unsupported OpenAPI reference {reference!r}; only local JSON pointers are supported")
    value: Any = document
    for encoded in reference[2:].split("/"):
        # JSON Pointer permits only ~0 and ~1 escapes.
        if "~" in encoded.replace("~0", "").replace("~1", ""):
            raise ValueError(f"Invalid OpenAPI reference escape in {reference!r}")
        key = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and key.isdecimal() and int(key) < len(value):
            value = value[int(key)]
        else:
            raise ValueError(f"OpenAPI reference {reference!r} does not resolve")
    return value


def _merge_schema(left: dict, right: dict, location: str) -> dict:
    result = copy.deepcopy(left)
    for key, value in right.items():
        if key not in result:
            result[key] = copy.deepcopy(value)
        elif key == "properties":
            if not isinstance(result[key], dict) or not isinstance(value, dict):
                raise ValueError(f"Conflicting OpenAPI properties at {location}")
            for field, field_schema in value.items():
                if field in result[key]:
                    result[key][field] = _merge_schema(result[key][field], field_schema,
                                                        f"{location}.properties.{field}")
                else:
                    result[key][field] = copy.deepcopy(field_schema)
        elif key == "required":
            if not isinstance(result[key], list) or not isinstance(value, list):
                raise ValueError(f"Conflicting OpenAPI required list at {location}")
            result[key] = list(dict.fromkeys([*result[key], *value]))
        elif key == "enum":
            if not isinstance(result[key], list) or not isinstance(value, list):
                raise ValueError(f"Conflicting OpenAPI enum at {location}")
            result[key] = [item for item in result[key] if item in value]
            if not result[key]:
                raise ValueError(f"Incompatible OpenAPI enum at {location}")
        elif key == "description":
            result[key] = "\n".join(dict.fromkeys(filter(None, (result[key], value))))
        elif key in ("items", "additionalProperties") and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_schema(result[key], value, f"{location}.{key}")
        elif result[key] != value:
            raise ValueError(f"Conflicting OpenAPI allOf keyword {key!r} at {location}")
    return result


def resolve_schema(schema: dict, document: dict, *, location: str = "schema",
                   _stack: tuple[str, ...] = ()) -> dict:
    """Expand local refs and allOf, including nested property and item schemas."""
    if not isinstance(schema, dict):
        raise ValueError(f"OpenAPI {location} must be an object")
    if "oneOf" in schema or "anyOf" in schema or "not" in schema:
        raise ValueError(f"Unsupported OpenAPI schema union/negation at {location}")
    base: dict = {}
    if "$ref" in schema:
        reference = schema["$ref"]
        if reference in _stack:
            raise ValueError(f"Cyclic OpenAPI reference at {location}: {' -> '.join((*_stack, reference))}")
        target = _pointer(document, reference)
        base = resolve_schema(target, document, location=reference,
                              _stack=(*_stack, reference))
    local = {key: value for key, value in schema.items() if key not in ("$ref", "allOf")}
    if "properties" in local:
        if not isinstance(local["properties"], dict):
            raise ValueError(f"OpenAPI properties at {location} must be an object")
        local["properties"] = {
            name: resolve_schema(value, document, location=f"{location}.properties.{name}",
                                 _stack=_stack)
            for name, value in local["properties"].items()
        }
    for key in ("items", "additionalProperties"):
        if isinstance(local.get(key), dict):
            local[key] = resolve_schema(local[key], document,
                                        location=f"{location}.{key}", _stack=_stack)
    result = _merge_schema(base, local, location)
    if "allOf" in schema:
        parts = schema["allOf"]
        if not isinstance(parts, list) or not parts:
            raise ValueError(f"OpenAPI allOf at {location} must contain schemas")
        for index, part in enumerate(parts):
            result = _merge_schema(result, resolve_schema(part, document,
                location=f"{location}.allOf[{index}]", _stack=_stack), location)
    return result


def resolve_component(value: dict, document: dict, *, location: str) -> dict:
    """Expand a requestBody/response component reference without changing it."""
    if not isinstance(value, dict):
        raise ValueError(f"OpenAPI {location} must be an object")
    stack: tuple[str, ...] = ()
    resolved = copy.deepcopy(value)
    while "$ref" in resolved:
        reference = resolved["$ref"]
        if reference in stack:
            raise ValueError(f"Cyclic OpenAPI reference at {location}: {' -> '.join((*stack, reference))}")
        target = _pointer(document, reference)
        if not isinstance(target, dict):
            raise ValueError(f"OpenAPI reference {reference!r} must resolve to an object")
        resolved = {**copy.deepcopy(target), **{k: v for k, v in resolved.items() if k != "$ref"}}
        stack = (*stack, reference)
    return resolved


def request_schema(operation: dict, document: dict) -> dict:
    body = resolve_component(operation.get("requestBody") or {}, document, location="requestBody")
    schema = (body.get("content") or {}).get("application/json", {}).get("schema") or {}
    return resolve_schema(schema, document, location="requestBody.content.application/json.schema")


def response_schemas(operation: dict, document: dict) -> dict[str, tuple[dict, dict]]:
    """Return code -> (resolved response component, resolved JSON schema)."""
    result = {}
    for code, response in (operation.get("responses") or {}).items():
        resolved = resolve_component(response, document, location=f"responses.{code}")
        schema = (resolved.get("content") or {}).get("application/json", {}).get("schema") or {}
        result[str(code)] = (resolved, resolve_schema(schema, document,
                             location=f"responses.{code}.content.application/json.schema"))
    return result


def normalize_openapi(document: dict) -> dict:
    """Return an OpenAPI copy with operation request/response schemas expanded.

    Components remain intact as source data.  Every schema consumed by the
    builder or QA is concrete, and unsupported references fail before use.
    """
    if not isinstance(document, dict) or not isinstance(document.get("paths"), dict):
        raise ValueError("OpenAPI must contain a paths object")
    normalized = copy.deepcopy(document)
    for path, path_item in normalized["paths"].items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"} or not isinstance(operation, dict):
                continue
            original = document["paths"][path][method]
            if "requestBody" in operation:
                body = resolve_component(original["requestBody"], document,
                                         location=f"{method.upper()} {path}.requestBody")
                media = (body.get("content") or {}).get("application/json")
                if isinstance(media, dict) and "schema" in media:
                    media["schema"] = resolve_schema(media["schema"], document,
                        location=f"{method.upper()} {path}.requestBody.schema")
                operation["requestBody"] = body
            if "responses" in operation:
                responses = {}
                for code, (response, schema) in response_schemas(original, document).items():
                    media = (response.get("content") or {}).get("application/json")
                    if isinstance(media, dict) and "schema" in media:
                        media["schema"] = schema
                    responses[code] = response
                operation["responses"] = responses
    return normalized

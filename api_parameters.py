"""Bind deterministic OpenAPI operation parameters to API call tools."""

from __future__ import annotations

import re

from openapi_resolver import resolve_component, resolve_schema


_METHODS = frozenset({"get", "post", "put", "patch", "delete"})
_CREDENTIAL_HEADERS = frozenset({
    "authorization", "proxy-authorization", "x-api-key", "api-key",
    "x-team-key", "x-auth-token", "x-access-token",
})


def required_header_bindings(openapi: dict, path: str, method: str) -> dict[str, dict]:
    """Return concrete required header leaves, or reject parameters we cannot send.

    Path-item parameters apply to every operation, while an operation parameter
    with the same (name, location) replaces the path-item version. Required
    query and path parameters are rejected until the API call transport has a
    proven binding for them.
    """
    path_item = (openapi.get("paths") or {}).get(path)
    if not isinstance(path_item, dict) or method.lower() not in _METHODS:
        raise ValueError(f"OpenAPI operation {method.upper()} {path} is unavailable")
    operation = path_item.get(method.lower())
    if not isinstance(operation, dict):
        raise ValueError(f"OpenAPI operation {method.upper()} {path} is unavailable")
    effective: dict[tuple[str, str], dict] = {}
    for scope, owner in (("path", path_item), ("operation", operation)):
        parameters = owner.get("parameters") or []
        if not isinstance(parameters, list):
            raise ValueError(f"{method.upper()} {path} {scope} parameters must be an array")
        seen: set[tuple[str, str]] = set()
        for index, raw in enumerate(parameters):
            parameter = resolve_component(raw, openapi,
                location=f"{method.upper()} {path} {scope}.parameters[{index}]")
            name, location = parameter.get("name"), parameter.get("in")
            if (not isinstance(name, str) or not name.strip() or "\r" in name or "\n" in name or
                    not isinstance(location, str) or not location):
                raise ValueError(f"{method.upper()} {path} {scope} parameter needs name and in")
            key = (location, name.lower() if location == "header" else name)
            if key in seen:
                raise ValueError(f"{method.upper()} {path} has duplicate {location} parameter {name!r}")
            seen.add(key)
            effective[key] = parameter

    if re.search(r"\{[^{}]+\}", path):
        raise ValueError(f"{method.upper()} {path} requires unsupported path parameter binding")

    headers: dict[str, dict] = {}
    for (location, _), parameter in effective.items():
        name = parameter["name"]
        required = parameter.get("required", False)
        if not isinstance(required, bool):
            raise ValueError(f"{method.upper()} {path} parameter {name!r} has invalid required flag")
        if not required:
            continue
        if location in {"path", "query"}:
            raise ValueError(f"{method.upper()} {path} requires unsupported {location} parameter {name!r}")
        if location != "header":
            raise ValueError(f"{method.upper()} {path} requires unsupported {location} parameter {name!r}")
        if name.lower() in _CREDENTIAL_HEADERS:
            raise ValueError(f"{method.upper()} {path} cannot embed credential header {name!r}")
        schema = resolve_schema(parameter.get("schema") or {}, openapi,
                                location=f"{method.upper()} {path} header {name}")
        if schema.get("type") not in (None, "string"):
            raise ValueError(f"{method.upper()} {path} required header {name!r} needs a string schema")
        choices = schema.get("enum")
        if choices is not None and (not isinstance(choices, list) or not choices or
                                    not all(isinstance(value, str) for value in choices)):
            raise ValueError(f"{method.upper()} {path} required header {name!r} has invalid enum")
        fixed = [schema[key] for key in ("const", "default") if key in schema]
        if choices is not None and len(choices) == 1:
            fixed.append(choices[0])
        if (not fixed or any(not isinstance(value, str) or not value or "\r" in value or "\n" in value
                             for value in fixed) or len(set(fixed)) != 1):
            raise ValueError(f"{method.upper()} {path} required header {name!r} needs one fixed enum/default value")
        value = fixed[0]
        if choices is not None and value not in choices:
            raise ValueError(f"{method.upper()} {path} required header {name!r} default is outside enum")
        if name.lower() == "content-type" and value != "application/json":
            raise ValueError(f"{method.upper()} {path} required Content-Type conflicts with JSON body")
        leaf = {"type": "string", "default": value}
        if choices is not None:
            leaf["enum"] = choices
        headers["Content-Type" if name.lower() == "content-type" else name] = leaf
    return headers

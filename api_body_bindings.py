"""Resolve required API request fields that are fixed by the supplied schema."""

from __future__ import annotations

import re

from api_bindings import (close_request_fields, identity_selector_field,
                          note_request_fields, promise_request_fields,
                          transfer_request_fields)
from customer_bindings import customer_identifier_field


def _one_named_field(schema: dict, role: str, words: set[str]) -> str:
    matches = [field for field in (schema.get("properties") or {})
               if set(re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_").split("_")) & words]
    if len(matches) != 1:
        raise ValueError(f"{role} request needs one {role} field; found {matches!r}")
    return matches[0]


def dynamic_request_fields(name: str, schema: dict) -> set[str]:
    """Fields whose values come from the current call or a validated workflow step."""
    dynamic = {customer_identifier_field(schema)}
    if name == "get_account":
        return dynamic
    if name == "verify_identity":
        selector, _ = identity_selector_field(schema)
        return dynamic | {selector, _one_named_field(schema, "security answer", {"answer", "response"})}
    if name == "create_promise":
        return dynamic | set(promise_request_fields(schema).values())
    if name == "add_note":
        return dynamic | set(note_request_fields(schema).values())
    if name == "schedule_callback":
        return dynamic | {_one_named_field(schema, "callback date", {"date", "day"}),
                          _one_named_field(schema, "callback time", {"time", "slot"})}
    if name == "transfer_to_agent":
        return dynamic | set(transfer_request_fields(schema).values())
    if name == "close_call":
        return dynamic | {field for field in close_request_fields(schema).values() if field}
    raise ValueError(f"Unsupported API operation {name!r}")


def fixed_required_body_values(name: str, schema: dict, source: str = "",
                               provided: dict | None = None) -> dict[str, object]:
    """Return unambiguous constants for required fields outside dynamic roles.

    A new required field cannot silently become an unresolved ``{{field}}``.
    """
    properties, required = schema.get("properties"), schema.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise ValueError(f"{name} request needs object properties and required fields")
    dynamic = dynamic_request_fields(name, schema)
    fixed: dict[str, object] = {}
    for field in required:
        spec = properties.get(field)
        if not isinstance(field, str) or not isinstance(spec, dict):
            raise ValueError(f"{name} required request field {field!r} is absent from properties")
        if field in dynamic:
            continue
        candidates = [spec[key] for key in ("const", "default") if key in spec]
        choices = spec.get("enum")
        if choices is not None:
            if not isinstance(choices, list) or not choices:
                raise ValueError(f"{name} required request field {field!r} has invalid enum")
            if len(choices) == 1:
                candidates.append(choices[0])
        if not candidates and isinstance(choices, list) and all(isinstance(choice, str) for choice in choices):
            field_words = re.escape(field).replace("_", r"[_\s]+")
            occurrences = list(re.finditer(rf"\b{field_words}\b\s*(?:is|=|:)?\s*"
                                          rf"({'|'.join(re.escape(choice) for choice in choices)})\b",
                                          source, re.I))
            if occurrences:
                last = occurrences[-1]
                tail = source[last.end():last.end() + 35]
                if re.match(r"\s*(?:or|/)\s*(?:" + "|".join(re.escape(choice) for choice in choices) + r")\b", tail, re.I):
                    raise ValueError(f"{name} required request field {field!r} has ambiguous source values")
                candidates.append(next(choice for choice in choices if choice.lower() == last.group(1).lower()))
            elif provided is not None and field in provided:
                candidates.append(provided[field])
        if not candidates or any(value != candidates[0] for value in candidates):
            raise ValueError(f"{name} required request field {field!r} needs one fixed enum/const/default value")
        value = candidates[0]
        expected_type = {"string": str, "integer": int, "number": (int, float), "boolean": bool}
        kind = spec.get("type")
        if kind not in expected_type or not isinstance(value, expected_type[kind]) or (
                kind in ("integer", "number") and isinstance(value, bool)) or (
                isinstance(value, str) and (not value or "\r" in value or "\n" in value)):
            raise ValueError(f"{name} required request field {field!r} has invalid fixed value")
        if choices is not None and value not in choices:
            raise ValueError(f"{name} required request field {field!r} fixed value is outside enum")
        fixed[field] = value
    return fixed

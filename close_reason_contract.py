"""Resolve the no-reason value for a required close_call reason field."""

from __future__ import annotations

import re

from api_bindings import close_request_fields
from reason_code_mappings import explicit_reason_codes


_NO_REASON_LABELS = ("none", "no_reason", "not_applicable", "n_a")


def required_no_reason_value(schema: dict, source: str) -> str | None:
    """Return a source/API-backed value, or reject an unfulfillable requirement."""
    reason_field = close_request_fields(schema)["reason"]
    if not reason_field or reason_field not in (schema.get("required") or []):
        return None
    spec = (schema.get("properties") or {}).get(reason_field) or {}
    allowed = spec.get("enum") or []
    if allowed and (not isinstance(allowed, list) or not all(
            isinstance(value, str) and value for value in allowed)):
        raise ValueError("close_call required reason enum must contain string values")
    _, explicit = explicit_reason_codes(source, [], allowed)
    candidates = {explicit[label] for label in _NO_REASON_LABELS if label in explicit}
    candidates.update(value for value in allowed if re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
                      in _NO_REASON_LABELS)
    if len(candidates) != 1:
        raise ValueError("close_call requires a reason for promise/paid outcomes but has no unique no-reason value")
    value = next(iter(candidates))
    if allowed and value not in allowed:
        raise ValueError("close_call no-reason value is absent from the API enum")
    return value

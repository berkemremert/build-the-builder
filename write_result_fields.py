"""Bind bank write-result booleans to their OpenAPI response schemas."""

from __future__ import annotations

import re

from openapi_resolver import response_schemas


_GENERIC = {"ok", "success", "succeeded", "successful", "completed", "done"}
_BY_OPERATION = {
    "add_note": {"saved", "noted", "recorded", "created"},
    "transfer_to_agent": {"accepted", "transferred", "connected", "queued"},
    "schedule_callback": {"booked", "scheduled", "accepted", "created"},
}
_SUCCESS_WORDS = re.compile(r"\b(?:success(?:ful(?:ly)?)?|saved|recorded|created|"
                            r"accepted|transferred|connected|booked|scheduled|carried out)\b", re.I)


def write_success_field(operation: dict, name: str, openapi: dict | None = None) -> str:
    """Find the single 2xx boolean that explicitly represents a completed write."""
    if name not in _BY_OPERATION:
        raise ValueError(f"Unsupported write-result operation {name!r}")
    responses = response_schemas(operation, openapi) if openapi is not None else {
        str(code): (response, (response.get("content") or {}).get("application/json", {}).get("schema") or {})
        for code, response in (operation.get("responses") or {}).items()
        if isinstance(response, dict)
    }
    fields = []
    for code, (response, schema) in responses.items():
        if not (code.isdigit() and 200 <= int(code) < 300):
            continue
        properties = schema.get("properties") or {}
        candidates = []
        for field, spec in properties.items():
            if not isinstance(spec, dict) or spec.get("type") != "boolean":
                continue
            normalized = re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_")
            name_signal = normalized in _GENERIC | _BY_OPERATION[name]
            detail = str(spec.get("description") or "")
            response_detail = str(response.get("description") or "")
            described = bool(_SUCCESS_WORDS.search(detail) and re.search(r"\btrue\b", detail, re.I))
            described |= bool(re.search(r"[\x60'\"]" + re.escape(field) + r"[\x60'\"]", response_detail, re.I)
                              and _SUCCESS_WORDS.search(response_detail))
            if name_signal or described:
                candidates.append(field)
        if len(candidates) != 1:
            raise ValueError(f"{name} response {code} needs one unambiguous boolean write-success field")
        fields.append(candidates[0])
    if not fields or len(set(fields)) != 1:
        raise ValueError(f"{name} successful responses need one consistent boolean write-success field")
    return fields[0]

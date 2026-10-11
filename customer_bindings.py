"""Identify the call customer's request field from an OpenAPI body schema."""

from __future__ import annotations

import re


_PERSON_ROLES = {"customer", "client", "borrower", "debtor"}
_ID_WORDS = {"id", "identifier", "ref", "reference"}


def customer_identifier_field(schema: dict) -> str:
    """Return one required customer identifier or fail on missing/ambiguous schema."""
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise ValueError("Request needs one required customer identifier field")
    candidates = []
    for field in required:
        spec = properties.get(field)
        if not isinstance(field, str) or not isinstance(spec, dict):
            continue
        if spec.get("type") not in (None, "string"):
            continue
        words = set(re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_").split("_"))
        description = str(spec.get("description") or "")
        described_as_person_id = bool(re.search(
            r"\b(?:customer|client|borrower|debtor)\b.{0,45}"
            r"\b(?:id|identifier|reference)\b", description, re.I))
        if (words & _PERSON_ROLES and words & _ID_WORDS) or described_as_person_id:
            candidates.append(field)
    if len(candidates) != 1:
        raise ValueError(f"Request needs one required customer identifier field; found {candidates!r}")
    return candidates[0]

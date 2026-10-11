"""Infer semantic request fields from the supplied bank API schema."""

from __future__ import annotations

import re

from openapi_resolver import response_schemas


def identity_selector_field(schema: dict) -> tuple[str, list[str]]:
    """Find the request field that chooses which security answer to verify.

    A unique multi-value string enum is sufficient. When the API has several
    such enums, require a clear selector name or description; do not guess.
    """
    candidates: list[tuple[int, str, list[str]]] = []
    for field, spec in (schema.get("properties") or {}).items():
        if not isinstance(spec, dict):
            continue
        values = spec.get("enum")
        if not isinstance(values, list) or len(values) < 2 or not all(
                isinstance(value, str) and value for value in values):
            continue
        kind = spec.get("type")
        if kind not in (None, "string"):
            continue
        words = set(re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_").split("_"))
        description = str(spec.get("description") or "").lower()
        score = 0
        if words & {"field", "question", "challenge", "check", "factor"}:
            score += 2
        if words & {"identity", "security", "verification", "verify"}:
            score += 1
        if re.search(r"\b(?:which|choose|select)\b.{0,50}\b(?:question|field|check|factor)\b", description):
            score += 2
        candidates.append((score, field, values))
    if len(candidates) == 1:
        _, field, values = candidates[0]
        return field, values
    if candidates:
        best = max(score for score, _, _ in candidates)
        winners = [(field, values) for score, field, values in candidates if score == best]
        if best > 0 and len(winners) == 1:
            return winners[0]
    raise ValueError("verify_identity request needs one unambiguous security selector with at least two enum values")


def promise_request_fields(schema: dict) -> dict[str, str]:
    """Map product, amount and date to their unambiguous API field names."""
    properties = schema.get("properties") or {}
    found: dict[str, list[str]] = {"product": [], "amount": [], "date": []}
    for field, spec in properties.items():
        name = re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_")
        if name == "customer_id" or name.startswith("customer_"):
            continue
        words = set(name.split("_"))
        description = str(spec.get("description") or "").lower() if isinstance(spec, dict) else ""
        role = None
        if words & {"date", "day"} or "settlement date" in description:
            role = "date"
        elif words & {"amount", "total", "sum"} or "payment amount" in description:
            role = "amount"
        elif (words & {"product", "account", "loan", "card", "debt"} and
              (words & {"id", "ref", "reference"} or "identifier" in description)):
            role = "product"
        if role:
            found[role].append(field)
    ambiguous = {role: names for role, names in found.items() if len(names) != 1}
    if ambiguous:
        raise ValueError(f"create_promise request fields need one product, amount and date mapping: {ambiguous}")
    return {role: names[0] for role, names in found.items()}


def note_request_fields(schema: dict) -> dict[str, str]:
    """Map the product reference and note reason when each is unambiguous."""
    found: dict[str, list[str]] = {"product": [], "reason": []}
    for field, spec in (schema.get("properties") or {}).items():
        name = re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_")
        if name == "customer_id" or name.startswith("customer_"):
            continue
        words = set(name.split("_"))
        description = str(spec.get("description") or "").lower() if isinstance(spec, dict) else ""
        if ("reason" in words or
                ("note" in words and "code" in words) or
                "note reason" in description):
            found["reason"].append(field)
        elif (words & {"product", "account", "loan", "card", "debt"} and
              (words & {"id", "ref", "reference"} or "identifier" in description)):
            found["product"].append(field)
    ambiguous = {role: names for role, names in found.items() if len(names) != 1}
    if ambiguous:
        raise ValueError(f"add_note request fields need one product and reason mapping: {ambiguous}")
    return {role: names[0] for role, names in found.items()}


def transfer_request_fields(schema: dict) -> dict[str, str]:
    """Find the transfer destination field, including renamed routing codes."""
    found = []
    for field, spec in (schema.get("properties") or {}).items():
        name = re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_")
        words = set(name.split("_"))
        description = str(spec.get("description") or "").lower() if isinstance(spec, dict) else ""
        if words & {"queue", "routing", "route", "destination", "department"} or re.search(
                r"\b(?:transfer|routing)\s+(?:queue|destination|code)\b", description):
            found.append(field)
    if len(found) != 1:
        raise ValueError(f"transfer_to_agent request needs one unambiguous queue field: {found!r}")
    return {"queue": found[0]}


def close_request_fields(schema: dict) -> dict[str, str | None]:
    """Find close outcome and optional reason fields from names/descriptions."""
    found: dict[str, list[str]] = {"outcome": [], "reason": []}
    for field, spec in (schema.get("properties") or {}).items():
        name = re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_")
        words = set(name.split("_"))
        description = str(spec.get("description") or "").lower() if isinstance(spec, dict) else ""
        if name == "customer_id" or name.startswith("customer_"):
            continue
        if words & {"outcome", "result", "disposition"} or "call outcome" in description:
            found["outcome"].append(field)
        elif words & {"reason", "detail", "cause"} or re.search(
                r"\b(?:close|outcome)\s+reason\b", description):
            found["reason"].append(field)
    if len(found["outcome"]) != 1 or len(found["reason"]) > 1:
        raise ValueError(f"close_call request needs one outcome and at most one reason field: {found!r}")
    return {"outcome": found["outcome"][0],
            "reason": found["reason"][0] if found["reason"] else None}


def account_collection_field(response_schema: dict) -> str:
    """Find the account's single product collection in an OpenAPI response."""
    properties = response_schema.get("properties") or {}
    arrays = [name for name, spec in properties.items()
              if isinstance(spec, dict) and spec.get("type") == "array" and
              isinstance(spec.get("items"), dict) and
              (spec["items"].get("type") == "object" or spec["items"].get("properties"))]
    if "products" in arrays:
        return "products"
    if len(arrays) == 1:
        return arrays[0]
    raise ValueError(f"get_account response needs one product collection; found {arrays!r}")


def account_response_collection(operation: dict, openapi: dict | None = None) -> tuple[str, dict]:
    """Find one product array across the operation's successful JSON responses."""
    matches = []
    resolved = response_schemas(operation, openapi) if openapi is not None else {
        str(code): (response, (response.get("content") or {}).get("application/json", {}).get("schema") or {})
        for code, response in (operation.get("responses") or {}).items() if isinstance(response, dict)}
    for code, (response, schema) in resolved.items():
        if not (str(code).isdigit() and 200 <= int(code) < 300 and isinstance(response, dict)):
            continue
        if not schema.get("properties"):
            continue
        field = account_collection_field(schema)
        matches.append((field, schema))
    if not matches or len({field for field, _ in matches}) != 1:
        raise ValueError("get_account successful responses need one consistent product collection")
    return matches[0]


def identity_response_fields(operation: dict, openapi: dict | None = None) -> dict[str, str]:
    """Identify per-answer, fully-verified and remaining-attempt fields."""
    mappings = []
    resolved = response_schemas(operation, openapi) if openapi is not None else {
        str(code): (response, (response.get("content") or {}).get("application/json", {}).get("schema") or {})
        for code, response in (operation.get("responses") or {}).items() if isinstance(response, dict)}
    for code, (response, schema) in resolved.items():
        if not (str(code).isdigit() and 200 <= int(code) < 300 and isinstance(response, dict)):
            continue
        if not schema.get("properties"):
            continue
        found: dict[str, list[str]] = {"answer": [], "complete": [], "remaining": []}
        for field, spec in (schema.get("properties") or {}).items():
            if not isinstance(spec, dict):
                continue
            name = re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_")
            words = set(name.split("_"))
            description = str(spec.get("description") or "").lower()
            if spec.get("type") == "boolean":
                if (name in {"all_verified", "fully_verified", "identity_complete", "complete"} or
                        "all" in words and "verified" in words or
                        "fully verified" in description):
                    found["complete"].append(field)
                elif (name in {"verified", "passed", "answer_verified", "answer_passed"} or
                      "answer" in words and words & {"verified", "passed"} or
                      "answer passed" in description):
                    found["answer"].append(field)
            elif spec.get("type") == "integer":
                if (name in {"wrong_answers_left", "tries_left", "attempts_remaining", "remaining_attempts"} or
                        words & {"left", "remaining"} and words & {"tries", "attempts", "answers"}):
                    found["remaining"].append(field)
        if any(len(names) != 1 for names in found.values()):
            raise ValueError(f"verify_identity response {code} has ambiguous result fields: {found}")
        mappings.append({role: names[0] for role, names in found.items()})
    if not mappings or any(mapping != mappings[0] for mapping in mappings[1:]):
        raise ValueError("verify_identity successful response needs one answer, complete and remaining-attempt field mapping")
    return mappings[0]

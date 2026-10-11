"""Infer semantic request fields from the supplied bank API schema."""

from __future__ import annotations

import re


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


def account_response_collection(operation: dict) -> tuple[str, dict]:
    """Find one product array across the operation's successful JSON responses."""
    matches = []
    for code, response in (operation.get("responses") or {}).items():
        if not (str(code).isdigit() and 200 <= int(code) < 300 and isinstance(response, dict)):
            continue
        schema = (response.get("content") or {}).get("application/json", {}).get("schema") or {}
        if not schema.get("properties"):
            continue
        field = account_collection_field(schema)
        matches.append((field, schema))
    if not matches or len({field for field, _ in matches}) != 1:
        raise ValueError("get_account successful responses need one consistent product collection")
    return matches[0]


def identity_response_fields(operation: dict) -> dict[str, str]:
    """Identify per-answer, fully-verified and remaining-attempt fields."""
    mappings = []
    for code, response in (operation.get("responses") or {}).items():
        if not (str(code).isdigit() and 200 <= int(code) < 300 and isinstance(response, dict)):
            continue
        schema = (response.get("content") or {}).get("application/json", {}).get("schema") or {}
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

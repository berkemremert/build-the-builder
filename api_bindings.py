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

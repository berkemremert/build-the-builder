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

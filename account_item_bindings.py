"""Infer the semantic fields of one get_account product item.

Only schema names and descriptions are used. Ambiguous or missing required
fields fail at workflow build time instead of silently producing zero balances.
"""

from __future__ import annotations

import re


def product_item_fields(item_schema: dict) -> dict[str, object]:
    """Map product ID, kind, arrears, age, floor fields, and optional card suffix."""
    properties = item_schema.get("properties") or {}
    if not isinstance(properties, dict):
        raise ValueError("get_account product item has no object properties")
    found: dict[str, list[str]] = {role: [] for role in
                                   ("product_id", "kind", "past_due", "days_late", "floor", "card_last4")}
    for field, spec in properties.items():
        if not isinstance(spec, dict):
            continue
        name = re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_")
        words = set(name.split("_"))
        description = str(spec.get("description") or "").lower()
        value_type = spec.get("type")
        numeric = value_type in (None, "number", "integer")
        textual = value_type in (None, "string")
        role = None
        if textual and (name in {"card_last4", "card_last_four", "card_suffix"} or
                        words & {"card"} and words & {"last4", "suffix"}):
            role = "card_last4"
        elif textual and (name == "product_id" or
                          words & {"product", "account", "loan", "card", "debt"} and
                          words & {"id", "ref", "reference", "identifier"} or
                          re.search(r"\bproduct (?:id|identifier|reference)\b", description)):
            role = "product_id"
        elif textual and (name == "type" or
                          words & {"product", "account", "loan", "card"} and
                          words & {"type", "kind", "category"} or
                          re.search(r"\bproduct (?:type|kind|category)\b", description)):
            role = "kind"
        elif numeric and (name in {"days_late", "late_days", "days_overdue", "overdue_days",
                                  "delinquency_days", "days_delinquent"} or
                          words & {"days", "day"} and
                          words & {"late", "overdue", "delinquency", "delinquent"} or
                          re.search(r"\bdays? (?:late|overdue|delinquent)\b", description)):
            role = "days_late"
        elif numeric and (name in {"minimum_payment", "min_payment", "instalment_amount",
                                  "installment_amount", "floor_amount", "required_payment"} or
                          words & {"minimum", "min", "floor", "instalment", "installment"} and
                          words & {"amount", "payment", "due"} or
                          re.search(r"\b(?:minimum|floor|required) payment\b", description)):
            role = "floor"
        elif numeric and (name in {"past_due", "past_due_amount", "arrears_total",
                                  "arrears_amount", "overdue_amount", "balance_due",
                                  "outstanding_balance"} or
                          words & {"arrears", "overdue"} and
                          words & {"total", "amount", "balance"} or
                          words & {"past", "due"} or
                          re.search(r"\b(?:total arrears|past.due amount|overdue balance)\b", description)):
            role = "past_due"
        if role:
            found[role].append(field)
    required = ("product_id", "kind", "past_due", "days_late")
    ambiguous = {role: found[role] for role in required if len(found[role]) != 1}
    if len(found["floor"]) < 1:
        ambiguous["floor"] = found["floor"]
    if len(found["card_last4"]) > 1:
        ambiguous["card_last4"] = found["card_last4"]
    if ambiguous:
        raise ValueError(f"get_account product item needs unambiguous semantic fields: {ambiguous}")
    return {"product_id": found["product_id"][0], "kind": found["kind"][0],
            "past_due": found["past_due"][0], "days_late": found["days_late"][0],
            "floors": found["floor"],
            "card_last4": found["card_last4"][0] if found["card_last4"] else None}

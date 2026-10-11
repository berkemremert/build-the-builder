"""Detect structural workflow capabilities from policy text and its source."""

from __future__ import annotations

import re


_ITEM_NOUN = r"(?:products?|accounts?|items?|debts?|balances?)"
_MULTI_PRODUCT = re.compile(
    rf"\b(?:one\s*(?:to|through|[-–]|or)\s*(?:two|three|2|3)|"
    rf"between\s+one\s+and\s+(?:two|three|2|3)|"
    rf"up\s+to\s+(?:two|three|2|3)|two\s+or\s+more|multiple|several)\s+{_ITEM_NOUN}\b|"
    rf"\b(?:all|each)\s+(?:of\s+)?(?:the\s+)?{_ITEM_NOUN}\b|"
    rf"\b{_ITEM_NOUN}\s+one\s+by\s+one\b|"
    rf"\bmore\s+than\s+one\s+{_ITEM_NOUN}\b|"
    rf"\bper-(?:account|product)\b|"
    rf"\beach\s+{_ITEM_NOUN}.{{0,35}}\border\b",
    re.IGNORECASE | re.DOTALL,
)


def requires_multi_product(policy: dict) -> bool:
    """Use explicit source or extracted rules to preserve multi-product routing."""
    constraints = policy.get("constraints") or {}
    limits = constraints.get("product_limits") if isinstance(constraints, dict) else ""
    text = "\n".join(str(value or "") for value in
                     (policy.get("raw_brief"), policy.get("verified_call"), limits))
    return bool(_MULTI_PRODUCT.search(text))

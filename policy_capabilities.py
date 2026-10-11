"""Detect structural workflow capabilities from policy text and its source."""

from __future__ import annotations

import re


_MULTI_PRODUCT = re.compile(
    r"\b(?:one\s*(?:to|through|[-–])\s*(?:three|3)|"
    r"up\s+to\s+(?:three|3)|two\s+or\s+more|multiple|several)\s+products\b|"
    r"\b(?:all|each)\s+(?:of\s+)?(?:the\s+)?products\b|"
    r"\bproducts?\s+one\s+by\s+one\b|"
    r"\bmore\s+than\s+one\s+product\b|"
    r"\beach\s+product.{0,35}\border\b",
    re.IGNORECASE | re.DOTALL,
)


def requires_multi_product(policy: dict) -> bool:
    """Use explicit source or extracted rules to preserve multi-product routing."""
    constraints = policy.get("constraints") or {}
    limits = constraints.get("product_limits") if isinstance(constraints, dict) else ""
    text = "\n".join(str(value or "") for value in
                     (policy.get("raw_brief"), policy.get("verified_call"), limits))
    return bool(_MULTI_PRODUCT.search(text))

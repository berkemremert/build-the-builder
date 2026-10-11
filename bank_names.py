"""Resolve the bank name from ordered source amendments."""

from __future__ import annotations

import re


BANK_NAME = re.compile(
    r"\b[A-Z][A-Za-z0-9&-]*(?:\s+[A-Z][A-Za-z0-9&-]*){0,4}\s+(?:Bank|Credit Union)\b"
)
_REVISION = re.compile(
    r"(?=^[ \t]*(?:later|updated|amended)\s+(?:memo|brief|policy)\b)|"
    r"(?=^=+\s*SOURCE:)", re.I | re.M,
)


def _directive_names(block: str) -> set[str]:
    explicit = set()
    for match in re.finditer(r"\b(?:renamed|now called|now named)\b\s*(?:(?:to|as)\s+)?", block, re.I):
        name = BANK_NAME.match(block, match.end())
        if name:
            explicit.add(name.group())
    for line in block.splitlines():
        if not re.search(r"\b(?:first sentence|opening(?: sentence| line)?)\b", line, re.I):
            continue
        if not re.search(r"\b(?:exactly|verbatim|must say|required wording)\b", line, re.I):
            continue
        for quote in re.findall(r'["“]([^"”]+)["”]', line):
            explicit.update(match.group() for match in BANK_NAME.finditer(quote))
    return explicit


def source_bank_name(source: str) -> str | None:
    """Use the latest named revision; reject unresolved names in one revision."""
    selected = None
    for block in _REVISION.split(source):
        names = {match.group() for match in BANK_NAME.finditer(block)}
        if not names:
            continue
        explicit = _directive_names(block)
        if len(explicit) > 1 or (not explicit and len(names) > 1):
            raise ValueError("Conflicting bank names in the same source revision")
        selected = next(iter(explicit or names))
    return selected

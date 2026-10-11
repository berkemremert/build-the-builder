"""Resolve the bank name from ordered source amendments."""

from __future__ import annotations

import re


BANK_NAME = re.compile(
    r"\b[A-Z][A-Za-z0-9&-]*(?:[ ]+[A-Z][A-Za-z0-9&-]*){1,5}\b"
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
        name = f"(?-i:{BANK_NAME.pattern})"
        quote_patterns = (
            rf"\b(?:this is|from|for|on behalf of)[ ]+(?P<name>{name})",
            rf"(?P<name>{name})['’]s[ ]+digital[ ]+assistant\b",
        )
        for quote in re.findall(r'["“]([^"”]+)["”]', line):
            for pattern in quote_patterns:
                explicit.update(match.group("name") for match in re.finditer(pattern, quote, re.I))
    return explicit


def _mentioned_names(block: str) -> set[str]:
    """Read names only where the source identifies the institution."""
    names = _directive_names(block)
    name = f"(?-i:{BANK_NAME.pattern})"
    patterns = (
        # A source title or a standalone heading can name the institution.
        rf"^[ \t]*\#{{1,6}}[ \t]*Brief[^:\n]*:[ \t]*(?P<name>{name})",
        rf"^[ \t]*(?P<name>{name})[ \t]*$",
        # These are statements about the organization, not customer names.
        rf"(?P<name>{name})\s+(?:records?\s+calls?|calls?\s+customers|is\s+fictional|holidays?)\b",
        rf"\b(?:calling\s+from|call\s+from|assistant\s+(?:from|for)|"
        rf"agent\s+(?:from|for)|represent(?:s|ing)|calls?\s+it|"
        rf"(?:bank|institution|company|organization|lender)\s+(?:is|called|named))\s+"
        rf"(?P<name>{name})",
    )
    for pattern in patterns:
        names.update(match.group("name") for match in re.finditer(pattern, block, re.I | re.M))
    joint = rf"(?P<first>{name})[ ]+and[ ]+(?P<second>{name})[ ]+jointly[ ]+own[ ]+this[ ]+call\b"
    for match in re.finditer(joint, block, re.I):
        names.update((match.group("first"), match.group("second")))
    return names


def source_bank_name(source: str) -> str | None:
    """Use the latest named revision; reject unresolved names in one revision."""
    selected = None
    for block in _REVISION.split(source):
        names = _mentioned_names(block)
        if not names:
            continue
        explicit = _directive_names(block)
        if len(explicit) > 1 or (not explicit and len(names) > 1):
            raise ValueError("Conflicting bank names in the same source revision")
        selected = next(iter(explicit or names))
    return selected

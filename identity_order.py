"""Find the latest explicit security-question order in the source brief."""

from __future__ import annotations

import re


ALIASES = {
    "date_of_birth": ("date of birth", "birth date", "dob"),
    "phone_last4": ("last four digits of the phone", "last 4 digits of the phone", "phone number"),
    "birth_year": ("birth year",),
}
_MORE_THAN_TWO_CHECKS = re.compile(
    r"\b(?:three|four|five|six|seven|eight|nine|ten|[3-9]|10)\s+"
    r"(?:(?:security|identity|verification)\s+)?"
    r"(?:questions|checks|factors)\b|"
    r"\ball\s+(?:three|four|five|six|seven|eight|nine|ten|[3-9]|10)\s+"
    r"(?:must|need to|have to)\s+"
    r"(?:pass|be\s+(?:verified|completed))\b",
    re.I,
)


def latest_explicit_identity_order(source: str, values: list[str]) -> list[str] | None:
    """Return the final clearly ordered pair of selectors, or None if absent."""
    if _MORE_THAN_TWO_CHECKS.search(source):
        raise ValueError("Source requires more than two identity checks; workflow supports two")
    latest = None
    for sentence in re.finditer(r"[^.!?\n]+", source):
        clause = sentence.group()
        hits = []
        for value in values:
            for name in ALIASES.get(value, (value.replace("_", " "),)):
                pattern = r"(?<!\w)" + re.escape(name) + r"(?!\w)"
                hits.extend((match.start(), match.end(), value)
                            for match in re.finditer(pattern, clause, re.I))
        hits.sort()
        ordered = []
        for _, _, value in hits:
            if value not in ordered:
                ordered.append(value)
        if len(ordered) > 2 and re.search(
                r"\b(?:ask|verify|check|first|then|before|after|order)\b", clause, re.I):
            raise ValueError("Source requires more than two identity checks; workflow supports two")
        if not re.search(r"\b(?:first|then|before|after|followed by|identity order)\b", clause, re.I):
            continue
        if len(ordered) != 2:
            continue
        first = next(hit for hit in hits if hit[2] == ordered[0])
        second = next(hit for hit in hits if hit[2] == ordered[1])
        if re.search(r"\bafter\b", clause[first[1]:second[0]], re.I):
            ordered.reverse()
        latest = ordered
    return latest

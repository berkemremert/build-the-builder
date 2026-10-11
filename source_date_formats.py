"""Parse numeric dates in source holiday rules without guessing date order."""

from __future__ import annotations

import datetime
import re

from source_holidays import _holiday_lines, is_holiday_date_context


_YEAR_FIRST = re.compile(r"\b(?P<year>20\d{2})/(?P<month>\d{1,2})/(?P<day>\d{1,2})\b")
_YEAR_LAST = re.compile(r"\b(?P<left>\d{1,2})/(?P<right>\d{1,2})/(?P<year>20\d{2})\b")
_ORDER = re.compile(
    r"\b(?P<dmy>DD/MM/YYYY|D/M/YYYY|day/month/year|day[- ]first|"
    r"(?:UK|British|European)\s+(?:date\s+)?format)\b|"
    r"\b(?P<mdy>MM/DD/YYYY|M/D/YYYY|month/day/year|month[- ]first|"
    r"(?:US|American)\s+(?:date\s+)?format)\b", re.I)


def slash_date_matches(source: str) -> list[tuple[int, int, str]]:
    """Return source offsets and ISO dates; reject ambiguous or invalid slash dates."""
    declarations = [(match.start(), "dmy" if match["dmy"] else "mdy")
                    for match in _ORDER.finditer(source)]
    governed = _holiday_lines(source)
    found: list[tuple[int, int, str]] = []
    for match in _YEAR_FIRST.finditer(source):
        if not is_holiday_date_context(source, match.start(), governed):
            continue
        try:
            date = datetime.date(int(match["year"]), int(match["month"]), int(match["day"]))
        except ValueError as exc:
            raise ValueError(f"Invalid source slash date {match.group()!r}") from exc
        found.append((match.start(), match.end(), date.isoformat()))
    for match in _YEAR_LAST.finditer(source):
        if not is_holiday_date_context(source, match.start(), governed):
            continue
        left, right = int(match["left"]), int(match["right"])
        if left > 12 and right <= 12:
            order = "dmy"
        elif right > 12 and left <= 12:
            order = "mdy"
        elif left == right and 1 <= left <= 12:
            order = "dmy"
        else:
            prior = [kind for offset, kind in declarations if offset <= match.start()]
            declared = prior[-1] if prior else (
                declarations[0][1] if len({kind for _, kind in declarations}) == 1 and declarations else None)
            if not declared:
                raise ValueError(f"Ambiguous source slash date {match.group()!r}; specify DD/MM/YYYY or MM/DD/YYYY")
            order = declared
        day, month = (left, right) if order == "dmy" else (right, left)
        try:
            date = datetime.date(int(match["year"]), month, day)
        except ValueError as exc:
            raise ValueError(f"Invalid source slash date {match.group()!r}") from exc
        found.append((match.start(), match.end(), date.isoformat()))
    return sorted(found)

"""Apply dated holiday lists and amendments in source order."""

from __future__ import annotations

import re


def _holiday_lines(source: str) -> set[int]:
    """Find list/table rows governed by a holiday heading."""
    active = False
    governed: set[int] = set()
    offset = 0
    for line in source.splitlines(keepends=True):
        stripped = line.strip()
        is_heading = bool(re.search(r"\bholidays?\b|\bholiday\s+calendar\b", stripped, re.I) and
                          (stripped.endswith(":") or stripped.startswith("#") or
                           stripped.startswith("|") and re.search(r"\bdate\b", stripped, re.I)))
        if is_heading:
            active = True
        elif not stripped:
            active = False
        elif active and (re.match(r"\s*(?:[-*]\s+|\|)", line) or
                         re.match(r"\s*(?:20\d{2}[-/]\d|\d{1,2}/\d{1,2}/20\d{2})", line)):
            governed.add(offset)
        else:
            active = False
        offset += len(line)
    return governed


def is_holiday_date_context(source: str, offset: int, governed: set[int] | None = None) -> bool:
    """Whether the date is attached to a holiday clause or governed list row."""
    line_start = source.rfind("\n", 0, offset) + 1
    if line_start in (governed if governed is not None else _holiday_lines(source)):
        return True
    start = max(source.rfind(mark, 0, offset) for mark in (";", ".", "\n")) + 1
    ends = [source.find(mark, offset) for mark in (";", ".", "\n")]
    end = min((point for point in ends if point >= 0), default=len(source))
    return "holiday" in source[start:end].lower()


def holiday_dates_from_matches(source: str, matches: list[tuple[int, int, str]]) -> list[str]:
    """Return effective ISO holidays, including changes in later source text."""
    governed = _holiday_lines(source)
    dates: set[str] = set()
    for index, (start_at, end_at, date) in enumerate(matches):
        start = max(source.rfind(mark, 0, start_at) for mark in (";", ".", "\n")) + 1
        ends = [source.find(mark, end_at) for mark in (";", ".", "\n")]
        end = min((point for point in ends if point >= 0), default=len(source))
        if not is_holiday_date_context(source, start_at, governed):
            continue
        next_date = matches[index + 1][0] if index + 1 < len(matches) else len(source)
        following = source[end_at:min(end, next_date)].lower()
        preceding = source[max(start, start_at - 55):start_at].lower()
        revoked = re.search(r"\b(?:no longer|not|removed|revoked|cancelled|canceled)\b", following)
        revoked = revoked or re.search(r"\b(?:remove|revoke|cancel)\b.{0,35}\bholiday\b", preceding)
        if revoked:
            dates.discard(date)
        else:
            dates.add(date)
    return sorted(dates)

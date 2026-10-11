"""Derive the working weekdays from the source brief's ordered calendar rules."""

from __future__ import annotations

import re


_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DAY = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
_WORK = r"(?:business\s+days?|work\s*days?|working\s+days?)"
_RANGE = re.compile(
    rf"\b{_WORK}\s+(?:are|run|fall|extend|span)\s+(?:from\s+)?"
    rf"(?P<first>{_DAY})\s*(?:to|through|thru|[-–])\s*(?P<last>{_DAY})\b", re.I)
_ADD = re.compile(rf"\b{_WORK}\s+(?:now\s+)?(?:also\s+)?include\s+(?P<day>{_DAY}|weekends?)\b", re.I)
_REMOVE = re.compile(rf"\b{_WORK}\s+(?:now\s+)?exclude\s+(?P<day>{_DAY}|weekends?)\b", re.I)
_CLOSED = re.compile(rf"\b(?P<only>only\s+)?(?P<day>{_DAY}|weekends?)\b"
                     r".{0,55}?\b(?:are|is)\s+(?:now\s+)?closed\b", re.I)
_OPEN = re.compile(rf"\b(?P<day>{_DAY}|weekends?)\b"
                   rf"\s+(?:are|is)\s+(?:now\s+)?(?:a\s+)?{_WORK}\b", re.I)
_CALENDAR_DECLARATION = re.compile(
    rf"\b{_WORK}\s+(?:now\s+)?(?:also\s+)?(?:are|run|fall|extend|span|include|exclude|change|vary)\b|"
    rf"\b(?:only\s+)?{_DAY}\b.{0,55}\b(?:open|closed|business\s+day|work\s*day)\b", re.I)


def business_weekdays(policy: dict) -> tuple[int, ...]:
    """Return Monday=0 through Sunday=6 weekdays; reject unclear source rules.

    A later statement replaces or edits the earlier calendar. The ordinary
    Monday-Friday week is the fallback when the brief gives no weekday rule.
    """
    source = str(policy.get("raw_brief") or "")
    if not source:
        source = "\n".join(str(policy.get(key) or "") for key in
                           ("global_rules", "opening", "verified_call"))
    weekdays = set(range(5))
    for clause in re.split(r"[.;\n]+", source):
        if not _CALENDAR_DECLARATION.search(clause):
            continue
        if re.search(r"\b(?:sometimes|depending|either|perhaps|maybe|might|may|var(?:y|ies)|or\s+"
                     rf"{_DAY})\b", clause, re.I):
            raise ValueError("Ambiguous business weekday calendar in source brief")
        events = []
        for match in _RANGE.finditer(clause):
            first, last = _DAYS.index(match["first"].lower()), _DAYS.index(match["last"].lower())
            if first > last:
                raise ValueError("Ambiguous business weekday range in source brief")
            events.append((match.start(), "set", set(range(first, last + 1))))
        for pattern, action in ((_ADD, "add"), (_REMOVE, "remove"),
                                (_CLOSED, "closed"), (_OPEN, "add")):
            for match in pattern.finditer(clause):
                day = match["day"].lower()
                selected = {5, 6} if day.startswith("weekend") else {_DAYS.index(day)}
                kind = "set" if action == "closed" and match.groupdict().get("only") else action
                if kind == "set":
                    selected = set(range(7)) - selected
                events.append((match.start(), kind, selected))
        if not events:
            if re.search(r"\b\d{4}-\d{2}-\d{2}\b", clause):
                continue
            raise ValueError("Cannot establish business weekdays from source brief")
        for _, action, selected in sorted(events, key=lambda item: item[0]):
            if action == "set":
                weekdays = set(selected)
            elif action == "add":
                weekdays.update(selected)
            else:
                weekdays.difference_update(selected)
        if not weekdays:
            raise ValueError("Business weekday calendar has no open days")
    return tuple(sorted(weekdays))

"""Extract callback limits and validate a proposed callback before a bank write."""

from __future__ import annotations

import datetime as dt
import re


_CLOCK = r"(?:[01]?\d|2[0-3]):[0-5]\d"
_HOURS = re.compile(rf"\b(?:from|between)\s+(?P<start>{_CLOCK})\s*(?:to|and|through|[-–])\s*(?P<end>{_CLOCK})\b", re.I)
_HOURS_SHORT = re.compile(rf"\b(?P<start>{_CLOCK})\s*(?:to|and|through|[-–])\s*(?P<end>{_CLOCK})\b", re.I)
_HORIZON = re.compile(r"\btoday\s+(?:or|and)\s+(?:the\s+)?next\s+"
                      r"(?:(?P<count>\d+|one|two|three|four)\s+)?business\s+days?\b", re.I)
_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4}


def callback_window(source: str) -> dict[str, object] | None:
    """Return the latest fully specified callback window, or no authorization."""
    days: int | None = None
    start: str | None = None
    end: str | None = None
    inclusive = False
    seen_rule = False
    for line in source.splitlines():
        if not re.search(r"\bcallbacks?\b", line, re.I):
            continue
        horizon = _HORIZON.search(line)
        hours = _HOURS.search(line) or _HOURS_SHORT.search(line)
        if not horizon and not hours:
            if re.search(r"\b(?:window|hours?|business\s+days?|today|tomorrow)\b", line, re.I):
                raise ValueError("Cannot establish callback window from source")
            continue
        seen_rule = True
        if horizon:
            count = horizon["count"]
            days = int(count) if count and count.isdecimal() else _NUMBERS.get((count or "one").lower(), 1)
        if hours:
            start, end = hours["start"], hours["end"]
            inclusive = bool(re.search(
                r"\b(?:both\s+(?:included|inclusive)|inclusive(?:\s+at\s+both\s+ends)?)\b", line, re.I))
        if days is not None and (days < 1 or days > 7) or (start and end and start >= end):
            raise ValueError("Invalid callback day horizon or time window in source")
    if not seen_rule:
        return None
    if days is None or start is None or end is None:
        raise ValueError("Callback source must state both the day horizon and time window")
    if not inclusive:
        raise ValueError("Callback source must say whether time-window endpoints are included")
    return {"business_days": days, "start": start, "end": end}


def callback_request_fields(schema: dict) -> dict[str, str]:
    """Identify one callback date and one time from the request schema."""
    found: dict[str, list[str]] = {"date": [], "time": []}
    for field, spec in (schema.get("properties") or {}).items():
        if not isinstance(spec, dict):
            continue
        words = set(re.sub(r"[^a-z0-9]+", "_", field.lower()).strip("_").split("_"))
        description = str(spec.get("description") or "").lower()
        if spec.get("format") == "date" or words & {"date", "day"}:
            found["date"].append(field)
        elif words & {"time", "slot", "hour"} or "hh:mm" in description:
            found["time"].append(field)
    if any(len(fields) != 1 for fields in found.values()):
        raise ValueError(f"schedule_callback request needs one date and time field: {found}")
    return {role: fields[0] for role, fields in found.items()}


def validate_callback(day_text: str, time_text: str, now_text: str,
                      holidays: list[str], weekdays: tuple[int, ...],
                      window: dict[str, object] | None) -> None:
    """Raise before the bank call unless the proposed local day/time is allowed."""
    if window is None:
        raise ValueError("Callback window is not established by the source brief")
    now = dt.datetime.fromisoformat(str(now_text).replace("Z", "+00:00"))
    day = dt.date.fromisoformat(day_text)
    if not re.fullmatch(r"\d{2}:\d{2}", time_text):
        raise ValueError("Callback time must be HH:MM")
    time = dt.time.fromisoformat(time_text)
    start = dt.time.fromisoformat(str(window["start"]))
    end = dt.time.fromisoformat(str(window["end"]))
    if not start <= time <= end:
        raise ValueError("Callback time is outside the source window")
    if day.weekday() not in weekdays or day.isoformat() in holidays:
        raise ValueError("Callback day is not a business day")
    horizon = now.date()
    for _ in range(int(window["business_days"])):
        horizon += dt.timedelta(days=1)
        while horizon.weekday() not in weekdays or horizon.isoformat() in holidays:
            horizon += dt.timedelta(days=1)
    if not now.date() <= day <= horizon:
        raise ValueError("Callback day is beyond the source horizon")
    if day == now.date() and time <= now.time().replace(tzinfo=None):
        raise ValueError("Callback time must be later than now")


def callback_guard_code(date_field: str, time_field: str, holidays: list[str],
                        weekdays: tuple[int, ...], window: dict[str, object] | None) -> str:
    """Embed the same validator in a standalone Freya pre-process hook."""
    import inspect

    source = inspect.getsource(validate_callback)
    source = source.replace("dt.datetime", "datetime.datetime").replace(
        "dt.date", "datetime.date").replace("dt.time", "datetime.time").replace(
        "dt.timedelta", "datetime.timedelta")
    return ("import datetime\nimport re\n" + source + "\n"
            "body = config.get('body')\n"
            "if not isinstance(body, dict):\n"
            "    raise ValueError('Callback body is missing')\n"
            f"validate_callback(str(body.get({date_field!r}) or ''), "
            f"str(body.get({time_field!r}) or ''), str(vars.get('now') or ''), "
            f"{holidays!r}, {weekdays!r}, {window!r})\n")

"""Read overdue-band rules from source Markdown tables without guessing units."""

from __future__ import annotations

import re


_NUMBERS = {
    "zero": 0, "one": 1, "first": 1, "two": 2, "second": 2,
    "three": 3, "third": 3, "four": 4, "fourth": 4,
    "five": 5, "fifth": 5, "six": 6, "sixth": 6,
    "seven": 7, "seventh": 7, "eight": 8, "eighth": 8,
    "nine": 9, "ninth": 9, "ten": 10, "tenth": 10,
    "eleven": 11, "eleventh": 11, "twelve": 12, "twelfth": 12,
    "thirteen": 13, "thirteenth": 13, "fourteen": 14, "fourteenth": 14,
    "fifteen": 15, "fifteenth": 15,
}


def _number(text: str) -> int | None:
    value = text.strip().lower()
    digit = re.fullmatch(r"(\d+)(?:st|nd|rd|th)?", value)
    if digit:
        return int(digit.group(1))
    return _NUMBERS.get(value)


def _cells(line: str) -> list[str] | None:
    text = line.strip()
    if not text.startswith("|") or not text.endswith("|"):
        return None
    return [cell.strip().strip("`") for cell in text[1:-1].split("|")]


def _separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _column(header: list[str], role: str) -> int | None:
    found = []
    for index, cell in enumerate(header):
        words = set(re.findall(r"[a-z]+", cell.lower()))
        if role == "age" and (
                ("days" in words and words & {"late", "overdue", "past", "arrears"}) or
                "days_late" in cell.lower().replace("`", "")):
            found.append(index)
        elif role == "deadline" and words & {"deadline", "due", "limit"}:
            found.append(index)
        elif role == "attempts" and words & {"attempt", "attempts", "persuasion"}:
            found.append(index)
        elif role == "label" and words & {"band", "category", "stage"}:
            found.append(index)
    if len(found) > 1:
        raise ValueError(f"Ambiguous overdue table {role} columns")
    return found[0] if found else None


def _range(text: str) -> tuple[int, int] | None:
    match = re.fullmatch(
        r"\s*(\d+)\s*(?:to|through|[-–—])\s*(\d+)\s*(?:days?\s*(?:late|overdue)?)?\s*",
        text, re.I,
    )
    if not match:
        return None
    low, high = map(int, match.groups())
    return (low, high) if 0 <= low <= high else None


def _business_days(cell: str, header: str) -> int | None:
    value = cell.strip().lower()
    header_business = bool(re.search(r"\bbusiness\s+days?\b", header, re.I))
    # A numeric cell needs its unit in the header. A prose cell carries its own.
    if header_business:
        number = _number(value)
        if number is not None:
            return number if number > 0 else None
    match = re.fullmatch(
        r"(?:the\s+)?(\d+(?:st|nd|rd|th)?|[a-z]+)\s+business\s+days?"
        r"(?:\s+after\s+(?:today|the\s+call(?:\s+date)?))?",
        value, re.I,
    )
    if match:
        number = _number(match.group(1))
        return number if number and number > 0 else None
    return None


def _attempts(cell: str) -> int | None:
    match = re.fullmatch(r"(\d+|[a-z]+)(?:\s+(?:persuasion\s+)?attempts?)?", cell.strip(), re.I)
    return _number(match.group(1)) if match else None


def parse_deadline_tables(source: str) -> list[tuple[int, dict]]:
    """Return (source offset, band) rows from recognized deadline tables.

    A recognized table with an incomplete or ambiguous data row is an error.
    Later rows retain later offsets so a later memo can override an earlier row.
    """
    lines = source.splitlines(keepends=True)
    offsets = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line)
    found: list[tuple[int, dict]] = []
    index = 0
    while index < len(lines):
        header = _cells(lines[index])
        if header is None:
            index += 1
            continue
        try:
            columns = {role: _column(header, role) for role in
                       ("age", "deadline", "attempts", "label")}
        except ValueError:
            raise
        if any(columns[role] is None for role in ("age", "deadline", "attempts")):
            index += 1
            continue
        if index + 1 >= len(lines) or _cells(lines[index + 1]) is None:
            raise ValueError("Overdue deadline table has no data rows")
        # Plain-text bank extracts sometimes omit Markdown's separator row.
        index += 2 if _separator(_cells(lines[index + 1]) or []) else 1
        rows = 0
        while index < len(lines):
            cells = _cells(lines[index])
            if cells is None:
                break
            if len(cells) != len(header) or _separator(cells):
                raise ValueError("Ambiguous overdue deadline table row")
            age = _range(cells[columns["age"]])
            days = _business_days(cells[columns["deadline"]], header[columns["deadline"]])
            attempts = _attempts(cells[columns["attempts"]])
            if age is None or days is None or attempts is None or attempts < 0:
                raise ValueError("Cannot establish source deadline and persuasion attempts from overdue table row")
            label_index = columns["label"]
            label = cells[label_index] if label_index is not None else ""
            found.append((offsets[index], {"low": age[0], "high": age[1], "label": label,
                                           "business_days": days, "attempts": attempts}))
            rows += 1
            index += 1
        if not rows:
            raise ValueError("Overdue deadline table has no data rows")
    return found

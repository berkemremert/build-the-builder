"""Interpret caller payment-date words using the call date, not the host clock.

The generator embeds ``parse_payment_date`` in a Freya code node, so keep its
runtime dependencies limited to ``datetime`` and ``re`` imported by that node.
"""

from __future__ import annotations

import datetime
import re


def parse_payment_date(raw: str, today: datetime.date) -> datetime.date:
    """Resolve a specific spoken date; leave policy and business-day checks to the caller."""
    phrase = str(raw).strip().lower()
    corrections = list(re.finditer(
        r"\b(?:sorry(?:\s*,?\s*i\s+mean)?|i\s+meant?|actually|rather|instead|"
        r"correction|make\s+that|wait(?:\s*,?\s*no)?|no(?:\s*,?\s*i\s+mean)?)\b",
        phrase))
    if corrections:
        phrase = phrase[corrections[-1].end():].strip(" ,.;:")
        if not phrase:
            raise ValueError("Corrected payment date missing")
    if re.search(r"\bor\b", phrase):
        raise ValueError("Ambiguous payment dates")
    if phrase in {"today", "now"}:
        return today
    if phrase == "tomorrow":
        return today + datetime.timedelta(days=1)

    iso_matches = list(re.finditer(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", phrase))
    if len(iso_matches) > 1:
        raise ValueError("Ambiguous payment dates")
    if iso_matches:
        return datetime.date.fromisoformat(iso_matches[0].group())
    phrase = re.sub(r"[\s-]+", " ", phrase)

    months = ("january", "february", "march", "april", "may", "june",
              "july", "august", "september", "october", "november", "december")
    weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    named_days = dict((name, index + 1) for index, name in enumerate((
        "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
        "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth",
        "eighteenth", "nineteenth", "twentieth", "twenty first", "twenty second", "twenty third",
        "twenty fourth", "twenty fifth", "twenty sixth", "twenty seventh", "twenty eighth",
        "twenty ninth", "thirtieth", "thirty first")))
    month_pattern = "|".join(months)
    day_pattern = "|".join(re.escape(name) for name in sorted(named_days, key=len, reverse=True))
    day_pattern += r"|[0-3]?\d(?:st|nd|rd|th)?"
    month_matches = list(re.finditer(r"\b(" + month_pattern + r")\b", phrase))
    weekday_matches = list(re.finditer(r"\b(" + "|".join(weekdays) + r")\b", phrase))
    if len({match.group() for match in month_matches}) > 1 or len({match.group() for match in weekday_matches}) > 1:
        raise ValueError("Conflicting payment dates")
    month_match = month_matches[0] if month_matches else None
    weekday_match = weekday_matches[0] if weekday_matches else None
    month = months.index(month_match.group()) + 1 if month_match else None
    weekday = weekdays.index(weekday_match.group()) if weekday_match else None

    day_match = None
    if month_match:
        date_matches = (list(re.finditer(r"\b(?:the\s+)?(?P<day>" + day_pattern +
                                         r")(?:\s+of)?\s+(?:" + month_pattern + r")\b", phrase)) +
                        list(re.finditer(r"\b(?:" + month_pattern + r")\s+(?:the\s+)?(?P<day>" +
                                         day_pattern + r")\b", phrase)))
        if len(date_matches) > 1:
            raise ValueError("Ambiguous payment dates")
        day_match = date_matches[0] if date_matches else None
    elif weekday_match:
        day_match = re.search(r"\b(?:the\s+)?(?P<day>" + day_pattern + r")\b", phrase)

    if day_match:
        day_text = day_match.group("day")
        if re.search(r"\b(?:twenty|thirty)\s+$", phrase[:day_match.start("day")]):
            raise ValueError("Invalid payment day")
        day_number = named_days.get(day_text)
        if day_number is None:
            day_number = int(re.match(r"\d+", day_text).group())
        if not 1 <= day_number <= 31:
            raise ValueError("Invalid payment day")

        digit_years = {int(value) for value in re.findall(r"(?<!\d)(?:19|20|21)\d{2}(?!\d)", phrase)}
        ones = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                "six": 6, "seven": 7, "eight": 8, "nine": 9}
        tens = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
                "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
        word_years = set()
        for match in re.finditer(
                r"\b(twenty|nineteen)\s+(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)"
                r"(?:\s+(one|two|three|four|five|six|seven|eight|nine))?\b", phrase):
            word_years.add((2000 if match.group(1) == "twenty" else 1900) +
                           tens[match.group(2)] + ones.get(match.group(3), 0))
        for match in re.finditer(
                r"\btwo\s+thousand(?:\s+and)?(?:\s+(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety))?"
                r"(?:\s+(one|two|three|four|five|six|seven|eight|nine))?\b", phrase):
            word_years.add(2000 + tens.get(match.group(1), 0) + ones.get(match.group(2), 0))
        years = digit_years | word_years
        if len(years) > 1:
            raise ValueError("Conflicting payment years")
        explicit_year = next(iter(years)) if years else None
        if explicit_year is not None:
            candidate = datetime.date(explicit_year, month or today.month, day_number)
        elif month is not None:
            candidate = None
            for year in range(today.year, today.year + 9):
                try:
                    possible = datetime.date(year, month, day_number)
                except ValueError:
                    continue
                if possible >= today:
                    candidate = possible
                    break
            if candidate is None:
                raise ValueError("Invalid payment date")
        else:
            candidate = None
            for offset in range(13):
                year = today.year + (today.month - 1 + offset) // 12
                current_month = (today.month - 1 + offset) % 12 + 1
                try:
                    possible = datetime.date(year, current_month, day_number)
                except ValueError:
                    continue
                if possible >= today:
                    candidate = possible
                    break
            if candidate is None:
                raise ValueError("Invalid payment date")
        if weekday is not None and candidate.weekday() != weekday:
            raise ValueError("Day and weekday disagree")
        return candidate

    if weekday is not None:
        if re.search(r"\bnext\s+" + weekdays[weekday] + r"\b", phrase):
            offset = 7 - today.weekday() + weekday
        elif re.search(r"\bthis\s+" + weekdays[weekday] + r"\b", phrase):
            offset = weekday - today.weekday()
        else:
            offset = (weekday - today.weekday()) % 7
        return today + datetime.timedelta(days=offset)

    raise ValueError("Payment date missing")

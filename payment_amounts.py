"""Strict parsing of an extracted payment amount, including spoken money.

The function is self-contained so its source can also run inside a generated
Freya code node. It does not choose a product floor or past-due amount: callers
must apply those policy bounds after parsing.
"""

from __future__ import annotations


def parse_payment_amount(value):
    """Return a finite amount in pounds/dollars, or raise ValueError.

    An explicit self-correction replaces the earlier complete amount. A vague
    alternative or unfinished phrase never produces a guessed amount.
    """
    import decimal
    import math
    import re

    def fail():
        raise ValueError("Unclear payment amount")

    if isinstance(value, bool) or value is None:
        fail()
    if isinstance(value, (int, float, decimal.Decimal)):
        try:
            number = decimal.Decimal(str(value))
        except decimal.InvalidOperation:
            fail()
        if not number.is_finite() or number < 0 or number.as_tuple().exponent < -2:
            fail()
        result = float(number)
        if not math.isfinite(result):
            fail()
        return result
    if not isinstance(value, str):
        fail()
    text = value.strip().lower().replace("−", "-")
    if not text:
        fail()
    correction = re.search(r"\s*,?\s*\b(?:sorry(?:\s*,?\s*i mean)?|i mean|actually|rather)\b\s*,?\s*", text)
    if correction:
        before, after = text[:correction.start()].strip(" ,"), text[correction.end():].strip(" ,")
        if not before or not after or re.search(r"\b(?:sorry|i mean|actually|rather)\b", after):
            fail()
        parse_payment_amount(before)
        return parse_payment_amount(after)
    if re.search(r"\b(?:or|about|around|maybe|approximately|roughly)\b|\.{2,}", text):
        fail()

    numeric = re.fullmatch(r"(?:£|\$)?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?\s*(?:pounds?|dollars?)?", text)
    if numeric:
        result = float(decimal.Decimal(numeric.group(1).replace(",", "") +
                                       ("." + numeric.group(2) if numeric.group(2) else "")))
        if not math.isfinite(result):
            fail()
        return result
    if re.search(r"[£$]|\d|[^a-z\s,\-]", text):
        fail()
    words = re.findall(r"[a-z]+", text.replace("-", " "))
    if not words:
        fail()

    ones = dict(zero=0, one=1, two=2, three=3, four=4, five=5,
                six=6, seven=7, eight=8, nine=9, ten=10, eleven=11,
                twelve=12, thirteen=13, fourteen=14, fifteen=15,
                sixteen=16, seventeen=17, eighteen=18, nineteen=19)
    tens = dict(twenty=20, thirty=30, forty=40, fifty=50,
                sixty=60, seventy=70, eighty=80, ninety=90)

    def under_hundred(part):
        if not part:
            fail()
        if len(part) == 1:
            if part[0] == "a":
                return 1
            if part[0] in ones:
                return ones[part[0]]
            if part[0] in tens:
                return tens[part[0]]
            fail()
        if len(part) == 2 and part[0] in tens and part[1] in ones and ones[part[1]] in range(1, 10):
            return tens[part[0]] + ones[part[1]]
        fail()

    def group(part):
        if not part:
            fail()
        if part[0] == "and" or part[-1] == "and" or part.count("and") > 1:
            fail()
        if "hundred" not in part:
            if "and" in part:
                fail()
            return under_hundred(part)
        if part.count("hundred") != 1:
            fail()
        index = part.index("hundred")
        if index != 1 or part[0] not in (*ones.keys(), "a") or (part[0] in ones and ones[part[0]] not in range(1, 10)):
            fail()
        result = (1 if part[0] == "a" else ones[part[0]]) * 100
        rest = part[index + 1:]
        if rest and rest[0] == "and":
            rest = rest[1:]
            if not rest:
                fail()
        return result + (under_hundred(rest) if rest else 0)

    def cardinal(part):
        if not part or part == ["a"]:
            fail()
        total = 0
        last_scale = 1_000_000_000
        start = 0
        for index, word in enumerate(part):
            if word not in ("million", "thousand"):
                continue
            scale = 1_000_000 if word == "million" else 1_000
            if scale >= last_scale:
                fail()
            total += group(part[start:index]) * scale
            last_scale = scale
            start = index + 1
            if start < len(part) and part[start] == "and":
                start += 1
                if start == len(part):
                    fail()
        if start < len(part):
            total += group(part[start:])
        elif total == 0:
            fail()
        return total

    major_markers = ("pound", "pounds", "dollar", "dollars")
    minor_markers = ("penny", "pennies", "pence", "cent", "cents")
    major_positions = [i for i, word in enumerate(words) if word in major_markers]
    minor_positions = [i for i, word in enumerate(words) if word in minor_markers]
    if len(major_positions) > 1 or len(minor_positions) > 1:
        fail()
    if major_positions:
        major_at = major_positions[0]
        major = cardinal(words[:major_at])
        tail = words[major_at + 1:]
        if not tail:
            return float(major)
        if tail[0] == "and":
            tail = tail[1:]
        if not tail or tail[-1] not in minor_markers or len(minor_positions) != 1:
            fail()
        minor = cardinal(tail[:-1])
        if minor >= 100:
            fail()
        return float(decimal.Decimal(major) + decimal.Decimal(minor) / 100)
    if minor_positions:
        if minor_positions[0] != len(words) - 1:
            fail()
        minor = cardinal(words[:-1])
        if minor >= 100:
            fail()
        return float(decimal.Decimal(minor) / 100)
    return float(cardinal(words))

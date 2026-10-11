"""Read explicit queue and outcome codes from a bank brief.

Only a labeled assignment or a table with a code/queue column counts as an
explicit mapping. A later amendment may replace an earlier mapping; conflicting
codes in the same revision are ambiguous and must not be guessed.
"""

from __future__ import annotations

import re


_ROLES = {
    "queue": {
        "support": "support", "customer support": "support", "hardship": "support",
        "human": "human", "human agent": "human", "general": "human",
        "restructuring": "restructuring", "repayment plan": "restructuring",
    },
    "outcome": {
        "promise": "promise", "paid": "paid", "transfer": "transferred",
        "transferred": "transferred", "no promise": "no_promise",
        "no_promise": "no_promise", "identity failure": "not_verified",
        "not verified": "not_verified", "not_verified": "not_verified",
        "not reached": "not_reached", "not_reached": "not_reached",
    },
}
_AMENDMENT = re.compile(r"\b(?:later memo|amendment|updated? policy|supersed\w*|overrid\w*|replac\w*)\b", re.I)
_ASSIGNMENT = re.compile(r"(?<![\w])([A-Za-z][A-Za-z_ -]{0,30}?)\s*[:=]\s*`?([A-Za-z0-9_-]+)`?(?=\W|$)")


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip(" `*_").lower().replace("-", " "))


def _table_columns(cells: list[str], kind: str) -> tuple[int, int] | None:
    headers = [_normalize(cell) for cell in cells]
    role_headers = {"purpose", "role", "reason", "use", "outcome"}
    code_headers = {"queue", "queue code"} if kind == "queue" else {"outcome code", "close outcome code", "code"}
    role_index = next((i for i, cell in enumerate(headers) if cell in role_headers), None)
    code_index = next((i for i, cell in enumerate(headers) if cell in code_headers), None)
    return (role_index, code_index) if role_index is not None and code_index is not None else None


def explicit_code_mappings(source: str, kind: str, allowed: list[str]) -> dict[str, str]:
    """Return source-labeled codes validated against an OpenAPI enum.

    ``kind`` is ``queue`` or ``outcome``. Unknown labels are ignored, but a
    recognized label with an invalid or conflicting code raises ValueError.
    """
    if kind not in _ROLES:
        raise ValueError(f"Unsupported code kind {kind!r}")
    aliases = _ROLES[kind]
    allowed_set = set(allowed)
    found: dict[str, tuple[str, int]] = {}
    revision = 0
    columns: tuple[int, int] | None = None

    def record(label: str, code: str) -> None:
        role = aliases.get(_normalize(label))
        if role is None:
            return
        if code not in allowed_set:
            raise ValueError(f"Source maps {kind} {role} to {code!r}, absent from OpenAPI enum {allowed!r}")
        prior = found.get(role)
        if prior and prior[0] != code and prior[1] == revision:
            raise ValueError(f"Ambiguous {kind} code for {role}: {prior[0]!r} and {code!r}")
        if prior is None or revision >= prior[1]:
            found[role] = (code, revision)

    for line in source.splitlines():
        if _AMENDMENT.search(line):
            revision += 1
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [cell.strip().strip("` ") for cell in stripped.strip("|").split("|")]
            header = _table_columns(cells, kind)
            if header is not None:
                columns = header
                continue
            if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                continue
            if columns is not None and max(columns) < len(cells):
                record(cells[columns[0]], cells[columns[1]])
            continue
        columns = None
        for match in _ASSIGNMENT.finditer(line):
            record(match.group(1), match.group(2))
    return {role: code for role, (code, _) in found.items()}

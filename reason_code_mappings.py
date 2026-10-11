"""Read source-labeled note and close reason codes without guessing opaque enums."""

from __future__ import annotations

import re


_AMENDMENT = re.compile(r"\b(?:later memo|amendment|updated? policy|supersed\w*|overrid\w*|replac\w*)\b", re.I)
_CODE_LIST = re.compile(r"\b(?:(close|note|add_note)\s+)?reason\s+code\s+list\s*:", re.I)
_ASSIGNMENT = re.compile(r"\b([a-z][a-z_ -]{0,25}?)\s*[:=]\s*`?([A-Za-z0-9_-]+)`?(?=\W|$)", re.I)
_ALIASES = {
    "service error": "service_error", "wrong number": "wrong_number",
    "no callback": "no_callback", "identity failed": "failed",
    "identity failure": "failed", "already paid": "paid",
}


def _role(label: str) -> str:
    text = re.sub(r"\s+", " ", label.strip(" `*_").lower().replace("-", " "))
    return _ALIASES.get(text, text.replace(" ", "_"))


def explicit_reason_codes(source: str, note_allowed: list[str],
                          close_allowed: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Return (note, close) mappings from explicit code lists and Markdown tables.

    A scope-specific table validates only against its corresponding API enum.
    A shared reason code list applies where its code belongs to that enum; if
    neither enum contains it, the source/API combination is inconsistent.
    """
    allowed = {"note": set(note_allowed), "close": set(close_allowed)}
    found: dict[str, dict[str, tuple[str, int]]] = {"note": {}, "close": {}}
    revision = 0
    table: tuple[int, int, tuple[str, ...]] | None = None

    def record(label: str, code: str, scopes: tuple[str, ...], *, shared: bool = False) -> None:
        role = _role(label)
        if not re.fullmatch(r"[a-z][a-z0-9_]*", role):
            return
        applicable = [scope for scope in scopes if not allowed[scope] or code in allowed[scope]]
        if not applicable:
            if shared:
                raise ValueError(f"Reason code {code!r} for {role} is absent from note and close_call enums")
            raise ValueError(f"Reason code {code!r} for {role} is absent from {scopes[0]} enum")
        for scope in applicable:
            prior = found[scope].get(role)
            if prior and prior[0] != code and prior[1] == revision:
                raise ValueError(f"Ambiguous {scope} reason code for {role}: {prior[0]!r} and {code!r}")
            if prior is None or revision >= prior[1]:
                found[scope][role] = (code, revision)

    for line in source.splitlines():
        if _AMENDMENT.search(line):
            revision += 1
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [cell.strip(" `*") for cell in stripped.strip("|").split("|")]
            headers = [re.sub(r"\s+", " ", cell.lower()) for cell in cells]
            if "reason" in headers:
                code_columns = [(i, "close" if "close" in header else "note" if "note" in header else "both")
                                for i, header in enumerate(headers)
                                if header in {"close code", "close reason code", "note code",
                                              "note reason code", "reason code", "code"}]
                if len(code_columns) == 1:
                    code_index, scope = code_columns[0]
                    scopes = ("note", "close") if scope == "both" else (scope,)
                    table = (headers.index("reason"), code_index, scopes)
                    continue
            if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                continue
            if table and max(table[:2]) < len(cells):
                record(cells[table[0]], cells[table[1]], table[2], shared=len(table[2]) == 2)
            continue
        table = None
        match = _CODE_LIST.search(line)
        if not match:
            continue
        label = (match.group(1) or "").lower()
        scopes = ("close",) if label == "close" else ("note",) if label in {"note", "add_note"} else ("note", "close")
        for item in _ASSIGNMENT.finditer(line[match.end():]):
            record(item.group(1), item.group(2), scopes, shared=len(scopes) == 2)
    return ({role: code for role, (code, _) in found["note"].items()},
            {role: code for role, (code, _) in found["close"].items()})

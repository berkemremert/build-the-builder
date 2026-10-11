"""Extract an exact post-recording sentence from policy source."""

from __future__ import annotations

import re


_CONTEXT = re.compile(r"\b(?:recorded?\s+promise|promise\s+(?:is\s+)?recorded|"
                      r"recording\s+the\s+promise|successful(?:ly)?\s+(?:promise\s+)?recording)\b", re.I)
_DIRECTIVE = re.compile(r"\b(?:say|tell|read|speak|use|wording|sentence|message|script)\b", re.I)
_EXACT = re.compile(r"\b(?:verbatim|exactly|exact|word.for.word|this sentence|required wording)\b", re.I)
_QUOTE = re.compile(r'["“]([^"”\n]+)["”]')
_AMENDMENT = re.compile(r"\b(?:later memo|amendment|updated? policy|supersed\w*|overrid\w*|replac\w*)\b", re.I)


def exact_success_sentence(source: str) -> str | None:
    """Find source-prescribed words to say after a recorded promise.

    A later amendment can replace an earlier instruction. Conflicting wording
    in the same revision is ambiguous and prevents a generated claim.
    """
    found: tuple[str, int] | None = None
    revision = 0
    table: tuple[int, int] | None = None

    def record(sentence: str) -> None:
        nonlocal found
        sentence = sentence.strip()
        if not sentence:
            return
        if found and found[1] == revision and found[0] != sentence:
            raise ValueError("Ambiguous recorded-promise success sentence in policy source")
        if found is None or revision >= found[1]:
            found = (sentence, revision)

    for line in source.splitlines():
        if _AMENDMENT.search(line):
            revision += 1
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [cell.strip(" `*") for cell in stripped.strip("|").split("|")]
            headers = [re.sub(r"\s+", " ", cell.lower()) for cell in cells]
            if any(header in {"situation", "event", "when", "trigger", "step"} for header in headers):
                context_index = next(i for i, header in enumerate(headers)
                                     if header in {"situation", "event", "when", "trigger", "step"})
                speech = [i for i, header in enumerate(headers)
                          if header in {"required words", "exact words", "verbatim message",
                                        "exact sentence", "required sentence", "say exactly"}]
                if len(speech) == 1:
                    table = (context_index, speech[0])
                    continue
            if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                continue
            if table and max(table) < len(cells) and _CONTEXT.search(cells[table[0]]):
                speech = cells[table[1]].strip('"“”')
                record(speech)
            continue
        table = None
        if _CONTEXT.search(line) and _DIRECTIVE.search(line) and _EXACT.search(line):
            quotes = _QUOTE.findall(line)
            if len(quotes) == 1:
                record(quotes[0])
            elif len(quotes) > 1:
                raise ValueError("Ambiguous recorded-promise success sentence in policy source")
    return found[0] if found else None

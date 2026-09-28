import re
from collections.abc import Sequence
from typing import Any

from openreview_cli.parsing.models import Clause

_NUPUNKT: Any = None


def count_paragraphs(text: str) -> int:
    if not text.strip():
        return 1
    return max(1, len([p for p in text.split("\n\n") if p.strip()]))


_count_paragraphs = count_paragraphs  # ponytail: compat alias, remove after callers migrate


def _get_nupunkt() -> Any:
    global _NUPUNKT
    if _NUPUNKT is None:
        from nupunkt import sent_spans

        _NUPUNKT = sent_spans
    return _NUPUNKT


def nupunkt_detect_boundaries(text: str) -> list[Any]:
    sent_spans = _get_nupunkt()
    return sent_spans(text)  # type: ignore[no-any-return]


_NUMBERING_PATTERNS = [
    (r"^\s*(?:ARTICLE|Article|SECTION|Section)\s+[IVXLCDM]+\b", 0),
    (r"^\s*(?:ARTICLE|Article|SECTION|Section)\s+\d+\.\d+", 1),
    (r"^\s*(?:ARTICLE|Article|SECTION|Section)\s+\d+\b", 0),
    (r"^\s*(?:Clause|clause)\s+\d+(?:\.\d+)*", 0),
    (r"^\s*\d+(?:\.\d+)+\b", 1),
    (r"^\s*\d+\.\s", 0),
    (r"^\s*\([a-z]\)", 2),
    (r"^\s*\(\d+\)", 2),
    (r"^\s*\([ivxlcdm]+\)", 2),
]


def detect_numbering_pattern(line: str) -> dict[str, Any] | None:
    for pattern, level in _NUMBERING_PATTERNS:
        if re.match(pattern, line.strip()):
            return {"level": level, "pattern": pattern}
    return None


def _extract_numbering_level(line: str) -> int | None:
    """Return the numbering level of *line*, or None when it declares no number."""
    match = detect_numbering_pattern(line)
    return match["level"] if match else None


def detect_clause_starts(text: str) -> list[tuple[int, dict[str, Any]]]:
    starts: list[tuple[int, dict[str, Any]]] = []
    for match in re.finditer(
        r"(?m)^\s*(?:(?:ARTICLE|Article|SECTION|Section)\s+(?:[IVXLCDM]+|\d+)[:\s.]|(?:Clause|clause)\s+\d+|\d+\.(?:\d+\.)*\s|Section\s+\d+\.\d+|\([a-z]\)|\(\d+\)|\([ivxlcdm]+\))",
        text,
    ):
        starts.append((match.start(), {"level": 1, "pattern": "auto"}))
    return starts


_UNICODE_RANGES = {
    "Arabic": range(0x0600, 0x06FF + 1),
    "CJK": range(0x4E00, 0x9FFF + 1),
    "Cyrillic": range(0x0400, 0x04FF + 1),
}

_LANGUAGE_MAP = {
    "Arabic": "Arabic",
    "CJK": "Chinese/Japanese/Korean",
    "Cyrillic": "Russian/Ukrainian/Bulgarian",
}


def detect_non_english(text: str) -> str | None:
    for name, rng in _UNICODE_RANGES.items():
        for ch in text:
            if ord(ch) in rng:
                return _LANGUAGE_MAP.get(name, name)
    return None


def detect_tofu(text: str) -> bool:
    return "\ufffd" in text


def annotate_clauses(clauses: list[Clause]) -> list[str]:
    """Set ``Clause.is_non_english`` in place; return document-level parse warnings."""
    languages: set[str] = set()
    has_tofu = False
    for clause in clauses:
        language = detect_non_english(clause.text)
        clause.is_non_english = language is not None
        if language:
            languages.add(language)
        if not has_tofu and detect_tofu(clause.text):
            has_tofu = True

    warnings = [
        f"The contract appears to be in {language}. Results may be less accurate"
        for language in sorted(languages)
    ]
    if has_tofu:
        warnings.append("Some text could not be read correctly. Results may contain errors")
    return warnings


def build_hierarchy(
    boundaries: list[tuple[int, int]],
    clause_starts: list[tuple[int, dict[str, Any]]],
    headings: list[tuple[int, str, int]],
    page_num: int,
    start_counter: int,
    page_text: str = "",
) -> list[Clause]:
    clauses: list[Clause] = []
    counter = start_counter

    if not clause_starts and not headings:
        if boundaries:
            for start, end in boundaries:
                text = page_text[start:end].strip()
                if text:
                    clauses.append(
                        Clause(
                            id=f"clause-{counter}",
                            title=None,
                            text=text,
                            level=0,
                            parent_id=None,
                            source_page=page_num,
                            source_paragraph=None,
                            source_span=(start, end),
                            paragraph_count=count_paragraphs(text),
                        )
                    )
                    counter += 1
        return clauses

    sorted_starts = sorted(clause_starts, key=lambda x: x[0])
    for i, (start_pos, match) in enumerate(sorted_starts):
        end_pos = sorted_starts[i + 1][0] if i + 1 < len(sorted_starts) else len(page_text)
        text = page_text[start_pos:end_pos].strip()
        if text:
            clauses.append(
                Clause(
                    id=f"clause-{counter}",
                    title=None,
                    text=text,
                    level=match["level"] if isinstance(match, dict) else 1,
                    parent_id=None,
                    source_page=page_num,
                    source_paragraph=None,
                    source_span=(start_pos, end_pos),
                    paragraph_count=count_paragraphs(text),
                )
            )
            counter += 1

    return clauses


def link_parent_ids(
    clauses: Sequence[Clause],
    open_levels: list[tuple[int, str]],
    *,
    levels: Sequence[int | None] | None = None,
) -> None:
    """Set ``Clause.parent_id`` from each clause's numbering level (0 = top).

    ``levels`` supplies the level per clause (the DOCX parser passes its heading
    level where it has one; the PDF parser passes the numbering level). ``None``
    is an unlevelled clause: it attaches to the deepest open ancestor if one is
    open, and never becomes an ancestor itself. The stack is a parameter, not a
    local, so the PDF parser's page loop can carry it across pages -- a section
    that opens on page 3 must parent the clauses on page 4.
    """
    for index, clause in enumerate(clauses):
        level = levels[index] if levels is not None else None
        if level is None:
            clause.parent_id = open_levels[-1][1] if open_levels else None
            continue
        while open_levels and open_levels[-1][0] >= level:
            open_levels.pop()
        clause.parent_id = open_levels[-1][1] if open_levels else None
        open_levels.append((level, clause.id))

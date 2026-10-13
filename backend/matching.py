"""Course-code normalization and fuzzy matching for the schedule builder.

Students select courses from a DB-backed autocomplete (current term only), but
typed input must be normalized (case-insensitive, ignore spaces/hyphens) and,
when a typed code has no exact match, we surface the closest matches via
rapidfuzz instead of failing silently.

Examples:
    "mktg201" / "MKTG-201" / "MKTG 201"  ->  "MKTG 201"
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz, process

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")

# "MKTG201" -> dept "MKTG", catalog "201"; supports a trailing letter ("110R").
_DEPT_NUM_RE = re.compile(r"^([A-Z]+)(\d{2,4}[A-Z]?)$")


def collapse(raw: str) -> str:
    """Strip non-alphanumerics and uppercase: "mktg-201" -> "MKTG201"."""
    return _NON_ALNUM.sub("", raw).upper()


def normalize_course_code(raw: str) -> str:
    """Canonical "DEPT NUM" form. Case-insensitive, ignores spaces/hyphens.

    "mktg201" / "MKTG-201" / "MKTG 201" -> "MKTG 201".
    Unparseable input (e.g. a dept-only prefix while typing) is returned as the
    collapsed uppercase string so callers can still prefix-match on it.
    """
    cleaned = collapse(raw)
    m = _DEPT_NUM_RE.match(cleaned)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return cleaned


def parse_course_code(raw: str) -> tuple[str, str] | None:
    """Return (dept, catalog_number) or None when not parseable."""
    m = _DEPT_NUM_RE.match(collapse(raw))
    if not m:
        return None
    return m.group(1), m.group(2)


def normalize_name(raw: str) -> str:
    """Normalize an instructor name into a stable key for RMP matching."""
    return " ".join(_NON_ALNUM.sub(" ", raw).lower().split())


def _title_word(word: str) -> str:
    """Title-case a single name word, handling Mc/Mac prefixes."""
    if not word:
        return word
    w = word.lower()
    if w.startswith("mc") and len(w) > 2:
        return "Mc" + w[2:].capitalize()
    if w.startswith("mac") and len(w) > 4:
        return "Mac" + w[3:].capitalize()
    return w.capitalize()


def title_case_name(raw: str | None) -> str:
    """Sensible title case for a person's display name.

    Keeps it simple and predictable rather than implementing a full proper-name
    lexicon: each whitespace-, hyphen-, or apostrophe-separated part is
    capitalized, with a small fix-up for common Mc/Mac surnames.

    Examples
        "heidi dixon" -> "Heidi Dixon"
        "mcdonald"    -> "McDonald"
        "o'brien"     -> "O'Brien"
        "de la cruz"  -> "De La Cruz"
        "jean-luc"    -> "Jean-Luc"
    """
    if not raw or not raw.strip():
        return ""
    tokens: list[str] = []
    for token in raw.split():
        parts: list[str] = []
        for part in re.split(r"([-'])", token):
            if part in ("-", "'"):
                parts.append(part)
            elif part:
                parts.append(_title_word(part))
        tokens.append("".join(parts))
    return " ".join(tokens)


def closest_courses(
    query: str, candidates: list[str], limit: int = 5
) -> list[tuple[str, int]]:
    """Fuzzy-rank candidate course codes against a query -> [(code, score)].

    `candidates` must already be canonical ("MKTG 201"). Scores are 0-100.
    """
    if not candidates:
        return []
    norm = normalize_course_code(query)
    # process.extract may return 2- or 3-tuples depending on rapidfuzz version;
    # we only need (choice, score).
    out: list[tuple[str, int]] = []
    for item in process.extract(norm, candidates, scorer=fuzz.ratio, limit=limit):
        out.append((item[0], int(item[1])))
    return out


def match_course(
    query: str, candidates: list[str]
) -> tuple[str | None, list[tuple[str, int]]]:
    """Exact match on normalized code, else fuzzy suggestions.

    `candidates`: canonical course codes ("MKTG 201").
    Returns (exact_match_or_None, suggestions). `suggestions` is empty on an
    exact match.
    """
    norm = normalize_course_code(query)
    if norm in candidates:
        return norm, []
    return None, closest_courses(query, candidates)

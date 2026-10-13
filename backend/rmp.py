"""RateMyProfessors (RMP) lookup, name matching, and ease scoring.

RMP has no official public API. We use the same internal GraphQL endpoint its
own site calls (visible in the browser network tab):

    POST https://www.ratemyprofessors.com/graphql
    Authorization: Basic dGVzdDp0ZXN0   (public hardcoded token the site sends)
    body: {"query": ..., "variables": {"q": {"text": NAME, "schoolID": SCHOOL_ID}}}

BYU's school ID is ``U2Nob29sLTEzNQ==`` (base64 of "School-135"), confirmed via
the ``newSearch.schools`` query. We never scrape RMP broadly — only per-name
lookups for professors in our DB that are missing a match or stale.

CLI:
    python -m backend.rmp --refresh [--limit N] [--force]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import httpx
from rapidfuzz import fuzz

from . import database
from .matching import normalize_name

logger = logging.getLogger("freeroomfinder.rmp")

# ── Config constants ─────────────────────────────────────────────────────────
RMP_GRAPHQL_URL = "https://www.ratemyprofessors.com/graphql"
RMP_SCHOOL_ID = "U2Nob29sLTEzNQ=="          # Brigham Young University (Provo)
RMP_AUTH = "Basic dGVzdDp0ZXN0"              # public token RMP's own site sends
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
RATE_LIMIT_SECONDS = 1.0
REFRESH_AGE_DAYS = 120
CONFIDENCE_THRESHOLD = 0.8                 # below this -> store NULL ratings
AMBIGUITY_MARGIN = 0.20                    # top-2 gap below this -> ambiguous
MAX_RETRIES = 4                            # exponential backoff attempts
SEARCH_FIRST = 8                           # candidates returned per query


class RmpError(Exception):
    """Raised when RMP returns unexpected data or the lookup fails hard."""


# ── GraphQL ──────────────────────────────────────────────────────────────────

_TEACHER_QUERY = """
query TeacherSearch($q: TeacherSearchQuery!) {
  newSearch {
    teachers(query: $q, first: 8) {
      edges {
        node {
          id
          firstName
          lastName
          department
          avgRating
          avgDifficulty
          wouldTakeAgainPercent
          numRatings
          courseCodes { courseName courseCount }
          school { id name }
        }
      }
    }
  }
}
"""


def _teacher_query(name: str) -> dict:
    return {
        "query": _TEACHER_QUERY,
        "variables": {"q": {"text": name, "schoolID": RMP_SCHOOL_ID}},
    }


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": USER_AGENT,
            "Authorization": RMP_AUTH,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        follow_redirects=True,
    )


async def search_teachers(client: httpx.AsyncClient, name: str) -> list[dict]:
    """Search RMP for *name* restricted to BYU. Returns a list of node dicts.

    Raises RmpError on persistent failure or an unexpected payload shape so the
    caller can count it as an error and skip (never crash, never wipe ratings).
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.post(RMP_GRAPHQL_URL, json=_teacher_query(name))
            if resp.status_code in (429, 500, 502, 503, 504):
                # transient: back off and retry
                await asyncio.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            data = resp.json()
            teachers = (
                data.get("data", {})
                .get("newSearch", {})
                .get("teachers", {})
            )
            if teachers is None:
                return []  # legitimately zero results
            edges = teachers.get("edges")
            if not isinstance(edges, list):
                raise RmpError(f"unexpected RMP payload shape for {name!r}")
            return [
                e["node"]
                for e in edges
                if isinstance(e, dict) and isinstance(e.get("node"), dict)
            ]
        except httpx.HTTPStatusError as e:
            last_exc = e
            await asyncio.sleep(2 ** attempt)
        except (httpx.TransportError, json.JSONDecodeError) as e:
            last_exc = e
            await asyncio.sleep(2 ** attempt)
        except RmpError:
            raise
    raise RmpError(f"RMP lookup failed for {name!r}: {last_exc}")


# ── Name normalization & matching ────────────────────────────────────────────

# Suffixes we strip before matching (never part of a first/last name).
_SUFFIXES = {
    "jr", "sr", "ii", "iii", "iv", "v", "vi", "phd", "md", "dds", "edd",
    "dmd", "dvm", "dr", "prof", "professor", "ms", "ma", "ba", "bs",
}

# Common nickname -> canonical first name. Applied to BOTH sides before
# comparing, so "Bob" == "Robert" while "Robert" stays "Robert".
_NICKNAMES: dict[str, str] = {
    "bob": "robert", "rob": "robert", "bobby": "robert", "robby": "robert",
    "bill": "william", "will": "william", "billy": "william",
    "jim": "james", "jimmy": "james", "jamie": "james",
    "mike": "michael", "mikey": "michael",
    "dave": "david", "davey": "david",
    "dan": "daniel", "danny": "daniel",
    "chris": "christopher", "topher": "christopher",
    "matt": "matthew", "matty": "matthew",
    "ben": "benjamin", "benny": "benjamin",
    "brad": "bradley",
    "alex": "alexander", "al": "alexander",
    "sam": "samuel", "sammy": "samuel",
    "joe": "joseph", "joey": "joseph",
    "steve": "steven", "stephen": "steven",
    "jon": "jonathan", "john": "john", "johnny": "john", "jack": "john",
    "kate": "katherine", "katie": "katherine", "kathy": "katherine",
    "liz": "elizabeth", "lizzie": "elizabeth", "beth": "elizabeth",
    "meg": "margaret", "peggy": "margaret", "maggie": "margaret",
    "jenny": "jennifer", "jen": "jennifer",
    "sue": "susan", "susie": "susan",
    "tom": "thomas", "tommy": "thomas",
    "tim": "timothy", "timmy": "timothy",
    "pete": "peter",
    "rick": "richard", "rich": "richard", "dick": "richard",
    "jeff": "jeffrey",
    "andy": "andrew", "drew": "andrew",
    "tony": "anthony",
    "nick": "nicholas", "nicki": "nicholas",
    "nate": "nathan", "nathaniel": "nathan",
    "jake": "jacob",
    "zach": "zachary", "zack": "zachary",
    "josh": "joshua",
    "fred": "frederick", "freddy": "frederick",
    "chuck": "charles", "charlie": "charles",
    "greg": "gregory",
    "hank": "henry",
    "ted": "theodore", "teddy": "theodore",
    "ed": "edward", "eddie": "edward",
    "larry": "lawrence",
    "ron": "ronald",
    "pat": "patrick",
    "doug": "douglas",
}

# BYU teaching-area code -> RMP department name hint (best-effort tiebreaker).
_DEPT_ALIASES: dict[str, list[str]] = {
    "ACC": ["accounting"], "ACCT": ["accounting"],
    "MKTG": ["marketing", "business", "management", "entrepreneurship"],
    "FIN": ["finance"],
    "ISYS": ["information systems"], "CS": ["computer science"],
    "MATH": ["mathematics"], "STAT": ["statistics"],
    "PSYCH": ["psychology"], "BIO": ["biology"], "CHEM": ["chemistry"],
    "PHYS": ["physics"], "ENGL": ["english"], "HIST": ["history"],
    "ECON": ["economics"], "REL": ["religion"], "SPAN": ["spanish"],
    "SOC": ["sociology"], "ANTHR": ["anthropology"], "POLI": ["political science"],
    "COMMS": ["communications"], "JOUR": ["journalism"], "MUSIC": ["music"],
    "ART": ["art"], "FREN": ["french"], "CHIN": ["chinese"],
    "JAPN": ["japanese"], "KOR": ["korean"], "GERM": ["german"],
    "PHIL": ["philosophy"], "LING": ["linguistics"], "CJ": ["criminal justice"],
    "SWELL": ["exercise science"], "NUTR": ["nutrition"], "NURS": ["nursing"],
    "AM ST": ["american studies", "history"], "A HTG": ["history"],
    "BUS M": ["business", "marketing", "management"], "BUS": ["business"],
}

# RMP course-code dept prefix -> canonical BYU teaching-area code. RMP codes
# use inconsistent prefixes for the same department (SPA vs SPAN, PSY vs PSYCH,
# RELC vs REL, BUSM vs BUS). This maps them to a single canonical token so
# course overlap survives those quirks.
_COURSE_DEPT_ALIASES: dict[str, str] = {
    "SPA": "SPAN", "SPAN": "SPAN",
    "PSY": "PSYCH", "PSYCH": "PSYCH",
    "RELC": "REL", "REL": "REL", "RELA": "REL", "RELD": "REL",
    "BUSM": "BUS", "BUS": "BUS",
    "MKT": "MKTG", "MKTG": "MKTG",
    "FIN": "FIN",
    "ACC": "ACC", "ACCT": "ACC",
    "CS": "CS", "CSCI": "CS",
    "MATH": "MATH",
    "STAT": "STAT",
    "BIO": "BIO", "BIOL": "BIO",
    "CHEM": "CHEM",
    "PHYS": "PHYS",
    "ENGL": "ENGL",
    "HIST": "HIST",
    "ECON": "ECON",
    "SOC": "SOC",
    "JAP": "JAPN", "JAPN": "JAPN",
    "CHIN": "CHIN", "CHI": "CHIN",
    "FREN": "FREN", "FRE": "FREN",
    "GERM": "GERM", "GER": "GERM",
    "ESL": "ESL", "LING": "LING",
    "STDEV": "STDEV", "ST DEV": "STDEV",
}

# Matches the leading non-digit dept of a canonical BYU course code, e.g.
# "MKTG 201" -> "MKTG", "AM ST 100" -> "AM ST".
_TEACHING_AREA_RE = re.compile(r"^(.+?)\s+\d")
# Matches an RMP course code's dept+number after collapsing, e.g. "SPAN321".
_RMP_COURSE_RE = re.compile(r"^([A-Z]{2,7})(\d{2,4})([A-Z]*)$")


def extract_teaching_area(course_code: str) -> str | None:
    """'MKTG 201' -> 'MKTG'; 'AM ST 100' -> 'AM ST'; None if unparseable."""
    m = _TEACHING_AREA_RE.match((course_code or "").strip().upper())
    return m.group(1).strip() if m else None


def normalize_course_for_match(raw: str) -> str | None:
    """Canonical 'DEPT NUM' for overlap comparison.

    Collapses case/spaces/hyphens, maps dept prefixes via `_COURSE_DEPT_ALIASES`
    (SPA321 == SPAN321 == SPAN 321), and drops a trailing letter suffix
    ("HIST206R" == "HIST 206"). Returns None when unparseable (e.g. bare digits).
    """
    if not raw:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    m = _RMP_COURSE_RE.match(cleaned)
    if not m:
        return None
    dept, num, _suffix = m.groups()
    dept = _COURSE_DEPT_ALIASES.get(dept, dept)
    return f"{dept} {num}"


def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def normalize_person_name(raw: str | None) -> dict:
    """Normalize a person's name into {first, last} (lowercase, no punctuation).

    - "Melissa Paul Larson"  -> first="melissa", last="larson"  (middle dropped)
    - "Larson, Melissa Paul" -> first="melissa", last="larson"  (comma format)
    - "Mary Smith-Jones"     -> first="mary", last="jones"      (hyphen split)
    - "José García-López"    -> first="jose", last="lopez"      (accents stripped)
    - "Dr. John A. Doe III"  -> first="john", last="doe"        (suffixes dropped)

    Missing parts are None. A single token is treated as a last name.
    """
    if not raw or not raw.strip():
        return {"first": None, "last": None}
    s = _strip_accents(raw)
    has_comma = "," in s
    s = s.replace("-", " ").replace("'", " ")
    s = re.sub(r"[^a-z ]", " ", s.lower())
    tokens = [t for t in s.split() if t and t not in _SUFFIXES]
    if not tokens:
        return {"first": None, "last": None}
    if has_comma:
        # "Larson, Melissa Paul" -> last is the pre-comma token
        last = tokens[0]
        first = tokens[1] if len(tokens) > 1 else None
    else:
        last = tokens[-1]
        first = tokens[0] if len(tokens) > 1 else None
    if first and first in _NICKNAMES:
        first = _NICKNAMES[first]
    return {"first": first, "last": last}


def _first_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    # initial match: "M" vs "Melissa"
    if len(a) == 1 or len(b) == 1:
        return 0.9 if a[0] == b[0] else 0.0
    return fuzz.ratio(a, b) / 100.0


def _name_score(ours: dict, theirs: dict) -> float:
    """0..1 similarity between two normalized names."""
    last_a, last_b = ours["last"], theirs["last"]
    if not last_a or not last_b:
        return 0.0
    last_sim = fuzz.ratio(last_a, last_b) / 100.0
    # Last name must match strongly (exact, or a clear typo variant).
    if last_sim < 0.8 and last_a[0] != last_b[0]:
        return 0.0
    first_a, first_b = ours["first"], theirs["first"]
    if first_a is None or first_b is None:
        first_component = 0.4  # last-name-only match is inherently weak
    else:
        # Raw similarity (no floor): an unrelated first name must score low so
        # it does not look "plausibly ambiguous" against an exact match.
        first_component = _first_similarity(first_a, first_b)
    return 0.6 * last_sim + 0.4 * first_component


def _dept_signal(byu_depts: list[str], rmp_department: str | None) -> tuple[float, str]:
    """Department match: (score 0..1, status in {'match','contradict','unknown'}).

    "unknown" means we have no department on one or both sides, so the signal
    is simply unusable. "contradict" means we have both sides and they clearly
    disagree (e.g. BYU Marketing vs RMP Biology).
    """
    if not byu_depts or not rmp_department:
        return 0.0, "unknown"
    rmp_d = _strip_accents(rmp_department).lower()
    best = 0.0
    for d in byu_depts:
        aliases = _DEPT_ALIASES.get(d.upper(), [d.lower()])
        for alias in aliases:
            # Full-string ratio (not partial): "marketing" vs "religion" must
            # read as a mismatch (47), while "accounting" vs "accountancy" (76)
            # still matches.
            best = max(best, fuzz.ratio(alias, rmp_d) / 100.0)
    status = "match" if best >= 0.5 else "contradict"
    return best, status


def _course_overlap(byu_courses: set[str], cand: dict) -> tuple[list[tuple[str, int]], int]:
    """Return ([(course, review_count)], total_reviews) for RMP courses that
    overlap the BYU instructor's course set, weighted by review count."""
    matched: list[tuple[str, int]] = []
    total = 0
    for cc in cand.get("courseCodes") or []:
        norm = normalize_course_for_match(cc.get("courseName"))
        if norm and norm in byu_courses:
            try:
                cnt = int(cc.get("courseCount") or 0)
            except (TypeError, ValueError):
                cnt = 0
            matched.append((norm, cnt))
            total += cnt
    return matched, total


@dataclass
class MatchResult:
    candidate: dict | None
    confidence: float
    matched: bool
    reason: str
    evidence: dict
    ambiguous: bool = False


def _score_candidates(
    byu_name: str,
    candidates: list[dict],
    byu_courses: set[str],
    byu_depts: list[str],
) -> list[dict]:
    """Score every candidate; returns a list of score dicts (sorted desc)."""
    ours = normalize_person_name(byu_name)
    scored = []
    for cand in candidates:
        theirs = normalize_person_name(
            f"{cand.get('firstName', '')} {cand.get('lastName', '')}"
        )
        name_score = _name_score(ours, theirs)
        matched_courses, overlap_reviews = _course_overlap(byu_courses, cand)
        dept_score, dept_status = _dept_signal(byu_depts, cand.get("department"))

        # Course overlap is the decisive signal. Even a single overlapping
        # course strongly identifies the professor (RMP's own `department`
        # label is often broader, e.g. "Business" for a Marketing professor, so
        # it must not sink a course-overlap match). Review count adds a little
        # extra confidence on top.
        if overlap_reviews > 0:
            overlap_boost = min(1.0, 0.85 + 0.05 * overlap_reviews)
            conf = 0.50 * overlap_boost + 0.35 * name_score + 0.15 * dept_score
        elif dept_status == "contradict":
            # Department actively disagrees -> non-match regardless of name.
            conf = 0.30 * name_score
        elif dept_status == "match":
            conf = 0.60 * name_score + 0.40 * dept_score
        else:  # unknown: rely on the name alone
            conf = name_score

        scored.append({
            "candidate": cand,
            "name_score": name_score,
            "overlap_reviews": overlap_reviews,
            "matched_courses": matched_courses,
            "dept_score": dept_score,
            "dept_status": dept_status,
            "confidence": round(min(1.0, conf), 3),
            "num_ratings": cand.get("numRatings") or 0,
        })
    scored.sort(key=lambda s: (s["confidence"], s["num_ratings"]), reverse=True)
    return scored


def match_professor(
    byu_name: str,
    candidates: list[dict],
    byu_courses: list[str] | None = None,
    byu_departments: list[str] | None = None,
) -> MatchResult:
    """Pick the best RMP candidate using course overlap > department > name.

    A candidate is accepted only when it clearly wins:
      * confidence >= CONFIDENCE_THRESHOLD (0.8), and
      * it beats the runner-up by >= AMBIGUITY_MARGIN.

    Otherwise ratings stay NULL, but `reason` and `evidence` record why so the
    result can be reviewed by hand. A lone candidate with zero course overlap
    AND a contradicting department is treated as a non-match.
    """
    byu_course_set = {normalize_course_for_match(c) for c in (byu_courses or [])}
    byu_course_set.discard(None)
    byu_departments = byu_departments or []

    if not candidates:
        return MatchResult(
            candidate=None, confidence=0.0, matched=False,
            reason="no RMP candidates", evidence={}, ambiguous=False,
        )

    scored = _score_candidates(byu_name, candidates, byu_course_set, byu_departments)
    best = scored[0]
    runner_up = scored[1] if len(scored) > 1 else None

    # Runner-up margin decides ambiguity (always beat by a meaningful gap).
    ambiguous = runner_up is not None and (
        best["confidence"] - runner_up["confidence"]
    ) < AMBIGUITY_MARGIN

    # Evidence for audit.
    evidence = {
        "best_name": f"{best['candidate'].get('firstName', '')} {best['candidate'].get('lastName', '')}".strip(),
        "best_department": best["candidate"].get("department"),
        "name_score": best["name_score"],
        "overlap_reviews": best["overlap_reviews"],
        "matched_courses": best["matched_courses"],
        "dept_score": best["dept_score"],
        "dept_status": best["dept_status"],
        "confidence": best["confidence"],
        "num_ratings": best["num_ratings"],
        "num_candidates": len(candidates),
        "runner_up_confidence": runner_up["confidence"] if runner_up else None,
    }

    # Determine outcome.
    if best["overlap_reviews"] > 0:
        course_str = ", ".join(
            f"{c} ({n} reviews)" for c, n in best["matched_courses"]
        )
        reason = f"course overlap: {course_str}"
    elif best["dept_status"] == "contradict":
        # Lone (or any) candidate with a clearly wrong department -> non-match.
        byu_d = "/".join(sorted(set(byu_departments))) or "unknown"
        rmp_d = best["candidate"].get("department") or "unknown"
        return MatchResult(
            candidate=best["candidate"], confidence=best["confidence"],
            matched=False, ambiguous=ambiguous,
            reason=f"non-match: department contradicts (BYU {byu_d} vs RMP {rmp_d})",
            evidence=evidence,
        )
    elif best["dept_status"] == "match":
        reason = f"department: {best['candidate'].get('department')}"
    else:
        reason = "name only (no course/department evidence)"

    matched = (not ambiguous) and best["confidence"] >= CONFIDENCE_THRESHOLD
    if not matched and not ambiguous:
        reason = f"low confidence ({best['confidence']:.2f}): {reason}"

    return MatchResult(
        candidate=best["candidate"], confidence=best["confidence"],
        matched=matched, reason=reason, evidence=evidence, ambiguous=ambiguous,
    )


# ── Ease score ───────────────────────────────────────────────────────────────

def ease_score(professor: dict) -> tuple[float, float]:
    """Rate how "easy" a professor is: (score 0-100, confidence 0-1).

    Higher score = easier. Components (each normalized to 0..1, blended by the
    weights below, then re-normalized over whatever data is present):

        difficulty_component = (5 - avg_difficulty) / 4     weight 0.5
        rating_component     = (avg_rating - 1) / 4         weight 0.3
        would_take_again     = wouldTakeAgainPercent / 100  weight 0.2

    Confidence scales with num_ratings (full confidence at 30+) and is reduced
    when fields are missing (missing data lowers confidence, never raises ease).

    Rules:
        * num_ratings < 3  -> neutral (50.0, low confidence). Too few to judge.
        * no data at all   -> neutral (50.0, low confidence).
        * Missing fields are simply omitted from the blend — they are NEVER
          assumed to be good/easy.
    """
    num = professor.get("num_ratings")
    rating = professor.get("avg_rating")
    difficulty = professor.get("avg_difficulty")
    wta = professor.get("would_take_again")

    if not num or num < 3:
        return 50.0, 0.1

    components: list[tuple[float, float]] = []
    if difficulty is not None:
        components.append(((5.0 - float(difficulty)) / 4.0, 0.5))
    if rating is not None:
        components.append(((float(rating) - 1.0) / 4.0, 0.3))
    if wta is not None:
        components.append((float(wta) / 100.0, 0.2))

    if not components:
        return 50.0, 0.1

    total_weight = sum(w for _, w in components)
    score = 100.0 * sum(v * w for v, w in components) / total_weight
    completeness = total_weight  # full set of weights sums to 1.0
    confidence = min(1.0, float(num) / 30.0) * completeness
    return round(min(100.0, max(0.0, score)), 1), round(min(1.0, max(0.0, confidence)), 3)


def rmp_url_from_id(rmp_id: str | None) -> str | None:
    """Build the public RateMyProfessors profile URL from a stored ``rmp_id``.

    RMP's GraphQL ``id`` is a base64-encoded string that decodes to
    ``"Teacher-123456"``. We decode it, require exactly that shape with an
    all-digit numeric id, and only then construct the URL. Any decoding or
    validation failure returns ``None`` so we never link to an unvalidated id.
    """
    if not rmp_id:
        return None
    try:
        # Tolerate missing base64 padding, but validate the alphabet strictly.
        padded = rmp_id + "=" * (-len(rmp_id) % 4)
        decoded = base64.b64decode(padded, validate=True).decode("utf-8").strip()
    except ValueError:  # binascii.Error / UnicodeDecodeError are ValueErrors
        return None
    m = re.fullmatch(r"Teacher-([0-9]+)", decoded)
    if not m:
        return None
    return f"https://www.ratemyprofessors.com/professor/{m.group(1)}"


# ── Refresh orchestration ────────────────────────────────────────────────────

async def refresh_professors(
    limit: int | None = None,
    force: bool = False,
    fetcher: Callable[[httpx.AsyncClient, str], Awaitable[list[dict]]] | None = None,
) -> dict:
    """Look up stale professors on RMP and store ratings. Returns a summary.

    `fetcher` is injectable for tests: an async callable (client, name) ->
    list[candidate dicts]. Defaults to the real RMP search.
    """
    await database.init_schedule_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=REFRESH_AGE_DAYS)).isoformat()
    professors = await database.get_professors_needing_refresh(cutoff, limit, force)

    summary = {
        "attempted": 0,
        "matched": 0,
        "low_confidence": 0,
        "not_found": 0,
        "errors": 0,
    }

    search = fetcher or search_teachers

    async with _client() as client:
        for p in professors:
            summary["attempted"] += 1
            name = p["byu_name"]
            pid = p["id"]
            try:
                candidates = await search(client, name)
                courses = await database.get_professor_courses(pid)
                depts = [extract_teaching_area(c) for c in courses]
                depts = [d for d in depts if d]
                result = match_professor(name, candidates, courses, depts)
                evidence_json = json.dumps(result.evidence) if result.evidence else None

                if result.candidate is None:
                    await database.mark_professor_fetched(pid)
                    summary["not_found"] += 1
                elif not result.matched:
                    # Store the best-guess identity + confidence + reason for
                    # audit, but keep ratings NULL (never guess).
                    c = result.candidate
                    await database.update_professor_ratings(
                        professor_id=pid,
                        rmp_id=c.get("id"),
                        rmp_name=f"{c.get('firstName', '')} {c.get('lastName', '')}".strip(),
                        avg_difficulty=None,
                        avg_rating=None,
                        would_take_again=None,
                        num_ratings=None,
                        match_confidence=result.confidence,
                        match_reason=result.reason,
                        match_evidence=evidence_json,
                    )
                    summary["low_confidence"] += 1
                    logger.info(
                        "RMP low-confidence for %r (conf=%.2f): %s",
                        name, result.confidence, result.reason,
                    )
                else:
                    c = result.candidate
                    await database.update_professor_ratings(
                        professor_id=pid,
                        rmp_id=c.get("id"),
                        rmp_name=f"{c.get('firstName', '')} {c.get('lastName', '')}".strip(),
                        avg_difficulty=c.get("avgDifficulty"),
                        avg_rating=c.get("avgRating"),
                        would_take_again=c.get("wouldTakeAgainPercent"),
                        num_ratings=c.get("numRatings"),
                        match_confidence=result.confidence,
                        match_reason=result.reason,
                        match_evidence=evidence_json,
                    )
                    summary["matched"] += 1
            except Exception as e:
                # Log clearly and skip; never crash or wipe existing ratings.
                logger.warning("RMP error for %r: %s", name, e)
                summary["errors"] += 1
            await asyncio.sleep(RATE_LIMIT_SECONDS)

    logger.info("RMP refresh summary: %s", summary)
    return summary


def _print_summary(s: dict) -> None:
    print(
        f"RMP refresh: attempted={s['attempted']} matched={s['matched']} "
        f"low_confidence={s['low_confidence']} not_found={s['not_found']} "
        f"errors={s['errors']}"
    )


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Refresh RateMyProfessors ratings")
    parser.add_argument("--refresh", action="store_true", help="Run a refresh pass")
    parser.add_argument("--limit", type=int, default=None, help="Max professors to query")
    parser.add_argument("--force", action="store_true", help="Ignore the 120-day freshness window")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    summary = await refresh_professors(limit=args.limit, force=args.force)
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

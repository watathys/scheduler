"""Deterministic schedule solver for the AI schedule builder.

The solver only READS the database (sections/meetings/professors), never
scrapes. It builds every conflict-free combination of sections (one per
course) via backtracking, scores each schedule against the student's ranked
preferences, and returns the top-3 *meaningfully different* schedules.

Design notes
------------
* Times are minutes since midnight; each meeting is one row per weekday.
* The LLM never places sections: this module is pure, deterministic logic.
* ``rmp.ease_score`` is reused verbatim for the "easy" preference.
* Async online sections have zero meetings, so they never conflict and are
  reported separately ("Online / no set time").
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Optional

import aiosqlite

from . import database, rmp
from .matching import closest_courses, normalize_course_code, title_case_name
from .models import (
    BottleneckDiagnosis,
    CourseFilterReport,
    EnvelopeFilterReport,
    MeetingOut,
    ScheduleMetrics,
    ScheduleOut,
    SectionOut,
    SolverRequest,
    SolverResponse,
)

# ── Tunable constants ─────────────────────────────────────────────────────────
PREFERENCE_WEIGHTS = [50, 30, 20]      # rank 1 / 2 / 3
MAX_SCHEDULES = 50_000                 # stop collecting after this many
TIME_LIMIT_SECONDS = 5.0               # ... or after this many seconds
GAP_OVER_LIMIT_MIN = 180               # gaps longer than this are penalized harder
NO_GAPS_NORMALIZER_MIN = 720           # effective-gap minutes that map to a 0 score
_NO_BOUND = 10 ** 9                    # "no earliest/latest bound" sentinel

# BYU building code for the Salt Lake Center. A section with any meeting here
# is treated as a "Salt Lake" class (excluded unless the student allows it).
SLC_BUILDING = "SLC"

FILTER_NAMES = ("instructor", "online", "salt_lake", "days_off", "full", "earliest_latest", "blocked_times")
RELAXATION_LABELS = [
    "earliest/latest window",
    "blocked times",
    "online allowed",
    "skip full sections",
    "salt lake allowed",
]

# Weekdays (in display order) that a student might want off.
OFF_DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


class SolverError(Exception):
    """Raised when the request itself cannot be satisfied (bad course, etc.)."""

    def __init__(self, message: str, *, course: Optional[str] = None,
                 suggestions: Optional[list[str]] = None):
        super().__init__(message)
        self.course = course
        self.suggestions = suggestions or []


# ── Internal model ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Meeting:
    day: str
    start_min: int
    end_min: int
    building: Optional[str] = None
    room: Optional[str] = None


@dataclass(frozen=True)
class Section:
    id: int
    course_code: str
    section_number: str
    is_online: bool
    seats_open: Optional[int]
    instructor_id: Optional[int]
    instructor_name: Optional[str]
    ease_score: float
    ease_confidence: float
    lacks_ratings: bool
    meetings: tuple[Meeting, ...] = ()
    # True when any meeting is at the Salt Lake Center (building == "SLC").
    is_salt_lake: bool = False
    title: Optional[str] = None
    rmp_id: Optional[str] = None
    rmp_name: Optional[str] = None
    avg_rating: Optional[float] = None
    avg_difficulty: Optional[float] = None
    would_take_again: Optional[float] = None
    num_ratings: Optional[int] = None
    # Half-semester support: ISO 'YYYY-MM-DD' (None => full-term / unknown).
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    # Envelope provenance (set only on sections placed via an envelope).
    via_envelope: Optional[str] = None
    env_id: Optional[str] = None
    captured_at: Optional[str] = None


@dataclass(frozen=True)
class Bundle:
    """One verified-ok envelope bundle: a fixed group of sections placed together."""
    env_slug: str
    env_title: str
    env_id: Optional[str]
    captured_at: str
    bundle_index: int
    sections: tuple[Section, ...]   # already tagged with via_envelope/env_id
    courses: frozenset[str]


# ── Conflict detection ────────────────────────────────────────────────────────

def meetings_overlap(a: Meeting, b: Meeting) -> bool:
    """Two meetings conflict iff same day and their [start, end) intervals
    overlap. Back-to-back (a.end == b.start) does NOT conflict (strict <)."""
    return a.day == b.day and a.start_min < b.end_min and b.start_min < a.end_min


def _dates_overlap(a: Section, b: Section) -> bool:
    """True when two sections' term date ranges overlap.

    Missing dates (either side NULL) mean "full term / unknown", which overlaps
    everything — this preserves the old behavior on databases that predate the
    ``start_date`` / ``end_date`` migration.
    """
    if (a.start_date is None or a.end_date is None
            or b.start_date is None or b.end_date is None):
        return True
    return max(a.start_date, b.start_date) <= min(a.end_date, b.end_date)


def sections_conflict(a: Section, b: Section) -> bool:
    """Two sections conflict iff their date ranges overlap AND any meeting
    overlaps (same weekday + overlapping [start, end) interval)."""
    if not _dates_overlap(a, b):
        return False
    for ma in a.meetings:
        for mb in b.meetings:
            if meetings_overlap(ma, mb):
                return True
    return False


# ── Normalization & course resolution ─────────────────────────────────────────

def _norm_day(day: str) -> str:
    return (day or "").strip().title()


async def _distinct_course_codes(term: str) -> set[str]:
    async with aiosqlite.connect(database.DB) as db:
        async with db.execute(
            "SELECT DISTINCT course_code FROM sections WHERE term = ?", (term,)
        ) as cur:
            rows = await cur.fetchall()
    return {r[0] for r in rows}


async def _resolve_courses(term: str, raw_codes: list[str]) -> list[str]:
    """Normalize each code and map to a canonical DB code (exact match).

    Raises SolverError (with fuzzy suggestions) when a code has no exact match.
    Duplicate codes are collapsed while preserving order.
    """
    available = await _distinct_course_codes(term)
    resolved: list[str] = []
    seen: set[str] = set()
    for raw in raw_codes:
        norm = normalize_course_code(raw)
        if norm in available:
            if norm not in seen:
                seen.add(norm)
                resolved.append(norm)
        else:
            suggestions = [c for c, _ in closest_courses(norm, sorted(available), limit=5)]
            raise SolverError(
                f"No sections found for course {raw!r} in term {term}. "
                f"Did you mean one of: {', '.join(suggestions) if suggestions else '(no close matches)'}",
                course=norm,
                suggestions=suggestions,
            )
    return resolved


# ── Loading from the DB ───────────────────────────────────────────────────────

async def load_sections(term: str, course_codes: list[str]) -> dict[str, list[Section]]:
    """Load all sections (+meetings +instructor ease) for the given courses.

    Returns {course_code: [Section, ...]} in deterministic order.
    """
    await database.init_schedule_db()

    async with aiosqlite.connect(database.DB) as db:
        db.row_factory = aiosqlite.Row
        ph = ",".join("?" for _ in course_codes)
        async with db.execute(
            f"""
            SELECT s.id, s.course_code, s.section_number, s.title, s.is_online,
                   s.seats_open, s.instructor_id, s.start_date, s.end_date,
                   p.byu_name, p.rmp_id, p.rmp_name,
                   p.avg_difficulty, p.avg_rating,
                   p.would_take_again, p.num_ratings
            FROM sections s
            LEFT JOIN professors p ON p.id = s.instructor_id
            WHERE s.term = ? AND s.course_code IN ({ph})
            ORDER BY s.course_code, s.section_number, s.id
            """,
            [term, *course_codes],
        ) as cur:
            rows = await cur.fetchall()

    section_ids = [r["id"] for r in rows]
    meetings_by_section: dict[int, list[Meeting]] = {}
    if section_ids:
        async with aiosqlite.connect(database.DB) as db:
            db.row_factory = aiosqlite.Row
            ph = ",".join("?" for _ in section_ids)
            async with db.execute(
                f"""
                SELECT section_id, day, start_min, end_min, building, room
                FROM meetings
                WHERE section_id IN ({ph})
                ORDER BY section_id, day, start_min
                """,
                section_ids,
            ) as cur:
                for mr in await cur.fetchall():
                    meetings_by_section.setdefault(mr["section_id"], []).append(
                        Meeting(mr["day"], mr["start_min"], mr["end_min"],
                                mr["building"], mr["room"])
                    )

    out: dict[str, list[Section]] = {}
    for r in rows:
        if r["instructor_id"] is None:
            ease, conf, lacks = 50.0, 0.0, True
        else:
            prof = {
                "avg_difficulty": r["avg_difficulty"],
                "avg_rating": r["avg_rating"],
                "would_take_again": r["would_take_again"],
                "num_ratings": r["num_ratings"],
            }
            ease, conf = rmp.ease_score(prof)
            lacks = (prof["num_ratings"] or 0) < 3

        meetings = tuple(meetings_by_section.get(r["id"], []))
        section = Section(
            id=r["id"],
            course_code=r["course_code"],
            section_number=r["section_number"],
            is_online=bool(r["is_online"]),
            seats_open=r["seats_open"],
            instructor_id=r["instructor_id"],
            instructor_name=r["byu_name"],
            ease_score=ease,
            ease_confidence=conf,
            lacks_ratings=lacks,
            meetings=meetings,
            is_salt_lake=any(m.building == SLC_BUILDING for m in meetings),
            title=r["title"],
            rmp_id=r["rmp_id"],
            rmp_name=r["rmp_name"],
            avg_rating=r["avg_rating"],
            avg_difficulty=r["avg_difficulty"],
            would_take_again=r["would_take_again"],
            num_ratings=r["num_ratings"],
            start_date=r["start_date"],
            end_date=r["end_date"],
        )
        out.setdefault(r["course_code"], []).append(section)
    return out


async def possible_days_off(term: str, raw_codes: list[str]) -> list[str]:
    """Weekdays that can actually be taken off for the given courses.

    A day is *unavailable* (impossible to take off) when some course meets on
    it in every one of its sections — i.e. no section of that course avoids
    that day. Async/online sections have no meetings, so they avoid every day
    and never make a day unavailable. Returns the Mon–Fri subset (in order)
    that remains available.
    """
    codes = await _resolve_courses(term, raw_codes)
    loaded = await load_sections(term, codes)
    unavailable: set[str] = set()
    for code in codes:
        sections = loaded.get(code, [])
        common: set[str] | None = None
        for s in sections:
            days = {m.day for m in s.meetings}
            common = days if common is None else (common & days)
            if not common:
                break
        if common:
            unavailable |= common
    return [d for d in OFF_DAY_ORDER if d not in unavailable]


async def load_sections_by_ids(term: str, section_ids: list[int]) -> dict[int, Section]:
    """Load Section objects (with meetings + ease) for specific section ids.

    Used to resolve envelope bundle sections to full ``Section`` objects.
    Returns {section_id: Section} in deterministic order.
    """
    if not section_ids:
        return {}
    await database.init_schedule_db()

    async with aiosqlite.connect(database.DB) as db:
        db.row_factory = aiosqlite.Row
        ph = ",".join("?" for _ in section_ids)
        async with db.execute(
            f"""
            SELECT s.id, s.course_code, s.section_number, s.title, s.is_online,
                   s.seats_open, s.instructor_id, s.start_date, s.end_date,
                   p.byu_name, p.rmp_id, p.rmp_name,
                   p.avg_difficulty, p.avg_rating,
                   p.would_take_again, p.num_ratings
            FROM sections s
            LEFT JOIN professors p ON p.id = s.instructor_id
            WHERE s.term = ? AND s.id IN ({ph})
            ORDER BY s.id
            """,
            [term, *section_ids],
        ) as cur:
            rows = await cur.fetchall()

    meetings_by_section: dict[int, list[Meeting]] = {}
    async with aiosqlite.connect(database.DB) as db:
        db.row_factory = aiosqlite.Row
        ph = ",".join("?" for _ in section_ids)
        async with db.execute(
            f"""
            SELECT section_id, day, start_min, end_min, building, room
            FROM meetings
            WHERE section_id IN ({ph})
            ORDER BY section_id, day, start_min
            """,
            section_ids,
        ) as cur:
            for mr in await cur.fetchall():
                meetings_by_section.setdefault(mr["section_id"], []).append(
                    Meeting(mr["day"], mr["start_min"], mr["end_min"],
                            mr["building"], mr["room"])
                )

    out: dict[int, Section] = {}
    for r in rows:
        if r["instructor_id"] is None:
            ease, conf, lacks = 50.0, 0.0, True
        else:
            prof = {
                "avg_difficulty": r["avg_difficulty"],
                "avg_rating": r["avg_rating"],
                "would_take_again": r["would_take_again"],
                "num_ratings": r["num_ratings"],
            }
            ease, conf = rmp.ease_score(prof)
            lacks = (prof["num_ratings"] or 0) < 3

        meetings = tuple(meetings_by_section.get(r["id"], []))
        out[r["id"]] = Section(
            id=r["id"],
            course_code=r["course_code"],
            section_number=r["section_number"],
            is_online=bool(r["is_online"]),
            seats_open=r["seats_open"],
            instructor_id=r["instructor_id"],
            instructor_name=r["byu_name"],
            ease_score=ease,
            ease_confidence=conf,
            lacks_ratings=lacks,
            meetings=meetings,
            is_salt_lake=any(m.building == SLC_BUILDING for m in meetings),
            title=r["title"],
            rmp_id=r["rmp_id"],
            rmp_name=r["rmp_name"],
            avg_rating=r["avg_rating"],
            avg_difficulty=r["avg_difficulty"],
            would_take_again=r["would_take_again"],
            num_ratings=r["num_ratings"],
            start_date=r["start_date"],
            end_date=r["end_date"],
        )
    return out


# ── Hard filters ──────────────────────────────────────────────────────────────

def _overlaps_blocked(m: Meeting, blocked_times: list[tuple[str, int, int]]) -> bool:
    for day, start, end in blocked_times:
        if m.day == day and m.start_min < end and start < m.end_min:
            return True
    return False


def _instructor_allowed(s: Section, allowed: set[str]) -> bool:
    """True when a section satisfies the course's instructor selection.

    ``allowed`` holds the chosen keys: a professor id as a string, the literal
    ``"online"`` (keep async/online sections), or ``"staff"`` (keep sections
    with no assigned instructor).
    """
    if "online" in allowed and s.is_online:
        return True
    if "staff" in allowed and s.instructor_id is None:
        return True
    return s.instructor_id is not None and str(s.instructor_id) in allowed


def apply_hard_filters(
    sections: list[Section],
    *,
    allow_online: bool,
    allow_salt_lake: bool = True,
    skip_full_sections: bool,
    earliest_start_min: int,
    latest_end_min: int,
    blocked_times: list[tuple[str, int, int]],
    days_off: list[str] | None = None,
    instructors: list[str] | None = None,
) -> tuple[list[Section], dict[str, int]]:
    """Remove any section that violates a hard constraint.

    ``days_off`` is a hard constraint: any section with a meeting on a requested
    off-day is removed. ``instructors`` is a hard constraint too: when set, only
    sections matching one of the selected instructor keys are kept. When
    ``allow_salt_lake`` is False, any section with a meeting at the Salt Lake
    Center is removed. Returns (kept_sections, removed_count_by_filter). Async
    sections (no meetings) never violate the time/blocked/days-off/salt-lake
    filters.
    """
    off_days = {_norm_day(d) for d in (days_off or [])}
    allowed = set(instructors or [])
    kept: list[Section] = []
    removed = {name: 0 for name in FILTER_NAMES}
    for s in sections:
        if allowed and not _instructor_allowed(s, allowed):
            removed["instructor"] += 1
            continue
        if s.is_online and not allow_online:
            removed["online"] += 1
            continue
        if s.is_salt_lake and not allow_salt_lake:
            removed["salt_lake"] += 1
            continue
        if off_days and any(m.day in off_days for m in s.meetings):
            removed["days_off"] += 1
            continue
        if skip_full_sections and s.seats_open == 0:
            removed["full"] += 1
            continue
        if any(m.start_min < earliest_start_min or m.end_min > latest_end_min
               for m in s.meetings):
            removed["earliest_latest"] += 1
            continue
        if any(_overlaps_blocked(m, blocked_times) for m in s.meetings):
            removed["blocked_times"] += 1
            continue
        kept.append(s)
    return kept, removed


# ── Scoring ───────────────────────────────────────────────────────────────────

def _has_ease_data(s: Section) -> bool:
    """True when a section's instructor has a confident RMP match with at least
    one rating — the same bar ``_professor_breakdown`` uses to keep
    ``data_status`` off ``"none"``.

    Professors with no RateMyProfessors page (or no confident match) have no
    trustworthy numbers, so they are excluded from the ease average entirely.
    """
    return s.num_ratings is not None and s.num_ratings >= 1


def _professor_breakdown(s: Section, courses: list[str]) -> dict:
    """Build one per-instructor breakdown entry from a representative section.

    We only surface RMP numbers we are confident in. Ratings are stored only
    after a confident match, so a ``NULL`` rating count means "no confident RMP
    match". A confident match with zero ratings (RMP returns ``0.0``/``-1.0``
    placeholders) is also treated as "no data": ``data_status`` is ``"none"``
    and every numeric field is nulled rather than guessed.
    """
    num = s.num_ratings
    if num is None or num < 1:
        data_status = "none"
        has_data = False
    elif num < 3:
        data_status = "limited"
        has_data = True
    else:
        data_status = "rated"
        has_data = True

    # Prefer the RMP name when we have data, else the BYU name.
    if has_data:
        name_source = s.rmp_name or s.instructor_name
    else:
        name_source = s.instructor_name
    display_name = title_case_name(name_source) or "Unknown instructor"

    return {
        "display_name": display_name,
        "courses": sorted(courses),
        "avg_rating": s.avg_rating if has_data else None,
        "avg_difficulty": s.avg_difficulty if has_data else None,
        "would_take_again_percent": s.would_take_again if has_data else None,
        "num_ratings": num if has_data else None,
        "rmp_url": rmp.rmp_url_from_id(s.rmp_id) if has_data else None,
        "ease_score": s.ease_score if has_data else None,
        "ease_confidence": s.ease_confidence if has_data else None,
        "data_status": data_status,
    }


def score_schedule(
    sections: list[Section],
    preferences: list[str],
) -> tuple[float, dict]:
    """Score a complete schedule (0-100) and compute its metrics.

    ``sections``: the chosen sections (async online included — they carry an
    instructor/ease but no meetings, so they don't affect campus/gap metrics).
    """
    meetings = [m for s in sections for m in s.meetings]
    by_day: dict[str, list[tuple[int, int]]] = {}
    for m in meetings:
        by_day.setdefault(m.day, []).append((m.start_min, m.end_min))
    for slots in by_day.values():
        slots.sort()

    # no_gaps: penalize idle minutes between classes; >3h gaps hurt more.
    total_gap = 0
    effective_gap = 0.0
    for slots in by_day.values():
        for i in range(len(slots) - 1):
            gap = slots[i + 1][0] - slots[i][1]
            total_gap += gap
            effective_gap += gap + max(0, gap - GAP_OVER_LIMIT_MIN)
    no_gaps_score = max(0.0, 100.0 * (1.0 - effective_gap / NO_GAPS_NORMALIZER_MIN))

    # easy: average ease over DISTINCT instructors that actually have
    # RateMyProfessors data. Professors with no RMP page are excluded entirely
    # rather than folded in as a neutral 50 (which would imply a rating we do
    # not have and drag the average toward the middle).
    by_instructor: dict[Optional[int], Section] = {}
    courses_by_instructor: dict[Optional[int], list[str]] = {}
    for s in sections:
        by_instructor.setdefault(s.instructor_id, s)
        course_list = courses_by_instructor.setdefault(s.instructor_id, [])
        if s.course_code not in course_list:
            course_list.append(s.course_code)
    rated = [s for s in by_instructor.values() if _has_ease_data(s)]
    avg_ease = (
        sum(s.ease_score for s in rated) / len(rated) if rated else None
    )

    # fewer_days: 1 day on campus = 100, 5+ days = 0 (linear in between).
    days_on_campus = len(by_day)
    fewer_days_score = (
        100.0 if days_on_campus <= 1
        else max(0.0, 100.0 * (1.0 - (days_on_campus - 1) / 4.0))
    )

    components = {
        # When no instructor has ratings, ease cannot be scored: use a neutral
        # 50 for ranking so schedules are neither rewarded nor punished for
        # data we do not have (the metric itself stays None for display).
        "easy": 50.0 if avg_ease is None else avg_ease,
        "no_gaps": no_gaps_score,
        "fewer_days": fewer_days_score,
    }
    total = 0.0
    for rank, pref in enumerate(preferences):
        total += PREFERENCE_WEIGHTS[rank] * components[pref] / 100.0

    earliest = min(m.start_min for m in meetings) if meetings else None
    latest = max(m.end_min for m in meetings) if meetings else None

    # Per-instructor breakdown (matches the average above exactly, since the
    # average is computed over these same distinct instructors).
    ease_breakdown = sorted(
        (
            _professor_breakdown(s, courses_by_instructor[s.instructor_id])
            for s in by_instructor.values()
        ),
        key=lambda e: (e["courses"][0] if e["courses"] else "", e["display_name"]),
    )

    metrics = {
        "days_on_campus": len(by_day),
        "total_gap_minutes": total_gap,
        # None means "cannot be calculated": no instructor had ratings data.
        "average_ease": round(avg_ease, 2) if avg_ease is not None else None,
        "earliest_start": earliest,
        "latest_end": latest,
        "professors_without_ratings": sum(
            1 for s in by_instructor.values() if not _has_ease_data(s)
        ),
        "ease_breakdown": ease_breakdown,
    }
    return round(total, 3), metrics


# ── Backtracking search ───────────────────────────────────────────────────────

def search_options(
    options: list[list[tuple[Section, ...]]],
    *,
    max_schedules: int = MAX_SCHEDULES,
    deadline_seconds: float = TIME_LIMIT_SECONDS,
) -> tuple[list[tuple[Section, ...]], bool]:
    """Backtracking over decision groups, each picking one *option* (a tuple of
    sections to place together — a single course section, or an envelope
    bundle's whole section group).

    ``options`` must be ordered fewest-options-first. Each returned assignment
    is a flat tuple of every chosen section (across all groups).
    """
    start = time.monotonic()
    n = len(options)
    current: list[Optional[tuple[Section, ...]]] = [None] * n
    assignments: list[tuple[Section, ...]] = []
    stopped = False

    def _option_ok(i: int, opt: tuple[Section, ...]) -> bool:
        # Within-option: a bundle's own sections must not conflict with each
        # other (identical ids are shared sections and never conflict).
        for a in range(len(opt)):
            for b in range(a + 1, len(opt)):
                if opt[a].id != opt[b].id and sections_conflict(opt[a], opt[b]):
                    return False
        for j in range(i):
            chosen = current[j]
            if chosen is None:
                continue
            for s in opt:
                for prev in chosen:
                    if s.id != prev.id and sections_conflict(s, prev):
                        return False
        return True

    def backtrack(i: int) -> None:
        nonlocal stopped
        if stopped:
            return
        if len(assignments) >= max_schedules:
            stopped = True
            return
        if time.monotonic() - start > deadline_seconds:
            stopped = True
            return
        if i == n:
            flat: list[Section] = []
            seen_ids: set[int] = set()
            for opt in current:
                for s in opt or ():
                    # Sections shared between bundles (same id) are placed once.
                    if s.id in seen_ids:
                        continue
                    seen_ids.add(s.id)
                    flat.append(s)
            assignments.append(tuple(flat))
            return
        for opt in options[i]:
            if _option_ok(i, opt):
                current[i] = opt
                backtrack(i + 1)
                current[i] = None
                if stopped:
                    return

    backtrack(0)
    return assignments, stopped


def search(
    courses_sections: list[tuple[str, list[Section]]],
    *,
    max_schedules: int = MAX_SCHEDULES,
    deadline_seconds: float = TIME_LIMIT_SECONDS,
) -> tuple[list[tuple[Section, ...]], bool]:
    """Backtracking: one section per course, pruning on the first conflict.

    ``courses_sections`` must already be ordered fewest-sections-first.
    Returns (list_of_assignments, hit_cap) where ``hit_cap`` is True when the
    search stopped because of the schedule cap or the time deadline.
    """
    options = [[(s,) for s in sections] for _, sections in courses_sections]
    return search_options(options, max_schedules=max_schedules,
                          deadline_seconds=deadline_seconds)


def _sort_key(item: tuple[float, dict, tuple[Section, ...]]):
    score, metrics, assignment = item
    section_ids = tuple(sorted(s.id for s in assignment))
    # None ease (no ratings anywhere) sorts as neutral so ordering stays stable.
    avg_ease = metrics["average_ease"]
    return (
        -score,
        metrics["days_on_campus"],
        metrics["total_gap_minutes"],
        -(50.0 if avg_ease is None else avg_ease),
        section_ids,
    )


def _shared_sections(a: tuple[Section, ...], b: tuple[Section, ...]) -> int:
    return len({s.id for s in a} & {s.id for s in b})


def _meaningfully_different_ids(a: frozenset[int], b: frozenset[int], n: int) -> bool:
    """True when two schedules (as section-id sets) share <= half the *choices*."""
    return 2 * len(a & b) <= n


def _meaningfully_different(a: tuple[Section, ...], b: tuple[Section, ...], n: int) -> bool:
    """True when the two schedules share a section for <= half the *choices*.

    ``n`` is the number of decision groups (plain courses + envelopes); an
    envelope bundle contributes several sections but is still one choice, so
    comparing shared section *ids* (rather than a positional zip) keeps two
    bundle choices distinct even when they share an individual section.
    """
    return _meaningfully_different_ids(
        frozenset(s.id for s in a), frozenset(s.id for s in b), n
    )


# ── Bottleneck diagnosis ──────────────────────────────────────────────────────

def _blocked_tuples(req: SolverRequest) -> list[tuple[str, int, int]]:
    return [(_norm_day(b.day), b.start_min, b.end_min) for b in req.blocked_times]


def _normalize_instructors(instructors: dict[str, list[str]] | None) -> dict[str, list[str]]:
    """Normalize course-code keys in an instructor selection map.

    Frontend keys arrive as canonical codes already, but normalize anyway so a
    hand-written client can pass "mktg201" and still match. Values are de-duped.
    """
    if not instructors:
        return {}
    out: dict[str, list[str]] = {}
    for raw, keys in instructors.items():
        code = normalize_course_code(raw)
        bucket = out.setdefault(code, [])
        for k in keys:
            if k not in bucket:
                bucket.append(k)
    return out


def _apply(req: SolverRequest, sections: list[Section],
           blocked: list[tuple[str, int, int]],
           instructors: list[str] | None = None) -> tuple[list[Section], dict[str, int]]:
    return apply_hard_filters(
        sections,
        allow_online=req.allow_online,
        allow_salt_lake=req.allow_salt_lake,
        skip_full_sections=req.skip_full_sections,
        earliest_start_min=req.earliest_start_min,
        latest_end_min=req.latest_end_min,
        blocked_times=blocked,
        days_off=req.days_off,
        instructors=instructors,
    )


def _relaxed_request(req: SolverRequest, label: str) -> SolverRequest:
    if label == "earliest/latest window":
        return req.model_copy(update={"earliest_start_min": 0, "latest_end_min": _NO_BOUND})
    if label == "blocked times":
        return req.model_copy(update={"blocked_times": []})
    if label == "online allowed":
        return req.model_copy(update={"allow_online": True})
    if label == "skip full sections":
        return req.model_copy(update={"skip_full_sections": False})
    if label == "salt lake allowed":
        return req.model_copy(update={"allow_salt_lake": True})
    raise ValueError(f"unknown relaxation: {label}")


def _always_conflict(a_sections: list[Section], b_sections: list[Section]) -> bool:
    for a in a_sections:
        for b in b_sections:
            if not sections_conflict(a, b):
                return False
    return True


def _feasible(courses_sections: list[tuple[str, list[Section]]]) -> bool:
    assignments, _ = search(courses_sections, max_schedules=1)
    return len(assignments) > 0


def _fmt_filters(removed: dict[str, int]) -> str:
    return ", ".join(f"{k}={v}" for k, v in removed.items() if v)


def diagnose_bottleneck(
    codes: list[str],
    loaded: dict[str, list[Section]],
    req: SolverRequest,
) -> BottleneckDiagnosis:
    """Explain why no schedule exists and what single relaxation would fix it."""
    blocked = _blocked_tuples(req)
    instructors = _normalize_instructors(req.instructors)
    filtered: dict[str, list[Section]] = {}
    no_sections: list[CourseFilterReport] = []
    for code in codes:
        kept, removed = _apply(req, loaded[code], blocked, instructors.get(code))
        filtered[code] = kept
        if not kept:
            no_sections.append(CourseFilterReport(
                course=code,
                removed_by={k: v for k, v in removed.items() if v},
            ))

    # Pairs of courses whose remaining sections always conflict.
    pairs: list[tuple[str, str]] = []
    ordered = sorted(filtered)
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            a, b = ordered[i], ordered[j]
            if filtered[a] and filtered[b] and _always_conflict(filtered[a], filtered[b]):
                pairs.append((a, b))

    # Which single relaxation makes the whole thing feasible?
    feasible: list[str] = []
    if codes:
        for label in RELAXATION_LABELS:
            rreq = _relaxed_request(req, label)
            rblocked = _blocked_tuples(rreq)
            courses_sections: list[tuple[str, list[Section]]] = []
            ok = True
            for code in codes:
                kept, _ = _apply(rreq, loaded[code], rblocked, instructors.get(code))
                if not kept:
                    ok = False
                    break
                courses_sections.append((code, kept))
            if ok:
                courses_sections.sort(key=lambda cs: (len(cs[1]), cs[0]))
                if _feasible(courses_sections):
                    feasible.append(label)

    msgs: list[str] = []
    for r in no_sections:
        if set(r.removed_by) == {"online"} and not req.allow_online:
            msgs.append(f"{r.course} is online-only but online sections are not allowed")
        elif set(r.removed_by) == {"salt_lake"} and not req.allow_salt_lake:
            msgs.append(f"{r.course} only has Salt Lake Center sections but Salt Lake classes are not allowed")
        elif "days_off" in r.removed_by and req.days_off:
            days = ", ".join(req.days_off)
            msgs.append(f"{r.course} has no sections that avoid {days} (a day you want off)")
        elif "instructor" in r.removed_by:
            msgs.append(f"{r.course} has no sections taught by the instructor(s) you selected")
        elif r.removed_by:
            msgs.append(f"{r.course} has no sections left after filtering ({_fmt_filters(r.removed_by)})")
    if pairs:
        msgs.append("always-conflicting pairs: " + "; ".join(f"{a} vs {b}" for a, b in pairs))
    if feasible:
        msgs.append("feasible if relaxing: " + ", ".join(feasible))

    return BottleneckDiagnosis(
        no_sections=no_sections,
        feasible_relaxations=feasible,
        always_conflicting_pairs=pairs,
        message=". ".join(msgs) if msgs else None,
    )


# ── Output assembly ───────────────────────────────────────────────────────────

def _section_out(s: Section) -> SectionOut:
    return SectionOut(
        id=s.id,
        course_code=s.course_code,
        section_number=s.section_number,
        title=s.title,
        is_online=s.is_online,
        seats_open=s.seats_open,
        instructor_name=s.instructor_name,
        ease_score=s.ease_score,
        ease_confidence=s.ease_confidence,
        meetings=[
            MeetingOut(day=m.day, start_min=m.start_min, end_min=m.end_min,
                       building=m.building, room=m.room)
            for m in s.meetings
        ],
        via_envelope=s.via_envelope,
        env_id=s.env_id,
        captured_at=s.captured_at,
    )


def _schedule_out(score: float, metrics: dict, assignment: tuple[Section, ...]) -> ScheduleOut:
    sections: list[SectionOut] = []
    async_online: list[SectionOut] = []
    for s in assignment:
        out = _section_out(s)
        if s.is_online and not s.meetings:
            async_online.append(out)
        else:
            sections.append(out)
    return ScheduleOut(
        sections=sections,
        async_online=async_online,
        total_score=score,
        metrics=ScheduleMetrics(**metrics),
    )


# ── Envelope loading ──────────────────────────────────────────────────────────

def _with_envelope(s: Section, title: str, env_id: Optional[str],
                   captured_at: str) -> Section:
    return replace(s, via_envelope=title, env_id=env_id, captured_at=captured_at)


async def _load_envelope_bundles(
    term: str, slugs: list[str]
) -> tuple[dict[str, list[Bundle]], dict[str, str]]:
    """Load verified-ok bundles for the requested envelope slugs.

    Returns ({slug: [Bundle, ...]}, {slug: display_title}). A requested slug
    with no ok bundles yields an empty list (caller decides how to report it).
    """
    db_envelopes = await database.get_envelopes(term)
    titles: dict[str, str] = {}
    raw: list[tuple] = []  # (slug, title, env_id, captured_at, idx, ids, courses)
    for env in db_envelopes:
        if env["slug"] not in slugs:
            continue
        titles[env["slug"]] = env["display_title"]
        for b in env["bundles"]:
            if b["ok"] != 1:
                continue
            raw.append((
                env["slug"], env["display_title"], b["env_id"],
                env["captured_at"], b["bundle_index"],
                [s["section_id"] for s in b["sections"] if s.get("section_id") is not None],
                [s["course_code"] for s in b["sections"]],
            ))

    all_ids = [sid for r in raw for sid in r[5]]
    sections_by_id = await load_sections_by_ids(term, all_ids)

    by_slug: dict[str, list[Bundle]] = {s: [] for s in slugs}
    for slug, title, env_id, captured_at, idx, sec_ids, courses in raw:
        secs = tuple(
            _with_envelope(sections_by_id[sid], title, env_id, captured_at)
            for sid in sec_ids if sid in sections_by_id
        )
        by_slug.setdefault(slug, []).append(
            Bundle(slug, title, env_id, captured_at, idx, secs, frozenset(courses))
        )
    return by_slug, titles


def _apply_bundle(bundle: Bundle, req: SolverRequest,
                  blocked: list[tuple[str, int, int]]) -> tuple[bool, dict[str, int]]:
    """Hard filters apply to the whole bundle: keep it only if every section
    passes. Returns (keep, removed_count_by_filter)."""
    kept, removed = apply_hard_filters(
        list(bundle.sections),
        allow_online=req.allow_online,
        allow_salt_lake=req.allow_salt_lake,
        skip_full_sections=req.skip_full_sections,
        earliest_start_min=req.earliest_start_min,
        latest_end_min=req.latest_end_min,
        blocked_times=blocked,
        days_off=req.days_off,
    )
    return len(kept) == len(bundle.sections), removed


# ── Public entry point ────────────────────────────────────────────────────────

async def solve_schedule(req: SolverRequest) -> SolverResponse:
    """Build, score, and return up to 3 meaningfully-different schedules."""
    codes = await _resolve_courses(req.term, req.courses)
    loaded = await load_sections(req.term, codes)

    # Defensive: a code passed resolution but somehow has no section rows.
    for code in codes:
        if code not in loaded or not loaded[code]:
            raise SolverError(
                f"No sections in the database for course {code!r} in term {req.term}.",
                course=code,
            )

    blocked = _blocked_tuples(req)
    instructors = _normalize_instructors(req.instructors)

    env_slugs = list(dict.fromkeys(req.envelopes or []))  # dedupe, keep order
    env_bundles: dict[str, list[Bundle]] = {}
    env_titles: dict[str, str] = {}
    if env_slugs:
        env_bundles, env_titles = await _load_envelope_bundles(req.term, env_slugs)
        for slug in env_slugs:
            if slug not in env_titles:
                raise SolverError(
                    f"No enrollment envelope {slug!r} for term {req.term}."
                )

    # A plain course that a selected envelope covers is satisfied by the
    # envelope's section (removed from the plain-course list).
    env_courses: dict[str, set[str]] = {
        slug: {c for b in bundles for c in b.courses}
        for slug, bundles in env_bundles.items()
    }
    covered = [c for c in codes
               if any(c in env_courses.get(slug, set()) for slug in env_slugs)]
    plain_codes = [c for c in codes if c not in covered]

    # Restrict each envelope to bundles containing every *listed* covered
    # course, but only when some (not all) of its bundles contain it.
    env_candidates: dict[str, list[Bundle]] = {}
    for slug in env_slugs:
        bundles = list(env_bundles.get(slug, []))
        for c in covered:
            if c not in env_courses.get(slug, set()):
                continue
            containing = [b for b in bundles if c in b.courses]
            if len(containing) < len(bundles):
                bundles = containing
        env_candidates[slug] = bundles

    # Filter plain courses.
    filtered: dict[str, list[Section]] = {}
    for code in plain_codes:
        filtered[code], _ = _apply(req, loaded[code], blocked, instructors.get(code))

    # Filter envelope bundles (whole-bundle hard filters).
    env_kept: dict[str, list[Bundle]] = {}
    envelope_problems: list[EnvelopeFilterReport] = []
    for slug in env_slugs:
        kept: list[Bundle] = []
        removed_agg = {name: 0 for name in FILTER_NAMES}
        for bundle in env_candidates[slug]:
            keep, removed = _apply_bundle(bundle, req, blocked)
            if keep:
                kept.append(bundle)
            else:
                for k, v in removed.items():
                    removed_agg[k] += v
        env_kept[slug] = kept
        if env_candidates[slug] and not kept:
            envelope_problems.append(EnvelopeFilterReport(
                slug=slug,
                title=env_titles[slug],
                removed_by={k: v for k, v in removed_agg.items() if v},
            ))

    # Early out: a plain course has no sections left, or an envelope has no
    # surviving bundle.
    if any(not filtered[code] for code in plain_codes) or envelope_problems:
        diag = diagnose_bottleneck(plain_codes, loaded, req)
        diag.envelope_problems = envelope_problems
        return SolverResponse(schedules=[], cap_hit=False, bottleneck=diag)

    # Build decision groups: plain courses (single-section options) then
    # envelopes (whole-bundle options).
    groups: list[tuple[str, list[tuple[Section, ...]]]] = []
    for code in plain_codes:
        groups.append((code, [(s,) for s in filtered[code]]))
    for slug in env_slugs:
        groups.append((slug, [tuple(b.sections) for b in env_kept[slug]]))

    groups.sort(key=lambda g: (len(g[1]), g[0]))
    options = [g[1] for g in groups]
    assignments, cap_hit = search_options(options)

    if not assignments:
        diag = diagnose_bottleneck(plain_codes, loaded, req)
        diag.envelope_problems = envelope_problems
        return SolverResponse(schedules=[], cap_hit=cap_hit, bottleneck=diag)

    scored: list[tuple[float, dict, tuple[Section, ...]]] = []
    for assignment in assignments:
        score, metrics = score_schedule(list(assignment), req.preferences)
        scored.append((score, metrics, assignment))

    scored.sort(key=_sort_key)

    n = len(groups)
    # A bundle is one choice: two schedules share it only when they contain
    # *all* of its sections. Counting individual shared section ids would
    # wrongly treat bundles that merely overlap on one section as duplicates.
    option_idsets = [
        [frozenset(x.id for x in opt) for opt in g[1]] for g in groups
    ]

    def _differs(a: frozenset[int], b: frozenset[int]) -> bool:
        shared = 0
        for idsets in option_idsets:
            if any(o <= a and o <= b for o in idsets):
                shared += 1
        return 2 * shared <= n

    chosen: list[tuple[float, dict, tuple[Section, ...]]] = []
    # Seed the "already chosen" set with schedules the caller has already seen
    # so a "generate more" request returns the next batch instead of repeats.
    seen_ids: list[frozenset[int]] = [frozenset(ids) for ids in (req.exclude or [])]
    for entry in scored:
        if len(chosen) >= req.limit:
            break
        ids = frozenset(s.id for s in entry[2])
        if all(_differs(ids, c) for c in seen_ids):
            chosen.append(entry)
            seen_ids.append(ids)

    # ``has_more`` is definitive only when the search completed; when it hit
    # the cap there may be schedules we never enumerated, so report "maybe".
    has_more = cap_hit or any(
        all(
            _differs(frozenset(s.id for s in e[2]), c)
            for c in seen_ids
        )
        for e in scored
    )

    schedules = [
        _schedule_out(score, metrics, assignment)
        for score, metrics, assignment in chosen
    ]
    return SolverResponse(
        schedules=schedules, cap_hit=cap_hit, has_more=has_more, bottleneck=None
    )

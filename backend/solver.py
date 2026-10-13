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
from dataclasses import dataclass
from typing import Optional

import aiosqlite

from . import database, rmp
from .matching import closest_courses, normalize_course_code
from .models import (
    BottleneckDiagnosis,
    CourseFilterReport,
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

FILTER_NAMES = ("online", "full", "earliest_latest", "blocked_times")
RELAXATION_LABELS = [
    "earliest/latest window",
    "blocked times",
    "online allowed",
    "skip full sections",
]


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
    title: Optional[str] = None


# ── Conflict detection ────────────────────────────────────────────────────────

def meetings_overlap(a: Meeting, b: Meeting) -> bool:
    """Two meetings conflict iff same day and their [start, end) intervals
    overlap. Back-to-back (a.end == b.start) does NOT conflict (strict <)."""
    return a.day == b.day and a.start_min < b.end_min and b.start_min < a.end_min


def sections_conflict(a: Section, b: Section) -> bool:
    """Two sections conflict if ANY of their meetings overlap."""
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
                   s.seats_open, s.instructor_id,
                   p.byu_name, p.avg_difficulty, p.avg_rating,
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
            meetings=tuple(meetings_by_section.get(r["id"], [])),
            title=r["title"],
        )
        out.setdefault(r["course_code"], []).append(section)
    return out


# ── Hard filters ──────────────────────────────────────────────────────────────

def _overlaps_blocked(m: Meeting, blocked_times: list[tuple[str, int, int]]) -> bool:
    for day, start, end in blocked_times:
        if m.day == day and m.start_min < end and start < m.end_min:
            return True
    return False


def apply_hard_filters(
    sections: list[Section],
    *,
    allow_online: bool,
    skip_full_sections: bool,
    earliest_start_min: int,
    latest_end_min: int,
    blocked_times: list[tuple[str, int, int]],
) -> tuple[list[Section], dict[str, int]]:
    """Remove any section that violates a hard constraint.

    Returns (kept_sections, removed_count_by_filter). Async sections (no
    meetings) never violate the time/blocked filters.
    """
    kept: list[Section] = []
    removed = {name: 0 for name in FILTER_NAMES}
    for s in sections:
        if s.is_online and not allow_online:
            removed["online"] += 1
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

def score_schedule(
    sections: list[Section],
    preferences: list[str],
    days_off: list[str],
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

    # days_off: fraction of requested off-days that have zero meetings.
    off_set = set(days_off)
    if off_set:
        free_days = sum(1 for d in off_set if d not in by_day)
        days_off_score = (free_days / len(off_set)) * 100.0
    else:
        days_off_score = 100.0

    # no_gaps: penalize idle minutes between classes; >3h gaps hurt more.
    total_gap = 0
    effective_gap = 0.0
    for slots in by_day.values():
        for i in range(len(slots) - 1):
            gap = slots[i + 1][0] - slots[i][1]
            total_gap += gap
            effective_gap += gap + max(0, gap - GAP_OVER_LIMIT_MIN)
    no_gaps_score = max(0.0, 100.0 * (1.0 - effective_gap / NO_GAPS_NORMALIZER_MIN))

    # easy: average ease over DISTINCT instructors (unknown counts as neutral 50).
    by_instructor: dict[Optional[int], Section] = {}
    for s in sections:
        by_instructor.setdefault(s.instructor_id, s)
    eases = [s.ease_score for s in by_instructor.values()]
    avg_ease = sum(eases) / len(eases) if eases else 50.0

    components = {
        "easy": avg_ease,
        "days_off": days_off_score,
        "no_gaps": no_gaps_score,
    }
    total = 0.0
    for rank, pref in enumerate(preferences):
        total += PREFERENCE_WEIGHTS[rank] * components[pref] / 100.0

    earliest = min(m.start_min for m in meetings) if meetings else None
    latest = max(m.end_min for m in meetings) if meetings else None

    metrics = {
        "days_on_campus": len(by_day),
        "total_gap_minutes": total_gap,
        "average_ease": round(avg_ease, 2),
        "earliest_start": earliest,
        "latest_end": latest,
        "professors_without_ratings": sum(
            1 for s in by_instructor.values() if s.lacks_ratings
        ),
    }
    return round(total, 3), metrics


# ── Backtracking search ───────────────────────────────────────────────────────

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
    start = time.monotonic()
    n = len(courses_sections)
    current: list[Optional[Section]] = [None] * n
    assignments: list[tuple[Section, ...]] = []
    stopped = False

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
            assignments.append(tuple(current))  # type: ignore[arg-type]
            return
        _, sections = courses_sections[i]
        for section in sections:
            ok = True
            for j in range(i):
                prev = current[j]
                if prev is not None and sections_conflict(section, prev):
                    ok = False
                    break
            if ok:
                current[i] = section
                backtrack(i + 1)
                current[i] = None
                if stopped:
                    return

    backtrack(0)
    return assignments, stopped


def _sort_key(item: tuple[float, dict, tuple[Section, ...]]):
    score, metrics, assignment = item
    section_ids = tuple(sorted(s.id for s in assignment))
    return (
        -score,
        metrics["days_on_campus"],
        metrics["total_gap_minutes"],
        -metrics["average_ease"],
        section_ids,
    )


def _shared_sections(a: tuple[Section, ...], b: tuple[Section, ...]) -> int:
    return sum(1 for sa, sb in zip(a, b) if sa.id == sb.id)


def _meaningfully_different(a: tuple[Section, ...], b: tuple[Section, ...], n: int) -> bool:
    """True when the two schedules share a section for <= half the courses."""
    return 2 * _shared_sections(a, b) <= n


# ── Bottleneck diagnosis ──────────────────────────────────────────────────────

def _blocked_tuples(req: SolverRequest) -> list[tuple[str, int, int]]:
    return [(_norm_day(b.day), b.start_min, b.end_min) for b in req.blocked_times]


def _apply(req: SolverRequest, sections: list[Section],
           blocked: list[tuple[str, int, int]]) -> tuple[list[Section], dict[str, int]]:
    return apply_hard_filters(
        sections,
        allow_online=req.allow_online,
        skip_full_sections=req.skip_full_sections,
        earliest_start_min=req.earliest_start_min,
        latest_end_min=req.latest_end_min,
        blocked_times=blocked,
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
    filtered: dict[str, list[Section]] = {}
    no_sections: list[CourseFilterReport] = []
    for code in codes:
        kept, removed = _apply(req, loaded[code], blocked)
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
    for label in RELAXATION_LABELS:
        rreq = _relaxed_request(req, label)
        rblocked = _blocked_tuples(rreq)
        courses_sections: list[tuple[str, list[Section]]] = []
        ok = True
        for code in codes:
            kept, _ = _apply(rreq, loaded[code], rblocked)
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


# ── Public entry point ────────────────────────────────────────────────────────

async def solve_schedule(req: SolverRequest) -> SolverResponse:
    """Build, score, and return up to 3 meaningfully-different schedules."""
    days_off = [_norm_day(d) for d in req.days_off]
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
    filtered: dict[str, list[Section]] = {}
    for code in codes:
        filtered[code], _ = _apply(req, loaded[code], blocked)

    # If any course has zero remaining sections, there is no schedule at all.
    if any(not filtered[code] for code in codes):
        return SolverResponse(
            schedules=[],
            cap_hit=False,
            bottleneck=diagnose_bottleneck(codes, loaded, req),
        )

    courses_sections = sorted(
        ((code, filtered[code]) for code in codes),
        key=lambda cs: (len(cs[1]), cs[0]),
    )
    assignments, cap_hit = search(courses_sections)

    if not assignments:
        return SolverResponse(
            schedules=[],
            cap_hit=cap_hit,
            bottleneck=diagnose_bottleneck(codes, loaded, req),
        )

    scored: list[tuple[float, dict, tuple[Section, ...]]] = []
    for assignment in assignments:
        score, metrics = score_schedule(list(assignment), req.preferences, days_off)
        scored.append((score, metrics, assignment))

    scored.sort(key=_sort_key)

    n = len(codes)
    chosen: list[tuple[float, dict, tuple[Section, ...]]] = []
    for entry in scored:
        if len(chosen) >= 3:
            break
        if all(_meaningfully_different(entry[2], c[2], n) for c in chosen):
            chosen.append(entry)

    schedules = [
        _schedule_out(score, metrics, assignment)
        for score, metrics, assignment in chosen
    ]
    return SolverResponse(schedules=schedules, cap_hit=cap_hit, bottleneck=None)

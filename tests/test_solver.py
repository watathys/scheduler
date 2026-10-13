"""Tests for the deterministic schedule solver (no network; temp SQLite DB)."""

import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import database, solver  # noqa: E402
from backend.models import SolverRequest  # noqa: E402
from backend.solver import (  # noqa: E402
    Meeting,
    Section,
    SolverError,
    apply_hard_filters,
    score_schedule,
    sections_conflict,
    solve_schedule,
)

TERM = "20271"


def M(day, start, end, building=None, room=None):
    return Meeting(day, start, end, building, room)


def sec(sid, meetings=(), *, is_online=False, seats_open=10, instructor_id=None,
        name=None, ease=50.0, conf=0.0, lacks=True, code="C", number="001", num=None):
    """Build a Section for pure-function tests.

    ``num`` is the stored RMP ``num_ratings`` (default ``None`` => no RMP data,
    matching a professor with no RateMyProfessors page).
    """
    return Section(id=sid, course_code=code, section_number=number, is_online=is_online,
                   seats_open=seats_open, instructor_id=instructor_id, instructor_name=name,
                   ease_score=ease, ease_confidence=conf, lacks_ratings=lacks,
                   meetings=tuple(meetings), title=None, num_ratings=num)


# ── DB fixture & seeding helpers ─────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path):
    orig = database.DB
    database.DB = str(tmp_path / "solver_test.db")
    asyncio.run(database.init_schedule_db())
    yield
    database.DB = orig


def add_prof(person_id, name, *, rating=None, diff=None, wta=None, num=None):
    async def run():
        pid = await database.upsert_professor(person_id, name)
        if num is not None:
            await database.update_professor_ratings(
                pid, None, name, diff, rating, wta, num, 1.0
            )
        return pid
    return asyncio.run(run())


def add_section(code, number, meetings=(), *, is_online=False, seats_open=10,
                instructor_id=None, title=None):
    async def run():
        sid = await database.upsert_section(
            TERM, code, number, title=title, is_online=is_online,
            seats_open=seats_open, instructor_id=instructor_id,
        )
        await database.replace_section_meetings(
            sid, [(m.day, m.start_min, m.end_min, m.building, m.room) for m in meetings]
        )
        return sid
    return asyncio.run(run())


def solve(**kw):
    defaults = dict(term=TERM, courses=[], preferences=["easy", "no_gaps"])
    defaults.update(kw)
    return asyncio.run(solve_schedule(SolverRequest(**defaults)))


# ── Conflict detection ───────────────────────────────────────────────────────

def test_meetings_overlap_same_day():
    assert sections_conflict(
        sec(1, [M("Monday", 480, 540)]), sec(2, [M("Monday", 500, 560)])
    )


def test_back_to_back_is_not_a_conflict():
    assert not sections_conflict(
        sec(1, [M("Monday", 480, 540)]), sec(2, [M("Monday", 540, 600)])
    )


def test_different_day_is_not_a_conflict():
    assert not sections_conflict(
        sec(1, [M("Monday", 480, 540)]), sec(2, [M("Tuesday", 480, 540)])
    )


def test_async_online_section_never_conflicts():
    async_sec = sec(1, [])  # async online: no meetings
    other = sec(2, [M("Monday", 480, 540)])
    assert not sections_conflict(async_sec, other)
    assert not sections_conflict(other, async_sec)


# ── Hard filters ─────────────────────────────────────────────────────────────

def test_filter_earliest_latest_window():
    sections = [
        sec(1, [M("Monday", 480, 540)]),    # starts before earliest
        sec(2, [M("Monday", 600, 660)]),    # OK
        sec(3, [M("Monday", 1020, 1080)]),  # ends after latest
    ]
    kept, removed = apply_hard_filters(
        sections, allow_online=True, skip_full_sections=True,
        earliest_start_min=540, latest_end_min=1020, blocked_times=[],
    )
    assert [s.id for s in kept] == [2]
    assert removed["earliest_latest"] == 2


def test_filter_blocked_time():
    sections = [
        sec(1, [M("Monday", 480, 540)]),
        sec(2, [M("Monday", 600, 660)]),
    ]
    kept, removed = apply_hard_filters(
        sections, allow_online=True, skip_full_sections=True,
        earliest_start_min=0, latest_end_min=1440,
        blocked_times=[("Monday", 500, 550)],
    )
    assert [s.id for s in kept] == [2]
    assert removed["blocked_times"] == 1


def test_filter_online_when_disallowed():
    sections = [
        sec(1, [M("Monday", 480, 540)], is_online=True),
        sec(2, [M("Monday", 600, 660)], is_online=False),
    ]
    kept, removed = apply_hard_filters(
        sections, allow_online=False, skip_full_sections=True,
        earliest_start_min=0, latest_end_min=1440, blocked_times=[],
    )
    assert [s.id for s in kept] == [2]
    assert removed["online"] == 1

    kept2, _ = apply_hard_filters(
        sections, allow_online=True, skip_full_sections=True,
        earliest_start_min=0, latest_end_min=1440, blocked_times=[],
    )
    assert len(kept2) == 2


def test_filter_full_sections():
    sections = [
        sec(1, [M("Monday", 480, 540)], seats_open=0),
        sec(2, [M("Monday", 600, 660)], seats_open=5),
        sec(3, [M("Monday", 700, 760)], seats_open=None),  # unknown -> keep
    ]
    kept, removed = apply_hard_filters(
        sections, allow_online=True, skip_full_sections=True,
        earliest_start_min=0, latest_end_min=1440, blocked_times=[],
    )
    assert [s.id for s in kept] == [2, 3]
    assert removed["full"] == 1

    kept2, _ = apply_hard_filters(
        sections, allow_online=True, skip_full_sections=False,
        earliest_start_min=0, latest_end_min=1440, blocked_times=[],
    )
    assert len(kept2) == 3  # full section retained when skip is off


# ── Scoring order per preference rank ────────────────────────────────────────

def test_scoring_easy_rank1_prefers_higher_ease():
    prefs = ["easy", "no_gaps"]
    # A professor only counts toward ease when they have RMP data (num >= 1).
    high = score_schedule(
        [sec(1, [M("Monday", 480, 540)], ease=90.0, num=50, instructor_id=1)], prefs)
    low = score_schedule(
        [sec(2, [M("Monday", 480, 540)], ease=50.0, num=50, instructor_id=2)], prefs)
    assert high[0] > low[0]


def test_ease_excludes_professors_without_rmp_pages():
    # Two distinct instructors: one rated, one with no RMP page. The unrated
    # professor must not drag the average toward the neutral 50.
    _, metrics = score_schedule(
        [
            sec(1, [M("Monday", 480, 540)], ease=80.0, num=50, instructor_id=1),
            sec(2, [M("Monday", 600, 660)], ease=50.0, num=None, instructor_id=2),
        ],
        ["easy", "no_gaps"],
    )
    assert metrics["average_ease"] == 80.0
    assert metrics["professors_without_ratings"] == 1
    # The unrated professor still appears in the breakdown as data_status "none".
    statuses = {b["data_status"] for b in metrics["ease_breakdown"]}
    assert statuses == {"rated", "none"}


def test_ease_unavailable_when_no_professor_has_ratings():
    # Every instructor lacks an RMP page -> ease cannot be calculated.
    score, metrics = score_schedule(
        [
            sec(1, [M("Monday", 480, 540)], ease=50.0, num=None, instructor_id=1),
            sec(2, [M("Monday", 600, 660)], ease=50.0, num=None, instructor_id=2),
        ],
        ["easy", "no_gaps"],
    )
    assert metrics["average_ease"] is None
    assert metrics["professors_without_ratings"] == 2
    assert all(b["data_status"] == "none" for b in metrics["ease_breakdown"])
    # Scoring still produces a finite result using the neutral easy component.
    assert score == score  # not NaN
    assert 0.0 <= score <= 100.0


def test_scoring_no_gaps_rank1_prefers_fewer_gaps():
    prefs = ["no_gaps", "easy"]
    gappy = score_schedule(
        [sec(1, [M("Monday", 480, 540)]), sec(2, [M("Monday", 660, 720)])],
        prefs,
    )
    tight = score_schedule(
        [sec(3, [M("Monday", 480, 540)]), sec(4, [M("Monday", 540, 600)])],
        prefs,
    )
    assert tight[0] > gappy[0]


def test_days_off_is_hard(db):
    add_section("MKTG 201", "001", [M("Friday", 480, 540)])
    res = solve(courses=["MKTG 201"], days_off=["Friday"])
    assert res.schedules == []
    assert res.bottleneck is not None
    assert res.bottleneck.no_sections[0].course == "MKTG 201"
    assert res.bottleneck.no_sections[0].removed_by.get("days_off") == 1


# ── Search cap ───────────────────────────────────────────────────────────────

def test_search_caps_at_max_schedules():
    courses = [
        ("A", [sec(1 + i, [M("Monday", 480 + i * 60, 540 + i * 60)]) for i in range(5)]),
        ("B", [sec(10 + i, [M("Monday", 480 + i * 60, 540 + i * 60)]) for i in range(5)]),
    ]
    assignments, hit_cap = solver.search(courses, max_schedules=10)
    assert len(assignments) == 10
    assert hit_cap is True


# ── Determinism ──────────────────────────────────────────────────────────────

def test_deterministic_output(db):
    add_section("MKTG 201", "001", [M("Monday", 480, 540)])
    add_section("MKTG 201", "002", [M("Monday", 600, 660)])
    add_section("STRAT 305", "001", [M("Monday", 480, 540)])
    add_section("STRAT 305", "002", [M("Monday", 600, 660)])
    args = dict(courses=["MKTG 201", "STRAT 305"])
    a = solve(**args).model_dump()
    b = solve(**args).model_dump()
    assert a == b


# ── Diversity of the top 3 ───────────────────────────────────────────────────

def _shared_section_ids(sa, sb):
    """Count courses where two response schedules pick the same section id."""
    a = {s.course_code: s.id for s in sa.sections}
    b = {s.course_code: s.id for s in sb.sections}
    return sum(1 for code in a if a[code] == b[code])


def test_top3_are_meaningfully_different(db):
    courses = ["MKTG 201", "STRAT 305", "FIN 201"]
    for code in courses:
        for k in range(4):
            start = 480 + k * 120
            add_section(code, f"{k + 1:03d}", [M("Monday", start, start + 50)])
    res = solve(courses=courses)
    assert len(res.schedules) == 3
    scheds = res.schedules
    n = len(courses)
    for i in range(len(scheds)):
        for j in range(i + 1, len(scheds)):
            shared = _shared_section_ids(scheds[i], scheds[j])
            assert shared * 2 <= n


def test_fewer_than_three_distinct_returns_what_exists(db):
    # Two courses, each with one section at the same single slot -> exactly one
    # valid schedule.
    add_section("MKTG 201", "001", [M("Monday", 480, 540)])
    add_section("STRAT 305", "001", [M("Monday", 600, 660)])
    res = solve(courses=["MKTG 201", "STRAT 305"])
    assert len(res.schedules) == 1


# ── Course normalization & closest-match error ───────────────────────────────

def test_normalization_matches_case_insensitive(db):
    add_section("MKTG 201", "001", [M("Monday", 480, 540)])
    res = solve(courses=["mktg201"])
    assert len(res.schedules) == 1
    assert res.schedules[0].sections[0].course_code == "MKTG 201"


def test_closest_match_error(db):
    add_section("MKTG 201", "001", [M("Monday", 480, 540)])
    add_section("MKTG 202", "001", [M("Monday", 600, 660)])
    with pytest.raises(SolverError) as exc:
        solve(courses=["MKTG 209"])
    assert exc.value.suggestions
    assert "MKTG 201" in exc.value.suggestions or "MKTG 202" in exc.value.suggestions


def test_zero_sections_in_db_raises_named_error(db):
    add_section("MKTG 201", "001", [M("Monday", 480, 540)])
    with pytest.raises(SolverError) as exc:
        solve(courses=["ECON 999"])
    assert "ECON 999" in str(exc.value)


# ── Online semantics ─────────────────────────────────────────────────────────

def test_async_online_included_and_listed_separately(db):
    add_section("MKTG 201", "900", is_online=True)  # async, no meetings
    add_section("STRAT 305", "001", [M("Monday", 600, 660)])
    res = solve(courses=["MKTG 201", "STRAT 305"], allow_online=True)
    assert len(res.schedules) == 1
    sched = res.schedules[0]
    assert len(sched.sections) == 1
    assert sched.sections[0].course_code == "STRAT 305"
    assert len(sched.async_online) == 1
    assert sched.async_online[0].course_code == "MKTG 201"


def test_online_only_course_reported_as_bottleneck(db):
    add_section("MKTG 201", "900", is_online=True)
    res = solve(courses=["MKTG 201"], allow_online=False)
    assert res.schedules == []
    assert res.bottleneck is not None
    assert res.bottleneck.no_sections[0].course == "MKTG 201"
    assert "online" in res.bottleneck.no_sections[0].removed_by
    assert "online allowed" in res.bottleneck.feasible_relaxations


# ── Bottleneck diagnosis ─────────────────────────────────────────────────────

def test_bottleneck_always_conflicting_pair(db):
    add_section("MKTG 201", "001", [M("Monday", 480, 540)])
    add_section("STRAT 305", "001", [M("Monday", 480, 540)])
    res = solve(courses=["MKTG 201", "STRAT 305"])
    assert res.schedules == []
    assert res.bottleneck is not None
    assert ("MKTG 201", "STRAT 305") in res.bottleneck.always_conflicting_pairs
    assert res.bottleneck.feasible_relaxations == []


def test_course_empty_after_filter_reports_filter_and_relaxation(db):
    add_section("MKTG 201", "001", [M("Monday", 480, 540)])  # 8:00-9:00
    res = solve(courses=["MKTG 201"], earliest_start_min=600, latest_end_min=1080)
    assert res.schedules == []
    assert res.bottleneck is not None
    report = res.bottleneck.no_sections[0]
    assert report.course == "MKTG 201"
    assert report.removed_by.get("earliest_latest") == 1
    assert "earliest/latest window" in res.bottleneck.feasible_relaxations


# ── Output shape (instructor ease reaches the response) ─────────────────────

def test_output_has_instructor_name_and_ease(db):
    pid = add_prof("p-1", "Melissa Larson", rating=4.5, diff=2.0, wta=90.0, num=50)
    add_section("MKTG 201", "001", [M("Monday", 480, 540)], instructor_id=pid)
    res = solve(courses=["MKTG 201"])
    out = res.schedules[0].sections[0]
    assert out.instructor_name == "melissa larson"
    assert out.ease_score > 50
    assert out.ease_confidence > 0.8


# ── Performance: 8 courses x 15 sections ─────────────────────────────────────

def test_performance_8_courses_x_15_sections(db):
    codes = ["MKTG 100", "FIN 100", "ACC 100", "ISYS 100",
             "STRAT 100", "ECON 100", "MATH 100", "STAT 100"]
    for code in codes:
        for k in range(15):
            start = 480 + k * 60
            add_section(code, f"{k + 1:03d}", [M("Monday", start, start + 50)])

    t0 = time.monotonic()
    res = solve(courses=codes)
    elapsed = time.monotonic() - t0

    assert len(res.schedules) == 3
    assert elapsed < 8.0


def test_scoring_fewer_days_rank1_prefers_fewer_campus_days():
    prefs = ["fewer_days", "easy", "no_gaps"]
    compact = score_schedule(
        [sec(1, [M("Monday", 480, 540)]), sec(2, [M("Monday", 540, 600)])], prefs)
    spread = score_schedule(
        [sec(3, [M("Monday", 480, 540)]), sec(4, [M("Wednesday", 480, 540)])], prefs)
    assert compact[0] > spread[0]
    assert compact[1]["days_on_campus"] == 1 and spread[1]["days_on_campus"] == 2

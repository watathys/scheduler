"""Tests for the deterministic schedule solver (no network; temp SQLite DB)."""

import asyncio
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import database, envelopes, solver  # noqa: E402
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
        name=None, ease=50.0, conf=0.0, lacks=True, code="C", number="001", num=None,
        start_date=None, end_date=None, is_salt_lake=False):
    """Build a Section for pure-function tests.

    ``num`` is the stored RMP ``num_ratings`` (default ``None`` => no RMP data,
    matching a professor with no RateMyProfessors page).
    """
    return Section(id=sid, course_code=code, section_number=number, is_online=is_online,
                   seats_open=seats_open, instructor_id=instructor_id, instructor_name=name,
                   ease_score=ease, ease_confidence=conf, lacks_ratings=lacks,
                   meetings=tuple(meetings), title=None, num_ratings=num,
                   start_date=start_date, end_date=end_date, is_salt_lake=is_salt_lake)


# ── DB fixture & seeding helpers ─────────────────────────────────────────────

@pytest.fixture()
def db(tmp_path):
    orig = database.DB
    orig_env_dir = envelopes.ENVELOPES_DIR
    database.DB = str(tmp_path / "solver_test.db")
    envelopes.ENVELOPES_DIR = tmp_path / "envelopes"
    asyncio.run(database.init_schedule_db())
    yield
    database.DB = orig
    envelopes.ENVELOPES_DIR = orig_env_dir


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
                instructor_id=None, title=None, start_date=None, end_date=None):
    async def run():
        sid = await database.upsert_section(
            TERM, code, number, title=title, is_online=is_online,
            seats_open=seats_open, instructor_id=instructor_id,
            start_date=start_date, end_date=end_date,
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


def test_filter_salt_lake_when_disallowed():
    sections = [
        sec(1, [M("Monday", 480, 540, "SLC", "101")], is_salt_lake=True),
        sec(2, [M("Monday", 600, 660, "TNRB", "101")]),
    ]
    kept, removed = apply_hard_filters(
        sections, allow_online=True, allow_salt_lake=False, skip_full_sections=True,
        earliest_start_min=0, latest_end_min=1440, blocked_times=[],
    )
    assert [s.id for s in kept] == [2]
    assert removed["salt_lake"] == 1

    kept2, _ = apply_hard_filters(
        sections, allow_online=True, allow_salt_lake=True, skip_full_sections=True,
        earliest_start_min=0, latest_end_min=1440, blocked_times=[],
    )
    assert len(kept2) == 2


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


def test_instructor_filter_keeps_only_selected_professor(db):
    pid1 = add_prof("p-1", "Alice")
    pid2 = add_prof("p-2", "Bob")
    add_section("MKTG 201", "001", [M("Monday", 480, 540)], instructor_id=pid1)
    add_section("MKTG 201", "002", [M("Monday", 600, 660)], instructor_id=pid2)

    res = solve(courses=["MKTG 201"], instructors={"MKTG 201": [str(pid1)]})
    assert len(res.schedules) == 1
    assert res.schedules[0].sections[0].instructor_name == "alice"


def test_instructor_filter_online_keeps_async_section(db):
    add_section("MKTG 201", "001", [M("Monday", 480, 540)])
    add_section("MKTG 201", "900", is_online=True)

    res = solve(courses=["MKTG 201"], allow_online=True, instructors={"MKTG 201": ["online"]})
    assert len(res.schedules) == 1
    assert res.schedules[0].sections == []
    assert [s.course_code for s in res.schedules[0].async_online] == ["MKTG 201"]


def test_instructor_filter_no_match_reports_bottleneck(db):
    pid = add_prof("p-1", "Alice")
    add_section("MKTG 201", "001", [M("Monday", 480, 540)], instructor_id=pid)
    add_section("MKTG 201", "002", [M("Monday", 600, 660)])

    res = solve(courses=["MKTG 201"], instructors={"MKTG 201": ["999999"]})
    assert res.schedules == []
    assert res.bottleneck is not None
    assert res.bottleneck.no_sections[0].removed_by.get("instructor") == 2


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


def test_generate_more_returns_next_batch_then_exhausts(db):
    # Two courses, two sections each -> exactly four conflict-free schedules.
    add_section("MKTG 201", "001", [M("Monday", 480, 540)])
    add_section("MKTG 201", "002", [M("Monday", 600, 660)])
    add_section("STRAT 305", "001", [M("Monday", 720, 780)])
    add_section("STRAT 305", "002", [M("Monday", 840, 900)])

    first = solve(courses=["MKTG 201", "STRAT 305"])
    assert len(first.schedules) == 3
    assert first.has_more is True

    seen = [[s.id for s in sched.sections] for sched in first.schedules]
    second = solve(courses=["MKTG 201", "STRAT 305"], exclude=seen)
    assert len(second.schedules) == 1
    assert second.has_more is False
    new_ids = {s.id for s in second.schedules[0].sections}
    assert new_ids not in [set(ids) for ids in seen]

    seen_all = seen + [[s.id for s in sched.sections] for sched in second.schedules]
    third = solve(courses=["MKTG 201", "STRAT 305"], exclude=seen_all)
    assert third.schedules == []
    assert third.has_more is False


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


def test_salt_lake_section_filtered_by_default(db):
    add_section("MKTG 201", "001", [M("Monday", 480, 540, "SLC", "101")])
    res = solve(courses=["MKTG 201"])  # allow_salt_lake defaults to False
    assert res.schedules == []
    assert res.bottleneck is not None
    report = res.bottleneck.no_sections[0]
    assert report.course == "MKTG 201"
    assert "salt_lake" in report.removed_by
    assert "salt lake allowed" in res.bottleneck.feasible_relaxations


def test_salt_lake_section_allowed_when_requested(db):
    add_section("MKTG 201", "001", [M("Monday", 480, 540, "SLC", "101")])
    res = solve(courses=["MKTG 201"], allow_salt_lake=True)
    assert len(res.schedules) == 1
    assert res.schedules[0].sections[0].course_code == "MKTG 201"


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


# ── Half-semester conflict rule ─────────────────────────────────────────────

def test_half_semester_sections_do_not_conflict():
    term1 = sec(1, [M("Tuesday", 570, 645)], code="MBA 570", number="001",
                start_date="2027-01-06", end_date="2027-02-17")
    term2 = sec(2, [M("Tuesday", 570, 645)], code="MBA 584", number="001",
                start_date="2027-02-18", end_date="2027-04-07")
    assert not sections_conflict(term1, term2)
    assert not sections_conflict(term2, term1)


def test_same_slot_full_term_still_conflicts():
    full1 = sec(3, [M("Tuesday", 570, 645)])
    full2 = sec(4, [M("Tuesday", 570, 645)])
    assert sections_conflict(full1, full2)


def test_solver_half_semester_sections_coexist(db):
    add_section("MBA 570", "001", [M("Tuesday", 570, 645)],
                start_date="2027-01-06", end_date="2027-02-17")
    add_section("MBA 584", "001", [M("Tuesday", 570, 645)],
                start_date="2027-02-18", end_date="2027-04-07")
    res = solve(courses=["MBA 570", "MBA 584"])
    assert len(res.schedules) == 1


# ── Envelope solver integration ─────────────────────────────────────────────

def _env_json(slug, bundles):
    d = envelopes.json_dir_for_term(TERM)
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "title": slug.replace("-", " ").title(),
        "display_title": slug.replace("-", " ").title(),
        "slug": slug,
        "term": TERM,
        "captured_at": "2026-10-13",
        "bundles": bundles,
    }
    (d / f"{slug}.json").write_text(json.dumps(payload), encoding="utf-8")


def _env_sec(course_code, number, meetings, instructor="Prof A"):
    return {
        "course_code": course_code, "section_number": number,
        "instructor_snapshot": instructor, "mode": None, "credits": 3.0,
        "part_of_term": None, "note_flag": False,
        "meetings": [{"day": d, "start_min": s, "end_min": e, "room": r}
                     for (d, s, e, r) in meetings],
    }


def _env_bundle(env_id, sections):
    return {"env_id": env_id, "capacity_snapshot": 10, "seats_snapshot": 10,
            "sections": sections}


def _load_envelopes_ok(slug, bundles):
    _env_json(slug, bundles)
    asyncio.run(envelopes.load_envelopes(TERM))

    async def mark_ok():
        for env in await database.get_envelopes(TERM):
            for b in env["bundles"]:
                await database.update_bundle_ok(b["id"], 1)
    asyncio.run(mark_ok())


def test_envelope_atomicity(db):
    add_section("EXDM 415", "001", [M("Monday", 480, 555)])
    add_section("EXDM 416", "001", [M("Monday", 570, 645)])
    add_section("EXDM 415", "002", [M("Monday", 840, 915)])
    add_section("EXDM 416", "002", [M("Monday", 930, 1005)])
    _load_envelopes_ok("exdm-core", [
        _env_bundle("28001", [
            _env_sec("EXDM 415", "001", [("Monday", 480, 555, "TNRB 1")]),
            _env_sec("EXDM 416", "001", [("Monday", 570, 645, "TNRB 1")]),
        ]),
        _env_bundle("28002", [
            _env_sec("EXDM 415", "002", [("Monday", 840, 915, "TNRB 2")]),
            _env_sec("EXDM 416", "002", [("Monday", 930, 1005, "TNRB 2")]),
        ]),
    ])
    res = solve(courses=["EXDM 415", "EXDM 416"], envelopes=["exdm-core"])
    assert len(res.schedules) == 2
    for sched in res.schedules:
        assert len(sched.sections) == 2
        # All sections come from exactly one bundle (shared env_id).
        assert len({s.env_id for s in sched.sections}) == 1
        assert all(s.via_envelope for s in sched.sections)
    env_ids = {next(iter(sched.sections)).env_id for sched in res.schedules}
    assert env_ids == {"28001", "28002"}


def test_envelope_filter_drops_full_bundle(db):
    add_section("IS 414", "001", [M("Monday", 480, 555)], seats_open=0)
    add_section("IS 455", "001", [M("Monday", 570, 645)], seats_open=0)
    add_section("IS 414", "002", [M("Monday", 840, 915)], seats_open=5)
    add_section("IS 455", "002", [M("Monday", 930, 1005)], seats_open=5)
    _load_envelopes_ok("is-core", [
        _env_bundle("28010", [
            _env_sec("IS 414", "001", [("Monday", 480, 555, "TNRB 1")]),
            _env_sec("IS 455", "001", [("Monday", 570, 645, "TNRB 1")]),
        ]),
        _env_bundle("28011", [
            _env_sec("IS 414", "002", [("Monday", 840, 915, "TNRB 2")]),
            _env_sec("IS 455", "002", [("Monday", 930, 1005, "TNRB 2")]),
        ]),
    ])
    res = solve(courses=["IS 414", "IS 455"], envelopes=["is-core"])
    assert len(res.schedules) == 1
    sched = res.schedules[0]
    assert all(s.env_id == "28011" for s in sched.sections)


def test_envelope_all_bundles_filtered_reports_filter(db):
    add_section("IS 414", "001", [M("Monday", 480, 555)], seats_open=0)
    add_section("IS 455", "001", [M("Monday", 570, 645)], seats_open=0)
    _load_envelopes_ok("is-core", [
        _env_bundle("28010", [
            _env_sec("IS 414", "001", [("Monday", 480, 555, "TNRB 1")]),
            _env_sec("IS 455", "001", [("Monday", 570, 645, "TNRB 1")]),
        ]),
    ])
    res = solve(courses=["IS 414", "IS 455"], envelopes=["is-core"])
    assert res.schedules == []
    assert res.bottleneck is not None
    assert res.bottleneck.envelope_problems
    assert res.bottleneck.envelope_problems[0].slug == "is-core"
    assert "full" in res.bottleneck.envelope_problems[0].removed_by


def test_plain_course_satisfied_by_envelope(db):
    add_section("STRAT 411", "001", [M("Monday", 480, 555)])
    add_section("STRAT 412", "001", [M("Monday", 570, 645)])
    add_section("STRAT 432", "001", [M("Monday", 660, 735)])
    add_section("MKTG 201", "001", [M("Monday", 750, 825)])
    _load_envelopes_ok("strat-core", [
        _env_bundle("28020", [
            _env_sec("STRAT 411", "001", [("Monday", 480, 555, "TNRB 1")]),
            _env_sec("STRAT 412", "001", [("Monday", 570, 645, "TNRB 1")]),
            _env_sec("STRAT 432", "001", [("Monday", 660, 735, "TNRB 1")]),
        ]),
    ])
    res = solve(courses=["STRAT 411", "MKTG 201"], envelopes=["strat-core"])
    assert len(res.schedules) == 1
    sched = res.schedules[0]
    codes = {s.course_code for s in sched.sections}
    assert codes == {"STRAT 411", "STRAT 412", "STRAT 432", "MKTG 201"}
    strat = [s for s in sched.sections if s.course_code.startswith("STRAT")]
    assert all(s.via_envelope for s in strat)
    assert all(s.env_id == "28020" for s in strat)
    plain = next(s for s in sched.sections if s.course_code == "MKTG 201")
    assert plain.via_envelope is None
    assert plain.env_id is None


def test_shared_sections_across_envelopes(db):
    add_section("STRAT 411", "001", [M("Monday", 480, 555)])
    add_section("STRAT 412", "001", [M("Monday", 570, 645)])
    add_section("STRAT 432", "001", [M("Monday", 660, 735)])
    _load_envelopes_ok("core-a", [
        _env_bundle("28030", [
            _env_sec("STRAT 411", "001", [("Monday", 480, 555, "TNRB 1")]),
            _env_sec("STRAT 412", "001", [("Monday", 570, 645, "TNRB 1")]),
        ]),
    ])
    _load_envelopes_ok("core-b", [
        _env_bundle("28031", [
            _env_sec("STRAT 412", "001", [("Monday", 570, 645, "TNRB 1")]),
            _env_sec("STRAT 432", "001", [("Monday", 660, 735, "TNRB 1")]),
        ]),
    ])
    res = solve(courses=["STRAT 411", "STRAT 432"], envelopes=["core-a", "core-b"])
    assert len(res.schedules) == 1
    sched = res.schedules[0]
    codes = [s.course_code for s in sched.sections]
    assert codes == ["STRAT 411", "STRAT 412", "STRAT 432"]
    # The shared section appears once.
    assert len(sched.sections) == 3

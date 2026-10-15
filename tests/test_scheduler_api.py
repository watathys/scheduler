"""Tests for the scheduler API router.

No network: background jobs are disabled and the LLM key is removed so
parse/explain use their deterministic fallbacks. A temp SQLite DB backs every
test.
"""

import asyncio
import base64
import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.main as main  # noqa: E402
from backend import database, envelopes, scheduler_api  # noqa: E402
from backend.config import settings  # noqa: E402

TERM = "20271"


# ── Fixtures & seeding helpers ───────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DISABLE_BACKGROUND_JOBS", "1")
    monkeypatch.setenv("YEAR_TERM", TERM)
    # Never reach the real OpenAI API from tests.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    db_path = str(tmp_path / "scheduler_api_test.db")
    monkeypatch.setattr(database, "DB", db_path)
    monkeypatch.setattr(settings, "db_path", db_path)
    monkeypatch.setattr(settings, "current_term", TERM)
    # Isolate envelope JSON so startup never reads the committed data/envelopes.
    monkeypatch.setattr(envelopes, "ENVELOPES_DIR", tmp_path / "envelopes")

    # Rate limiter state is global; start each test clean.
    scheduler_api._generate_rate.clear()

    with TestClient(main.app) as c:
        yield c


def _seed_prof(name, *, rating=None, diff=None, wta=None, num=None,
               rmp_id=None, rmp_name=None):
    async def run():
        pid = await database.upsert_professor(f"person-{name}", name)
        if num is not None:
            await database.update_professor_ratings(
                pid, rmp_id or f"rmp-{name}", rmp_name or name,
                diff, rating, wta, num, 1.0,
            )
        return pid
    return asyncio.run(run())


def _seed_section(code, number, meetings, *, is_online=False, seats_open=10,
                  instructor_id=None, title=None, credits=None, term=TERM):
    async def run():
        sid = await database.upsert_section(
            term, code, number, title=title, credits=credits,
            is_online=is_online, seats_open=seats_open, instructor_id=instructor_id,
        )
        await database.replace_section_meetings(
            sid, [(m[0], m[1], m[2], m[3], m[4]) for m in meetings]
        )
        return sid
    return asyncio.run(run())


def M(day, start, end, building="TNRB", room="101"):
    return (day, start, end, building, room)


def _generate(client, **overrides):
    body = {
        "courses": ["MKTG 201", "STRAT 305"],
        "preferences": ["easy", "no_gaps"],
        "days_off": ["Friday"],
        "earliest_start": "08:00",
        "latest_end": "22:00",
        "allow_online": False,
    }
    body.update(overrides)
    return client.post("/api/v1/scheduler/schedules/generate", json=body)


# ── Autocomplete ─────────────────────────────────────────────────────────────

def test_autocomplete_prefix_match(client):
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)], title="Marketing Management")
    _seed_section("MKTG 201", "002", [M("Monday", 600, 660)], title="Marketing Management")
    _seed_section("MKTG 202", "001", [M("Monday", 700, 760)], title="Consumer Behavior")
    _seed_section("STRAT 305", "001", [M("Monday", 800, 860)], title="Strategy")

    r = client.get("/api/v1/scheduler/courses/search", params={"q": "mktg", "limit": 10})
    assert r.status_code == 200
    codes = [item["code"] for item in r.json()]
    assert codes[0].startswith("MKTG")
    assert "MKTG 201" in codes and "MKTG 202" in codes
    # STRAT is not a prefix match; it only shows up via fuzzy (last).
    assert codes[-1] == "STRAT 305"

    mktg201 = next(i for i in r.json() if i["code"] == "MKTG 201")
    assert mktg201["section_count"] == 2
    assert mktg201["title"] == "Marketing Management"


def test_autocomplete_ignores_spaces_and_hyphens(client):
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)], title="Marketing")
    r = client.get("/api/v1/scheduler/courses/search", params={"q": "mktg-201"})
    assert r.status_code == 200
    assert [i["code"] for i in r.json()][0] == "MKTG 201"

    r = client.get("/api/v1/scheduler/courses/search", params={"q": "MKTG201"})
    assert [i["code"] for i in r.json()][0] == "MKTG 201"


def test_autocomplete_fuzzy(client):
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)], title="Marketing Management")
    _seed_section("STRAT 305", "001", [M("Monday", 600, 660)], title="Strategy")
    # A near-miss on the course code still finds the real course via fuzzy.
    r = client.get("/api/v1/scheduler/courses/search", params={"q": "mktg 209"})
    assert r.status_code == 200
    assert any(i["code"] == "MKTG 201" for i in r.json())


def test_autocomplete_short_query_empty(client):
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)])
    r = client.get("/api/v1/scheduler/courses/search", params={"q": "m"})
    assert r.status_code == 200
    assert r.json() == []


def test_instructors_endpoint_lists_professors_and_online(client):
    pid1 = _seed_prof("Melissa Larson")
    pid2 = _seed_prof("John Doe")
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)], instructor_id=pid1)
    _seed_section("MKTG 201", "002", [M("Monday", 600, 660)], instructor_id=pid1)
    _seed_section("MKTG 201", "003", [M("Monday", 720, 780)], instructor_id=pid2)
    _seed_section("MKTG 201", "900", [], is_online=True)

    r = client.get("/api/v1/scheduler/courses/instructors", params={"course": "MKTG 201"})
    assert r.status_code == 200
    body = r.json()
    assert body["course"] == "MKTG 201"
    names = {p["name"] for p in body["professors"]}
    assert names == {"melissa larson", "john doe"}
    assert body["has_online"] is True


def test_instructors_endpoint_no_professors_still_reports_online(client):
    _seed_section("MKTG 201", "900", [], is_online=True)
    r = client.get("/api/v1/scheduler/courses/instructors", params={"course": "MKTG 201"})
    assert r.status_code == 200
    body = r.json()
    assert body["professors"] == []
    assert body["has_online"] is True


# ── Days-off feasibility ─────────────────────────────────────────────────────

def test_days_off_single_day_course(client):
    # Marketing meets Mon/Wed only -> Mon & Wed are impossible to take off.
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540), M("Wednesday", 480, 540)])
    r = client.get("/api/v1/scheduler/courses/days-off",
                   params=[("courses", "MKTG 201")])
    assert r.status_code == 200
    assert r.json()["possible_days_off"] == ["Tuesday", "Thursday", "Friday"]


def test_days_off_unavailable_days_union_across_courses(client):
    # Marketing is Mon/Wed only; Strategy is Thursday only.
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540), M("Wednesday", 480, 540)])
    _seed_section("STRAT 305", "001", [M("Thursday", 600, 660)])
    r = client.get("/api/v1/scheduler/courses/days-off",
                   params=[("courses", "MKTG 201"), ("courses", "STRAT 305")])
    assert r.status_code == 200
    assert r.json()["possible_days_off"] == ["Tuesday", "Friday"]


def test_days_off_day_available_when_any_section_avoids_it(client):
    # Two sections: one Mon, one Wed. Every weekday is avoidable.
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)])
    _seed_section("MKTG 201", "002", [M("Wednesday", 480, 540)])
    r = client.get("/api/v1/scheduler/courses/days-off",
                   params=[("courses", "MKTG 201")])
    assert r.status_code == 200
    assert r.json()["possible_days_off"] == ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


def test_days_off_async_online_section_avoids_all_days(client):
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)])
    _seed_section("MKTG 201", "900", [], is_online=True)  # async, no meetings
    r = client.get("/api/v1/scheduler/courses/days-off",
                   params=[("courses", "MKTG 201")])
    assert r.status_code == 200
    assert r.json()["possible_days_off"] == ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


def test_days_off_empty_returns_all_weekdays(client):
    r = client.get("/api/v1/scheduler/courses/days-off")
    assert r.status_code == 200
    assert r.json()["possible_days_off"] == ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


# ── Generate: happy path ─────────────────────────────────────────────────────

def test_generate_happy_path(client):
    pid = _seed_prof("Melissa Larson", rating=4.5, diff=2.0, wta=90.0, num=50)
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)],
                  instructor_id=pid, title="Marketing Management")
    _seed_section("STRAT 305", "001", [M("Monday", 600, 660)], title="Strategy")

    r = _generate(client)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["term"] == TERM
    assert len(body["schedules"]) == 1

    sched = body["schedules"][0]
    assert len(sched["sections"]) == 2
    codes = {s["course_code"] for s in sched["sections"]}
    assert codes == {"MKTG 201", "STRAT 305"}
    mktg = next(s for s in sched["sections"] if s["course_code"] == "MKTG 201")
    assert mktg["instructor_name"] == "melissa larson"
    assert mktg["ease_score"] > 50
    assert mktg["ease_confidence"] > 0.8
    assert len(mktg["meetings"]) == 1
    # Metrics + score + explanation are present.
    assert "days_on_campus" in sched["metrics"]
    assert sched["total_score"] > 0
    assert sched["explanation"]  # fallback template (no LLM key)


# ── Professor filtering ─────────────────────────────────────────────────────

def test_generate_filters_by_professor(client):
    pid_larson = _seed_prof("Melissa Larson")
    pid_doe = _seed_prof("John Doe")
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)], instructor_id=pid_larson)
    _seed_section("MKTG 201", "002", [M("Monday", 600, 660)], instructor_id=pid_doe)
    _seed_section("STRAT 305", "001", [M("Monday", 660, 720)])

    r = _generate(client, instructors={"MKTG 201": [str(pid_larson)]})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    mktg = next(s for s in body["schedules"][0]["sections"] if s["course_code"] == "MKTG 201")
    assert mktg["instructor_name"] == "melissa larson"


def test_generate_online_filter(client):
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)])
    _seed_section("MKTG 201", "900", [], is_online=True)

    r = _generate(client, courses=["MKTG 201"], allow_online=True,
                  instructors={"MKTG 201": ["online"]})
    assert r.status_code == 200
    sched = r.json()["schedules"][0]
    assert sched["sections"] == []
    assert [s["course_code"] for s in sched["async_online"]] == ["MKTG 201"]


def test_generate_professor_filter_no_match_returns_no_schedule(client):
    pid = _seed_prof("Melissa Larson")
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)], instructor_id=pid)
    _seed_section("MKTG 201", "002", [M("Monday", 600, 660)])

    r = _generate(client, courses=["MKTG 201"], instructors={"MKTG 201": ["999999"]})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "no_schedule"
    assert body["bottleneck"]["no_sections"][0]["removed_by"]["instructor"] == 2


# ── Per-professor breakdown ──────────────────────────────────────────────────

def _breakdown(client, courses):
    body = _generate(client, courses=courses)
    assert body.status_code == 200
    sched = body.json()["schedules"][0]
    return sched["metrics"]["ease_breakdown"]


def test_breakdown_rated_professor_has_rmp_stats_and_url(client):
    rmp_id = base64.b64encode(b"Teacher-1742516").decode()
    pid = _seed_prof("mike taylor", rating=4.2, diff=3.5, wta=92.8571, num=50,
                     rmp_id=rmp_id, rmp_name="Michael Taylor")
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)],
                  instructor_id=pid, title="Marketing")

    rows = _breakdown(client, ["MKTG 201"])
    assert len(rows) == 1
    p = rows[0]
    assert p["display_name"] == "Michael Taylor"   # prefers RMP name over BYU
    assert p["courses"] == ["MKTG 201"]
    assert p["avg_rating"] == 4.2
    assert p["avg_difficulty"] == 3.5
    assert p["would_take_again_percent"] == 92.8571
    assert p["num_ratings"] == 50
    assert p["rmp_url"] == "https://www.ratemyprofessors.com/professor/1742516"
    assert p["data_status"] == "rated"
    assert p["ease_score"] > 50


def test_breakdown_none_professor_has_null_stats(client):
    pid = _seed_prof("heidi dixon")  # no ratings stored -> no confident match
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)], instructor_id=pid)

    rows = _breakdown(client, ["MKTG 201"])
    assert len(rows) == 1
    p = rows[0]
    assert p["data_status"] == "none"
    assert p["display_name"] == "Heidi Dixon"
    assert p["avg_rating"] is None
    assert p["avg_difficulty"] is None
    assert p["would_take_again_percent"] is None
    assert p["num_ratings"] is None
    assert p["rmp_url"] is None
    assert p["ease_score"] is None
    assert p["ease_confidence"] is None


def test_breakdown_limited_boundary_at_three_ratings(client):
    # Exactly 3 ratings is the cutoff between "limited" and "rated".
    pid3 = _seed_prof("three rated", rating=4.0, diff=3.0, wta=80.0, num=3)
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)], instructor_id=pid3)
    row3 = _breakdown(client, ["MKTG 201"])[0]
    assert row3["data_status"] == "rated"
    assert row3["num_ratings"] == 3
    assert row3["avg_rating"] == 4.0

    # Two ratings is "limited": numbers are real but flagged thin.
    pid2 = _seed_prof("two rated", rating=4.9, diff=1.7, wta=100.0, num=2)
    _seed_section("FIN 201", "001", [M("Monday", 600, 660)], instructor_id=pid2)
    row2 = _breakdown(client, ["FIN 201"])[0]
    assert row2["data_status"] == "limited"
    assert row2["num_ratings"] == 2
    assert row2["avg_rating"] == 4.9


def test_breakdown_zero_ratings_is_none(client):
    # A confident match with zero ratings carries no trustworthy numbers.
    pid = _seed_prof("jim allison", rating=0.0, diff=0.0, wta=-1.0, num=0,
                     rmp_name="Jim Allison")
    _seed_section("ANTHR 402", "001", [M("Monday", 480, 540)], instructor_id=pid)
    row = _breakdown(client, ["ANTHR 402"])[0]
    assert row["data_status"] == "none"
    assert row["avg_rating"] is None
    assert row["num_ratings"] is None
    assert row["rmp_url"] is None


def test_breakdown_invalid_rmp_id_gives_null_url(client):
    pid = _seed_prof("jane roe", rating=4.0, diff=2.5, wta=70.0, num=10,
                     rmp_id="not-valid-base64!!!", rmp_name="Jane Roe")
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)], instructor_id=pid)
    row = _breakdown(client, ["MKTG 201"])[0]
    assert row["data_status"] == "rated"
    assert row["rmp_url"] is None


# ── Schedule-wide ease average ────────────────────────────────────────────────

def test_average_ease_null_when_no_professor_has_ratings(client):
    # Every instructor lacks an RMP page -> ease cannot be calculated.
    pid = _seed_prof("heidi dixon")
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)], instructor_id=pid)
    _seed_section("STRAT 305", "001", [M("Monday", 600, 660)], instructor_id=pid)

    r = _generate(client)
    assert r.status_code == 200
    metrics = r.json()["schedules"][0]["metrics"]
    assert metrics["average_ease"] is None
    assert metrics["professors_without_ratings"] == 1


def test_average_ease_excludes_professors_without_rmp_pages(client):
    # One rated professor (ease 78) and one with no RMP page: the unrated
    # instructor must not drag the average toward the neutral 50.
    rated = _seed_prof("mike taylor", rating=4.0, diff=2.0, wta=90.0, num=50,
                       rmp_name="Michael Taylor")
    unrated = _seed_prof("heidi dixon")
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)], instructor_id=rated)
    _seed_section("STRAT 305", "001", [M("Monday", 600, 660)], instructor_id=unrated)

    r = _generate(client)
    assert r.status_code == 200
    metrics = r.json()["schedules"][0]["metrics"]
    assert metrics["average_ease"] == 78.0
    assert metrics["professors_without_ratings"] == 1
    statuses = sorted(p["data_status"] for p in metrics["ease_breakdown"])
    assert statuses == ["none", "rated"]


# ── Generate: error / fallback paths ─────────────────────────────────────────

def test_generate_unknown_course_returns_suggestions(client):
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)])
    r = _generate(client, courses=["MKTG 209"])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "suggestions" in detail
    assert "MKTG 201" in detail["suggestions"]


def test_generate_no_schedule_returns_diagnosis(client):
    # Both sections only meet at the same Monday slot -> always conflicting.
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)])
    _seed_section("STRAT 305", "001", [M("Monday", 480, 540)])
    r = _generate(client)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "no_schedule"
    assert body["schedules"] == []
    assert body["bottleneck"] is not None
    pairs = body["bottleneck"]["always_conflicting_pairs"]
    assert ["MKTG 201", "STRAT 305"] in pairs


def test_generate_invalid_preferences_422(client):
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)])
    r = _generate(client, preferences=["easy", "easy"])
    assert r.status_code == 422


def test_generate_invalid_time_window_422(client):
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)])
    r = _generate(client, earliest_start="10:00", latest_end="08:00")
    assert r.status_code == 422


def test_generate_too_many_courses_422(client):
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)])
    r = _generate(client, courses=[f"DEPT {100 + i}" for i in range(9)])
    assert r.status_code == 422


def test_generate_llm_failure_returns_schedules_with_fallback(client):
    # No OPENAI key is set in the fixture -> parse + explain both fall back,
    # but the solver still returns real schedules with template explanations.
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)])
    r = _generate(client, courses=["MKTG 201"],
                  extra_constraints_text="no classes Tuesday at 4")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["schedules"]
    # Parse fell back -> a warning is surfaced.
    assert body["warnings"]
    # Explain fell back -> deterministic template explanations.
    assert body["schedules"][0]["explanation"].startswith("Schedule 1")


def test_generate_duplicate_courses_deduplicated(client):
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)])
    r = _generate(client, courses=["MKTG201", "MKTG 201", "mktg-201"])
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert len(body["schedules"][0]["sections"]) == 1
    assert "Duplicate courses were removed." in body["warnings"]


def test_generate_more_exclude_roundtrip(client):
    # Four conflict-free schedules -> first call returns 3 with has_more.
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)])
    _seed_section("MKTG 201", "002", [M("Monday", 600, 660)])
    _seed_section("STRAT 305", "001", [M("Monday", 720, 780)])
    _seed_section("STRAT 305", "002", [M("Monday", 840, 900)])

    first = _generate(client).json()
    assert first["status"] == "ok"
    assert len(first["schedules"]) == 3
    assert first["has_more"] is True

    seen = [[s["id"] for s in sched["sections"]] for sched in first["schedules"]]
    second = _generate(client, exclude=seen).json()
    assert second["status"] == "ok"
    assert len(second["schedules"]) == 1
    assert second["has_more"] is False

    seen_all = seen + [[s["id"] for s in sched["sections"]] for sched in second["schedules"]]
    third = _generate(client, exclude=seen_all).json()
    assert third["status"] == "ok"
    assert third["schedules"] == []
    assert third["has_more"] is False


# ── Active term ──────────────────────────────────────────────────────────────

def test_active_term_picks_newest_term_in_db(client, monkeypatch):
    # Config term is older than what's actually in the DB.
    monkeypatch.setattr(settings, "current_term", "20265")
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)], term="20271")

    r = client.get("/api/v1/scheduler/status")
    assert r.status_code == 200
    assert r.json()["term"] == "20271"


def test_active_term_falls_back_when_empty(client, monkeypatch):
    monkeypatch.setattr(settings, "current_term", "20271")
    r = client.get("/api/v1/scheduler/status")
    assert r.json()["term"] == "20271"
    assert r.json()["status"] == "loading"


# ── Status ───────────────────────────────────────────────────────────────────

def test_status_reports_counts_and_llm_flag(client):
    pid = _seed_prof("Melissa Larson", rating=4.5, diff=2.0, wta=90.0, num=50)
    _seed_prof("Unrated Person")
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)], instructor_id=pid)
    _seed_section("MKTG 201", "002", [M("Monday", 600, 660)])

    r = client.get("/api/v1/scheduler/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["term"] == TERM
    assert body["section_count"] == 2
    assert body["meeting_count"] == 2
    assert body["professor_count"] == 2
    assert body["professors_with_ratings"] == 1
    assert body["last_scrape_time"] is not None
    assert body["llm_configured"] is False  # key removed by the fixture


def test_status_llm_configured_true(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)])
    r = client.get("/api/v1/scheduler/status")
    assert r.json()["llm_configured"] is True


# ── Rate limiting ────────────────────────────────────────────────────────────

def test_rate_limit_returns_429_after_cap(client):
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)])
    codes = []
    for _ in range(scheduler_api.GENERATE_RATE_LIMIT):
        codes.append(_generate(client, courses=["MKTG 201"]).status_code)
    # The next request is over the per-minute cap.
    codes.append(_generate(client, courses=["MKTG 201"]).status_code)

    assert codes[:scheduler_api.GENERATE_RATE_LIMIT] == [200] * scheduler_api.GENERATE_RATE_LIMIT
    assert codes[-1] == 429


# ── Empty DB behavior ────────────────────────────────────────────────────────

def test_empty_db_returns_503_data_loading(client):
    r = _generate(client)
    assert r.status_code == 503
    assert "loading" in r.json()["detail"].lower()

    s = client.get("/api/v1/scheduler/status")
    assert s.json()["status"] == "loading"


# ── Envelopes ───────────────────────────────────────────────────────────────

def _write_env_json(slug, bundles):
    d = envelopes.json_dir_for_term(TERM)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.json").write_text(json.dumps({
        "title": slug.replace("-", " ").title(),
        "display_title": slug.replace("-", " ").title(),
        "slug": slug,
        "term": TERM,
        "captured_at": "2026-10-13",
        "bundles": bundles,
    }), encoding="utf-8")


def _load_and_verify(slug, bundles):
    _write_env_json(slug, bundles)
    asyncio.run(envelopes.load_envelopes(TERM))
    asyncio.run(envelopes.verify_envelopes(TERM))


def _env_section(course_code, number, meetings, instructor="TBD"):
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


def test_envelopes_endpoint_lists_active_term(client):
    _seed_section("EXDM 415", "001", [M("Monday", 480, 555, "TNRB", "1258")])
    _seed_section("EXDM 416", "001", [M("Monday", 570, 645, "TNRB", "1258")])
    _load_and_verify("exdm-core", [
        _env_bundle("28004", [
            _env_section("EXDM 415", "001", [("Monday", 480, 555, "TNRB 1258")]),
            _env_section("EXDM 416", "001", [("Monday", 570, 645, "TNRB 1258")]),
        ]),
    ])

    r = client.get("/api/v1/scheduler/envelopes")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    env = data[0]
    assert env["slug"] == "exdm-core"
    assert env["title"] == "Exdm Core"
    assert env["ok_bundles"] == 1
    assert env["total_bundles"] == 1
    assert set(env["courses"]) == {"EXDM 415", "EXDM 416"}
    assert env["captured_at"] == "2026-10-13"


def test_generate_with_envelope_returns_tagged_sections(client):
    _seed_section("EXDM 415", "001", [M("Monday", 480, 555, "TNRB", "1258")])
    _seed_section("EXDM 416", "001", [M("Monday", 570, 645, "TNRB", "1258")])
    _load_and_verify("exdm-core", [
        _env_bundle("28004", [
            _env_section("EXDM 415", "001", [("Monday", 480, 555, "TNRB 1258")]),
            _env_section("EXDM 416", "001", [("Monday", 570, 645, "TNRB 1258")]),
        ]),
    ])

    r = _generate(client, courses=["EXDM 415", "EXDM 416"], envelopes=["exdm-core"])
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    sched = body["schedules"][0]
    assert len(sched["sections"]) == 2
    for s in sched["sections"]:
        assert s["via_envelope"] == "Exdm Core"
        assert s["env_id"] == "28004"
        assert s["captured_at"] == "2026-10-13"


def test_generate_with_only_envelope_without_courses(client):
    _seed_section("EXDM 415", "001", [M("Monday", 480, 555, "TNRB", "1258")])
    _seed_section("EXDM 416", "001", [M("Monday", 570, 645, "TNRB", "1258")])
    _load_and_verify("exdm-core", [
        _env_bundle("28004", [
            _env_section("EXDM 415", "001", [("Monday", 480, 555, "TNRB 1258")]),
            _env_section("EXDM 416", "001", [("Monday", 570, 645, "TNRB 1258")]),
        ]),
    ])

    r = _generate(client, courses=[], envelopes=["exdm-core"])
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    sched = body["schedules"][0]
    assert len(sched["sections"]) == 2
    for s in sched["sections"]:
        assert s["via_envelope"] == "Exdm Core"
        assert s["env_id"] == "28004"


def test_generate_without_courses_or_envelopes_422(client):
    r = _generate(client, courses=[], envelopes=[])
    assert r.status_code == 422

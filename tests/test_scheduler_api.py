"""Tests for the scheduler API router (Step 5).

No network: the app lifespan's calendar fetch is monkeypatched, background
jobs are disabled, and the LLM key is removed so parse/explain use their
deterministic fallbacks. A temp SQLite DB backs every test.
"""

import asyncio
import os
import sys
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.main as main  # noqa: E402
from backend import database, mcp_server, scheduler_api  # noqa: E402
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
    monkeypatch.setattr(settings, "db_path", db_path)  # availability.* uses this
    monkeypatch.setattr(settings, "current_term", TERM)

    # The lifespan scrapes BYU's calendar on first run; stub it out.
    async def fake_fetch_semester_starts(years_ahead=2):
        return []

    monkeypatch.setattr(main, "fetch_semester_starts", fake_fetch_semester_starts)

    # The MCP StreamableHTTPSessionManager can only be started once per process,
    # which breaks re-running the lifespan for every test. Stub it out.
    @asynccontextmanager
    async def _noop_session_manager():
        yield

    monkeypatch.setattr(mcp_server.mcp.session_manager, "run", _noop_session_manager)

    # Rate limiter state is global; start each test clean.
    scheduler_api._generate_rate.clear()

    with TestClient(main.app) as c:
        yield c


def _seed_prof(name, *, rating=None, diff=None, wta=None, num=None):
    async def run():
        pid = await database.upsert_professor(f"person-{name}", name)
        if num is not None:
            await database.update_professor_ratings(
                pid, f"rmp-{name}", name, diff, rating, wta, num, 1.0
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
        "preferences": ["easy", "days_off", "no_gaps"],
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
    r = _generate(client, preferences=["easy", "easy", "no_gaps"])
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


# ── Existing room-finder endpoints still respond ─────────────────────────────

def test_room_finder_endpoints_still_respond(client):
    assert client.get("/api/v1/buildings").status_code == 200
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/buildings").status_code == 200
    assert client.get(
        "/api/v1/rooms/available", params={"start": "15:00", "duration": 60}
    ).status_code == 200

"""Tests for the BYU section scraper's parsing + persistence (no network).

Parsing is exercised against a saved JSON fixture (the exact shape returned by
``ajax/getSections.php``) including a lecture+lab section and online sections.
"""

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import database  # noqa: E402
from backend import section_scraper as ss  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sections_sample.json")


def _load_sections():
    with open(FIXTURE) as f:
        data = json.load(f)
    return data["sections"]


def _by_number(num):
    return next(s for s in _load_sections() if s["section_number"] == num)


# ── Time parsing ─────────────────────────────────────────────────────────────

def test_hhmm_to_minutes():
    assert ss.hhmm_to_minutes("0800") == 480
    assert ss.hhmm_to_minutes("0915") == 555
    assert ss.hhmm_to_minutes("0000") == 0
    assert ss.hhmm_to_minutes(None) is None
    assert ss.hhmm_to_minutes("") is None


# ── Online detection ─────────────────────────────────────────────────────────

def test_is_online_detection():
    assert ss.is_online_section(_by_number("001")) is False      # Classroom
    assert ss.is_online_section(_by_number("900")) is True       # On-Demand
    assert ss.is_online_section(_by_number("950")) is True       # Live Remote


# ── Meeting parsing (per-day rows, minutes) ─────────────────────────────────

def test_single_block_mwf_becomes_three_day_rows():
    meetings = ss.meetings_from_times(_by_number("001")["times"])
    assert meetings == [
        ("Monday", 480, 530, "JSB", "140"),
        ("Wednesday", 480, 530, "JSB", "140"),
        ("Friday", 480, 530, "JSB", "140"),
    ]


def test_lecture_plus_lab_multiple_blocks():
    # Section 002: lecture TTh + lab W -> three rows across two blocks.
    meetings = ss.meetings_from_times(_by_number("002")["times"])
    assert len(meetings) == 3
    by_day = {m[0]: m for m in meetings}
    assert by_day["Tuesday"][1:4] == (600, 675, "TNRB")      # 10:00-11:15
    assert by_day["Thursday"][1:4] == (600, 675, "TNRB")
    assert by_day["Wednesday"][1:4] == (840, 950, "TNRB")    # 14:00-15:50
    assert by_day["Tuesday"][4] == "321"
    assert by_day["Wednesday"][4] == "205"


def test_async_online_section_has_no_meeting_rows():
    assert ss.meetings_from_times(_by_number("900")["times"]) == []


def test_live_remote_section_keeps_time_but_no_building():
    meetings = ss.meetings_from_times(_by_number("950")["times"])
    assert meetings == [("Wednesday", 480, 555, None, None)]


# ── Instructor / seats / credits ─────────────────────────────────────────────

def test_primary_instructor_name():
    assert ss.primary_instructor_name(_by_number("001")) == "Melissa Larson"
    assert ss.primary_instructor_name({"instructors": []}) is None


def test_section_seats_open():
    assert ss.section_seats_open(_by_number("001")) == 35
    assert ss.section_seats_open({"availability": None}) is None


def test_section_credits_section_and_course_fallback():
    course = {"credit_hours": "3"}
    assert ss.section_credits(_by_number("001"), course) == 3.0
    assert ss.section_credits({"credit_hours": None}, course) == 3.0
    assert ss.section_credits({"credit_hours": None}, None) is None


def test_course_code_for():
    assert ss.course_code_for(
        {"dept_name": "MKTG", "catalog_number": "201", "catalog_suffix": None}
    ) == "MKTG 201"
    assert ss.course_code_for(
        {"dept_name": "CS", "catalog_number": "110", "catalog_suffix": "R"}
    ) == "CS 110R"


# ── Persistence round-trip (temp DB, no network) ─────────────────────────────

@pytest.fixture()
def db(tmp_path):
    orig = database.DB
    database.DB = str(tmp_path / "sb_test.db")
    asyncio.run(database.init_schedule_db())
    yield
    database.DB = orig


def test_persist_course_writes_sections_and_meetings(db):
    course = {"dept_name": "ACC", "catalog_number": "200", "catalog_suffix": None,
              "title": "Principles of Accounting", "credit_hours": "3"}
    sections = _load_sections()

    async def run():
        return await ss._persist_course("20271", course, sections)

    written = asyncio.run(run())
    assert written == 4

    async def check():
        rows = await database.get_sections_for_term("20271")
        return rows

    rows = asyncio.run(check())
    assert len(rows) == 4

    by_num = {r["section_number"]: r for r in rows}
    assert by_num["001"]["course_code"] == "ACC 200"
    assert by_num["001"]["is_online"] == 0
    assert by_num["001"]["seats_open"] == 35
    assert by_num["001"]["instructor_id"] is not None
    assert by_num["900"]["is_online"] == 1
    assert by_num["900"]["meetings"] == []

    # Lecture + lab: 3 meeting rows.
    assert len(by_num["002"]["meetings"]) == 3


def test_persist_is_idempotent(db):
    course = {"dept_name": "ACC", "catalog_number": "200", "catalog_suffix": None,
              "title": "Principles of Accounting", "credit_hours": "3"}
    sections = _load_sections()

    async def run():
        await ss._persist_course("20271", course, sections)
        await ss._persist_course("20271", course, sections)  # re-scrape
        return await database.get_sections_for_term("20271")

    rows = asyncio.run(run())
    assert len(rows) == 4  # upserted, not duplicated

    async def count_meetings():
        async with __import__("aiosqlite").connect(database.DB) as c:
            async with c.execute("SELECT COUNT(*) FROM meetings") as cur:
                return (await cur.fetchone())[0]

    # 001 (MWF) = 3, 002 (lecture+lab) = 3, 900 (async) = 0, 950 (live) = 1
    assert asyncio.run(count_meetings()) == 7  # re-scrape did not duplicate rows

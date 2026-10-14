"""Tests for envelope parsing, loading, and verification.

The parser is pure (no DB/network); loader + verification run against a temp
SQLite DB seeded with sections.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import database, envelopes  # noqa: E402
from backend.envelopes import (  # noqa: E402
    display_title,
    parse_cart_text,
    parse_days,
    parse_time_range,
    slugify,
)

TERM = "20271"
RAW_FILE = Path(__file__).resolve().parents[1] / "data" / "envelopes" / "raw" / "20271.txt"


def _parse(text, term=TERM, captured_at="2026-10-13"):
    return parse_cart_text(text, term, captured_at)


# ── Title / slug / time helpers ──────────────────────────────────────────────

def test_slugify_and_display_title():
    assert slugify("EXDM junior core") == "exdm-junior-core"
    assert slugify("2nd yr MACc Pro") == "2nd-yr-macc-pro"
    assert slugify("Math 213 & 215 Envelope") == "math-213-215-envelope"
    assert display_title("Accountin jr core") == "Accounting Junior Core"
    assert display_title("EXDM junior core") == "EXDM Junior Core"
    assert display_title("2nd yr MACc Pro") == "2nd Year MAcc Program"


def test_parse_days():
    assert parse_days("M") == ["Monday"]
    assert parse_days("T") == ["Tuesday"]
    assert parse_days("Th") == ["Thursday"]
    assert parse_days("MW") == ["Monday", "Wednesday"]
    assert parse_days("TTh") == ["Tuesday", "Thursday"]
    assert parse_days("MWF") == ["Monday", "Wednesday", "Friday"]
    assert parse_days("8 AM") is None
    assert parse_days("") is None


def test_parse_time_range():
    assert parse_time_range("9:30a - 10:45a") == (570, 645)
    assert parse_time_range("7:45a - 11:30a") == (465, 690)
    assert parse_time_range("12p - 12:50p") == (720, 770)
    assert parse_time_range("12:15p - 4p") == (735, 960)
    assert parse_time_range("9a - 9:50a") == (540, 590)
    assert parse_time_range("garbage") is None


# ── Parser: real pasted text ─────────────────────────────────────────────────

def test_parser_counts():
    text = RAW_FILE.read_text(encoding="utf-8")
    envs = _parse(text)
    assert len(envs) == 10
    assert sum(len(e.bundles) for e in envs) == 38

    by_slug = {e.slug: e for e in envs}
    expected = {
        "exdm-junior-core": 4,
        "hrm-jr-core": 1,
        "strat-jr-core": 4,
        "is-jr-core": 4,
        "accountin-jr-core": 6,
        "2nd-yr-macc-pro": 4,
        "mba-first-year-core": 2,
        "sfl-221-and-222-envelope": 4,
        "learning-communities-envelope": 8,
        "math-213-215-envelope": 1,
    }
    assert {slug: len(e.bundles) for slug, e in by_slug.items()} == expected


def test_strat_bundle_28513_contents():
    text = RAW_FILE.read_text(encoding="utf-8")
    strat = next(e for e in _parse(text) if e.slug == "strat-jr-core")
    b = next(b for b in strat.bundles if b.env_id == "28513")
    got = [(s.course_code, s.section_number) for s in b.sections]
    assert got == [("STRAT 411", "003"), ("STRAT 412", "002"), ("STRAT 432", "002")]


def test_strat_412_001_in_two_bundles():
    text = RAW_FILE.read_text(encoding="utf-8")
    strat = next(e for e in _parse(text) if e.slug == "strat-jr-core")
    hits = [b.env_id for b in strat.bundles
            if any(s.course_code == "STRAT 412" and s.section_number == "001"
                   for s in b.sections)]
    assert hits == ["28512", "28514"]


def test_mba_term1_term2():
    text = RAW_FILE.read_text(encoding="utf-8")
    mba = next(e for e in _parse(text) if e.slug == "mba-first-year-core")
    b = mba.bundles[0]
    parts = {s.course_code: s.part_of_term for s in b.sections}
    assert parts["MBA 570"] == "1"
    assert parts["MBA 584"] == "2"
    assert parts["MBA 560"] is None
    # MBA 693 catalog number keeps its R suffix resolution target in the loader
    assert ("MBA 693", "015") in [(s.course_code, s.section_number) for s in b.sections]
    # ...and its note flag is captured
    assert next(s for s in b.sections if s.course_code == "MBA 693").note_flag is True


def test_tbd_instructor():
    text = RAW_FILE.read_text(encoding="utf-8")
    lc = next(e for e in _parse(text) if e.slug == "learning-communities-envelope")
    tbd = [s for b in lc.bundles for s in b.sections
           if s.instructor_snapshot == "TBD"]
    assert {s.course_code for s in tbd} == {"WRTG 150"}
    assert {s.section_number for s in tbd} == {"075", "054", "076"}


def test_two_meeting_sections():
    text = RAW_FILE.read_text(encoding="utf-8")
    sfl = next(e for e in _parse(text) if e.slug == "sfl-221-and-222-envelope")
    sec = next(s for b in sfl.bundles for s in b.sections
               if s.course_code == "SFL 222" and s.section_number == "003")
    assert [(m["day"], m["start_min"], m["end_min"], m["room"]) for m in sec.meetings] == [
        ("Tuesday", 465, 650, "JFSB 1100"),
        ("Thursday", 465, 690, "JFSB 1100"),
    ]

    lc = next(e for e in _parse(text) if e.slug == "learning-communities-envelope")
    phys = next(s for b in lc.bundles for s in b.sections
                if s.course_code == "PHYS 100" and s.section_number == "014")
    assert [(m["day"], m["start_min"], m["end_min"], m["room"]) for m in phys.meetings] == [
        ("Monday", 600, 650, "CB 377"),
        ("Wednesday", 600, 650, "CB 377"),
        ("Friday", 600, 650, "CB 377"),
        ("Thursday", 600, 650, "ESC N127"),
    ]


def test_blended_mode_and_seats_snapshot():
    text = RAW_FILE.read_text(encoding="utf-8")
    hrm = next(e for e in _parse(text) if e.slug == "hrm-jr-core")
    b = hrm.bundles[0]
    msb = next(s for s in b.sections if s.course_code == "MSB 325")
    assert msb.mode == "Blended"
    assert b.capacity_snapshot == 70
    assert b.seats_snapshot == 70


# ── Parser: synthetic edge cases ─────────────────────────────────────────────

def test_multi_meeting_parsing():
    text = """Multi meet envelope
Prof A
TEST 201
Section 001
5/5 seats left
3 CREDITS
MWF
10a - 10:50a
CB 377
Th
10a - 10:50a
ESC N127
Env 20001
Select
"""
    envs = _parse(text)
    assert len(envs) == 1
    b = envs[0].bundles[0]
    assert b.env_id == "20001"
    assert [(m["day"], m["room"]) for m in b.sections[0].meetings] == [
        ("Monday", "CB 377"), ("Wednesday", "CB 377"),
        ("Friday", "CB 377"), ("Thursday", "ESC N127"),
    ]


def test_no_env_line_warns_and_keeps_null_env_id():
    text = """Test envelope
Ann Smith
TEST 101
Section 001
10/10 seats left
3 CREDITS
MW
9a - 10a
ROOM 1
"""
    envs = _parse(text)
    assert len(envs) == 1
    b = envs[0].bundles[0]
    assert b.env_id is None
    assert envs[0].warnings, "a missing Env line must produce a warning"


# ── Loader: idempotent + section resolution ──────────────────────────────────

@pytest.fixture()
def db(tmp_path, monkeypatch):
    orig_db = database.DB
    orig_dir = envelopes.ENVELOPES_DIR
    database.DB = str(tmp_path / "env_test.db")
    envelopes.ENVELOPES_DIR = tmp_path / "envelopes"
    asyncio.run(database.init_schedule_db())
    yield
    database.DB = orig_db
    envelopes.ENVELOPES_DIR = orig_dir


def _seed_section(code, number, meetings, instructor="Prof A"):
    async def run():
        pid = await database.upsert_professor(None, instructor)
        sid = await database.upsert_section(TERM, code, number, instructor_id=pid)
        await database.replace_section_meetings(
            sid, [(m[0], m[1], m[2], m[3], m[4]) for m in meetings]
        )
        return sid
    return asyncio.run(run())


def _write_envelope_json(slug, bundles):
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


def test_loader_is_idempotent(db):
    _seed_section("TEST 101", "001", [("Monday", 540, 590, "ROOM", "1")])
    _write_envelope_json("test-envelope", [{
        "env_id": "20001",
        "capacity_snapshot": 10,
        "seats_snapshot": 10,
        "sections": [{
            "course_code": "TEST 101", "section_number": "001",
            "instructor_snapshot": "Ann Smith", "mode": None, "credits": 3.0,
            "part_of_term": None, "note_flag": False,
            "meetings": [{"day": "Monday", "start_min": 540, "end_min": 590, "room": "ROOM 1"}],
        }],
    }])

    asyncio.run(envelopes.load_envelopes(TERM))
    asyncio.run(envelopes.load_envelopes(TERM))  # second load must not duplicate

    db_envs = asyncio.run(database.get_envelopes(TERM))
    assert len(db_envs) == 1
    assert len(db_envs[0]["bundles"]) == 1
    assert len(db_envs[0]["bundles"][0]["sections"]) == 1
    assert db_envs[0]["bundles"][0]["sections"][0]["section_id"] is not None


# ── Verification: ok / mismatch / missing ────────────────────────────────────

def _envelope_with_sections(sections):
    return [{
        "env_id": "30001", "capacity_snapshot": 10, "seats_snapshot": 10,
        "sections": sections,
    }]


def _sec(code, number, meetings, instructor="Prof A"):
    return {
        "course_code": code, "section_number": number,
        "instructor_snapshot": instructor, "mode": None, "credits": 3.0,
        "part_of_term": None, "note_flag": False,
        "meetings": [{"day": d, "start_min": s, "end_min": e, "room": r}
                     for (d, s, e, r) in meetings],
    }


def test_verify_ok_mismatch_missing(db):
    _seed_section("OK 101", "001", [("Monday", 540, 590, "ROOM", "1")])
    _seed_section("BAD 101", "001", [("Monday", 600, 660, "ROOM", "2")])  # wrong time
    # "MISS 101" intentionally not seeded -> missing

    _write_envelope_json("verify-env", [
        _envelope_with_sections([_sec("OK 101", "001", [("Monday", 540, 590, "ROOM 1")])])[0],
        _envelope_with_sections([_sec("BAD 101", "001", [("Monday", 540, 590, "ROOM 2")])])[0],
        _envelope_with_sections([_sec("MISS 101", "001", [("Monday", 540, 590, "ROOM 3")])])[0],
    ])

    asyncio.run(envelopes.load_envelopes(TERM))
    report = asyncio.run(envelopes.verify_envelopes(TERM))

    assert report.ok_bundles == 1
    assert report.mismatch_bundles == 1
    assert report.missing_bundles == 1
    assert report.has_problems

    db_envs = asyncio.run(database.get_envelopes(TERM))
    statuses = sorted(b["ok"] for b in db_envs[0]["bundles"])
    assert statuses == [0, 0, 1]


def test_verify_ok_bundle_stamped(db):
    _seed_section("GOOD 101", "001", [("Monday", 540, 590, "ROOM", "1")])
    _write_envelope_json("verify-good", _envelope_with_sections([
        _sec("GOOD 101", "001", [("Monday", 540, 590, "ROOM 1")]),
    ]))
    asyncio.run(envelopes.load_envelopes(TERM))
    report = asyncio.run(envelopes.verify_envelopes(TERM))
    assert report.ok_bundles == 1
    assert not report.has_problems
    db_envs = asyncio.run(database.get_envelopes(TERM))
    assert db_envs[0]["bundles"][0]["ok"] == 1


def test_instructor_matching_normalizes_names():
    from backend.envelopes import instructors_match
    assert instructors_match("Nicole Smith", "nicole smith")
    assert instructors_match("Nicole Smith", "N. Smith")  # initial + last name
    assert instructors_match("John Q. Public", "john public")
    assert instructors_match("TBD", None)  # no snapshot => no mismatch
    assert instructors_match(None, None)
    assert not instructors_match("Alice Jones", "bob williams")


def test_verify_instructor_only_difference_stays_ok_with_warning(db):
    _seed_section("OK 101", "001", [("Monday", 540, 590, "ROOM", "1")],
                  instructor="Bob Williams")
    _write_envelope_json("verify-instructor", _envelope_with_sections([
        _sec("OK 101", "001", [("Monday", 540, 590, "ROOM 1")],
             instructor="Alice Jones"),
    ]))

    asyncio.run(envelopes.load_envelopes(TERM))
    report = asyncio.run(envelopes.verify_envelopes(TERM))

    assert report.ok_bundles == 1
    assert report.mismatch_bundles == 0
    assert report.missing_bundles == 0
    assert not report.has_problems
    # The soft warning names both the snapshot and the DB's current name.
    assert report.warnings
    assert any("Alice Jones" in w and "bob williams" in w for w in report.warnings)

    db_envs = asyncio.run(database.get_envelopes(TERM))
    assert db_envs[0]["bundles"][0]["ok"] == 1


def test_serialize_round_trips():
    from backend.envelopes import envelope_to_text
    text = RAW_FILE.read_text(encoding="utf-8")
    envs = _parse(text)
    # Serialize every envelope back to text and re-parse: contents must match.
    recombined = "\n\n".join(envelope_to_text(e) for e in envs)
    reparsed = _parse(recombined)
    assert len(reparsed) == len(envs)
    for a, b in zip(envs, reparsed):
        assert a.slug == b.slug
        assert [(x.course_code, x.section_number) for bl in a.bundles for x in bl.sections] == \
               [(x.course_code, x.section_number) for bl in b.bundles for x in bl.sections]
        assert [x.env_id for bl in a.bundles for x in [bl]] == \
               [x.env_id for bl in b.bundles for x in [bl]]
        # Meetings survive the round trip.
        a_meetings = [(m["day"], m["start_min"], m["end_min"], m["room"])
                      for bl in a.bundles for s in bl.sections for m in s.meetings]
        b_meetings = [(m["day"], m["start_min"], m["end_min"], m["room"])
                      for bl in b.bundles for s in bl.sections for m in s.meetings]
        assert a_meetings == b_meetings


def test_verify_time_difference_is_withheld(db):
    _seed_section("BAD 101", "001", [("Monday", 600, 660, "ROOM", "1")])
    _write_envelope_json("verify-time", _envelope_with_sections([
        _sec("BAD 101", "001", [("Monday", 540, 590, "ROOM 1")]),
    ]))

    asyncio.run(envelopes.load_envelopes(TERM))
    report = asyncio.run(envelopes.verify_envelopes(TERM))

    assert report.ok_bundles == 0
    assert report.mismatch_bundles == 1
    assert report.has_problems

    db_envs = asyncio.run(database.get_envelopes(TERM))
    assert db_envs[0]["bundles"][0]["ok"] == 0

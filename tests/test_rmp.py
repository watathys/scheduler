"""Tests for RMP name matching, ease scoring, and refresh logic.

No live network calls: refresh uses an injected fake fetcher against a temp DB.
"""

import asyncio
import base64
import os
import sys

import aiosqlite
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import database  # noqa: E402
from backend import rmp  # noqa: E402
from backend.rmp import (  # noqa: E402
    MatchResult,
    ease_score,
    extract_teaching_area,
    match_professor,
    normalize_course_for_match,
    normalize_person_name,
    rmp_url_from_id,
)


def cand(first, last, dept=None, rating=4.5, diff=2.5, wta=80.0, num=30,
         rid=None, courses=None):
    return {
        "id": rid or f"T-{first}{last}",
        "firstName": first,
        "lastName": last,
        "department": dept,
        "avgRating": rating,
        "avgDifficulty": diff,
        "wouldTakeAgainPercent": wta,
        "numRatings": num,
        "courseCodes": [{"courseName": n, "courseCount": c} for n, c in (courses or [])],
    }


def prof(rating=None, diff=None, wta=None, num=None):
    """A DB professor row (snake_case keys, as ease_score expects)."""
    return {
        "avg_rating": rating,
        "avg_difficulty": diff,
        "would_take_again": wta,
        "num_ratings": num,
    }


# ── Name normalization ───────────────────────────────────────────────────────

def test_normalize_person_name_basic():
    assert normalize_person_name("Melissa Paul Larson") == {"first": "melissa", "last": "larson"}


def test_normalize_person_name_comma_format():
    assert normalize_person_name("Larson, Melissa Paul") == {"first": "melissa", "last": "larson"}


def test_normalize_person_name_last_initial():
    assert normalize_person_name("Larson, Melissa P") == {"first": "melissa", "last": "larson"}


def test_normalize_person_name_strips_suffixes_and_accents():
    assert normalize_person_name("Dr. John A. Doe III") == {"first": "john", "last": "doe"}
    assert normalize_person_name("José García-López") == {"first": "jose", "last": "lopez"}


def test_normalize_person_name_single_token_is_last():
    assert normalize_person_name("Larson") == {"first": None, "last": "larson"}


# ── Name matching ────────────────────────────────────────────────────────────

def test_match_nickname():
    res = match_professor("Bob Smith", [cand("Robert", "Smith")])
    assert res.candidate is not None
    assert res.confidence >= rmp.CONFIDENCE_THRESHOLD


def test_match_hyphenated_name():
    # "Mary Smith-Jones" normalizes last -> "jones"; candidate "Smith Jones" too.
    res = match_professor("Mary Smith-Jones", [cand("Mary", "Smith Jones")])
    assert res.candidate is not None
    assert res.confidence >= rmp.CONFIDENCE_THRESHOLD


def test_match_two_same_last_name_picks_right_first():
    res = match_professor(
        "Melissa Larson",
        [cand("Jenn", "Larson"), cand("Melissa", "Larson"), cand("Michael", "Larson")],
    )
    assert res.candidate["firstName"] == "Melissa"
    assert res.confidence >= rmp.CONFIDENCE_THRESHOLD


def test_match_last_comma_first_format():
    res = match_professor("Larson, Melissa P", [cand("Melissa", "Larson")])
    assert res.candidate is not None
    assert res.confidence >= rmp.CONFIDENCE_THRESHOLD


def test_missing_first_name_is_ambiguous_with_multiple_candidates():
    res = match_professor("Larson", [cand("Jenn", "Larson"), cand("Michael", "Larson")])
    assert res.ambiguous or res.confidence < rmp.CONFIDENCE_THRESHOLD


def test_no_candidates_returns_none():
    res = match_professor("Melissa Larson", [])
    assert res.candidate is None
    assert res.confidence == 0.0


# ── Ease score ───────────────────────────────────────────────────────────────

def test_ease_few_ratings_neutral():
    score, conf = ease_score(prof(num=2))
    assert score == 50.0
    assert conf < 0.3


def test_ease_high_difficulty_is_low_score():
    score, conf = ease_score(prof(rating=1.0, diff=5.0, wta=0.0, num=10))
    assert score < 30.0
    assert conf > 0.0


def test_ease_easy_professor_high_score():
    score, conf = ease_score(prof(rating=5.0, diff=1.0, wta=100.0, num=50))
    assert score > 85.0
    assert conf > 0.8


def test_ease_missing_data_neutral_not_easy():
    score, conf = ease_score(prof(rating=None, diff=None, wta=None, num=10))
    assert score == 50.0
    assert conf < 0.5


# ── rmp_url extraction ───────────────────────────────────────────────────────

def _b64id(payload: str) -> str:
    return base64.b64encode(payload.encode("utf-8")).decode("ascii")


def test_rmp_url_valid_id():
    assert rmp_url_from_id(_b64id("Teacher-1742516")) == (
        "https://www.ratemyprofessors.com/professor/1742516"
    )


def test_rmp_url_padded_id():
    # Real stored ids include base64 padding.
    assert rmp_url_from_id(_b64id("Teacher-511061")) == (
        "https://www.ratemyprofessors.com/professor/511061"
    )


def test_rmp_url_missing_none():
    assert rmp_url_from_id(None) is None
    assert rmp_url_from_id("") is None


def test_rmp_url_malformed_base64():
    assert rmp_url_from_id("not-base64!!!") is None
    assert rmp_url_from_id("###") is None


def test_rmp_url_non_digit_payload():
    # Decodes to something other than "Teacher-<digits>".
    assert rmp_url_from_id(_b64id("Teacher-abc")) is None
    assert rmp_url_from_id(_b64id("School-135")) is None
    assert rmp_url_from_id(_b64id("Teacher-12-34")) is None
    assert rmp_url_from_id(_b64id("garbage")) is None


# ── Refresh orchestration (mocked, no network) ───────────────────────────────

@pytest.fixture()
def db(tmp_path):
    orig = database.DB
    database.DB = str(tmp_path / "rmp_test.db")
    asyncio.run(database.init_schedule_db())
    yield
    database.DB = orig


def _seed_professors(names):
    """Seed professors keyed by person_id (stable identity). Returns {name: id}."""
    async def run():
        ids = {}
        for i, n in enumerate(names):
            ids[n] = await database.upsert_professor(f"person-{i}", n)
        return ids

    return asyncio.run(run())


def _row(byu_name):
    """Fetch a professor row by normalized byu_name (test helper)."""
    async def run():
        async with __import__("aiosqlite").connect(database.DB) as c:
            c.row_factory = __import__("aiosqlite").Row
            async with c.execute(
                "SELECT * FROM professors WHERE byu_name=?", (byu_name,)
            ) as cur:
                return dict((await cur.fetchone()))

    return asyncio.run(run())


def test_refresh_skips_recently_fetched(db):
    ids = _seed_professors(["John Doe", "Jane Roe"])
    # Mark "Jane Roe" as fetched just now (with ratings).
    asyncio.run(
        database.update_professor_ratings(
            ids["Jane Roe"], "T-x", "Jane Roe", 2.5, 4.5, 80.0, 30, 0.95
        )
    )

    called = []

    async def fake_fetcher(client, name):
        called.append(name)
        return [cand("John", "Doe")]

    summary = asyncio.run(
        rmp.refresh_professors(limit=10, force=False, fetcher=fake_fetcher)
    )
    assert summary["attempted"] == 1          # only John Doe is stale
    assert "jane roe" not in called
    assert "john doe" in called


def test_refresh_force_queries_everyone(db):
    ids = _seed_professors(["John Doe", "Jane Roe"])
    asyncio.run(
        database.update_professor_ratings(
            ids["Jane Roe"], "T-x", "Jane Roe", 2.5, 4.5, 80.0, 30, 0.95
        )
    )

    async def fake_fetcher(client, name):
        return [cand("John", "Doe")] if name == "john doe" else [cand("Jane", "Roe")]

    summary = asyncio.run(
        rmp.refresh_professors(limit=10, force=True, fetcher=fake_fetcher)
    )
    assert summary["attempted"] == 2


def test_refresh_not_found_marks_fetched_and_null(db):
    _seed_professors(["Nobody Here"])

    async def fake_fetcher(client, name):
        return []

    summary = asyncio.run(rmp.refresh_professors(fetcher=fake_fetcher))
    assert summary["not_found"] == 1

    async def check():
        async with __import__("aiosqlite").connect(database.DB) as c:
            c.row_factory = __import__("aiosqlite").Row
            async with c.execute("SELECT * FROM professors WHERE byu_name='nobody here'") as cur:
                return dict((await cur.fetchone()))

    row = asyncio.run(check())
    assert row["last_fetched"] is not None
    assert row["avg_rating"] is None


def test_refresh_low_confidence_stores_null(db):
    _seed_professors(["Larson"])  # missing first name -> ambiguous among candidates

    async def fake_fetcher(client, name):
        return [cand("Jenn", "Larson"), cand("Michael", "Larson")]

    summary = asyncio.run(rmp.refresh_professors(fetcher=fake_fetcher))
    assert summary["low_confidence"] == 1

    async def check():
        async with __import__("aiosqlite").connect(database.DB) as c:
            c.row_factory = __import__("aiosqlite").Row
            async with c.execute("SELECT * FROM professors WHERE byu_name='larson'") as cur:
                return dict((await cur.fetchone()))

    row = asyncio.run(check())
    assert row["avg_rating"] is None
    assert row["last_fetched"] is not None


def test_refresh_confident_match_stores_ratings(db):
    _seed_professors(["Melissa Larson"])

    async def fake_fetcher(client, name):
        return [cand("Melissa", "Larson", dept="Accounting", rating=4.4, diff=3.4, wta=88.8, num=350)]

    summary = asyncio.run(rmp.refresh_professors(fetcher=fake_fetcher))
    assert summary["matched"] == 1

    async def check():
        async with __import__("aiosqlite").connect(database.DB) as c:
            c.row_factory = __import__("aiosqlite").Row
            async with c.execute("SELECT * FROM professors WHERE byu_name='melissa larson'") as cur:
                return dict((await cur.fetchone()))

    row = asyncio.run(check())
    assert row["rmp_name"] == "Melissa Larson"
    assert row["avg_rating"] == 4.4
    assert row["num_ratings"] == 350
    assert row["match_confidence"] >= rmp.CONFIDENCE_THRESHOLD


def test_refresh_error_is_caught_and_counted(db):
    _seed_professors(["Boom Bad"])

    async def fake_fetcher(client, name):
        raise RuntimeError("endpoint changed")

    summary = asyncio.run(rmp.refresh_professors(fetcher=fake_fetcher))
    assert summary["errors"] == 1
    assert summary["matched"] == 0


# ── Course-code normalization (PART 2) ───────────────────────────────────────

def test_normalize_course_for_match():
    assert normalize_course_for_match("MKTG201") == "MKTG 201"
    assert normalize_course_for_match("mktg 201") == "MKTG 201"
    assert normalize_course_for_match("MKTG 201R") == "MKTG 201"  # suffix dropped
    assert normalize_course_for_match("SPA321") == "SPAN 321"      # dept alias
    assert normalize_course_for_match("SPAN321") == "SPAN 321"
    assert normalize_course_for_match("RELC261") == "REL 261"
    assert normalize_course_for_match("342") is None               # bare digits
    assert normalize_course_for_match(None) is None


def test_extract_teaching_area():
    assert extract_teaching_area("MKTG 201") == "MKTG"
    assert extract_teaching_area("REL 250") == "REL"
    assert extract_teaching_area("AM ST 100") == "AM ST"
    assert extract_teaching_area("garbage") is None


# ── Course-overlap disambiguation (PART 2) ───────────────────────────────────

def test_course_overlap_picks_marketing_vs_religion():
    cands = [
        cand("Aaron", "Jackson", dept="Marketing", courses=[("MKTG201", 14)]),
        cand("Aaron", "Jackson", dept="Religion", courses=[("RELC250", 30)]),
    ]
    res = match_professor("Aaron Jackson", cands, ["MKTG 201"], ["MKTG"])
    assert res.matched
    assert res.candidate["department"] == "Marketing"
    assert "MKTG 201" in res.reason

    res2 = match_professor("Aaron Jackson", cands, ["REL 250"], ["REL"])
    assert res2.matched
    assert res2.candidate["department"] == "Religion"
    assert "REL 250" in res2.reason


def test_two_byu_same_name_different_areas_separate_rows(db):
    async def run():
        await database.init_schedule_db()
        id1 = await database.upsert_professor("p-111", "Aaron Jackson")
        id2 = await database.upsert_professor("p-222", "Aaron Jackson")
        assert id1 != id2
        await database.upsert_section("20271", "MKTG 201", "001", instructor_id=id1)
        await database.upsert_section("20271", "REL 250", "001", instructor_id=id2)

        assert await database.get_professor_courses(id1) == ["MKTG 201"]
        assert await database.get_professor_courses(id2) == ["REL 250"]

        async with aiosqlite.connect(database.DB) as c:
            async with c.execute(
                "SELECT COUNT(*) FROM professors WHERE byu_name='aaron jackson'"
            ) as cur:
                return (await cur.fetchone())[0]

    assert asyncio.run(run()) == 2


def test_two_byu_same_name_get_separate_ratings(db):
    """Two same-named instructors in different areas each resolve to their own RMP profile."""
    async def seed():
        await database.init_schedule_db()
        id1 = await database.upsert_professor("p-111", "Aaron Jackson")
        id2 = await database.upsert_professor("p-222", "Aaron Jackson")
        await database.upsert_section("20271", "MKTG 201", "001", instructor_id=id1)
        await database.upsert_section("20271", "REL 250", "001", instructor_id=id2)
        return id1, id2

    id1, id2 = asyncio.run(seed())

    async def fake_fetcher(client, name):
        # Same RMP name -> both candidates returned for either instructor.
        return [
            cand("Aaron", "Jackson", dept="Marketing", courses=[("MKTG201", 14)],
                 rid="T-mktg"),
            cand("Aaron", "Jackson", dept="Religion", courses=[("RELC250", 30)],
                 rid="T-rel"),
        ]

    summary = asyncio.run(rmp.refresh_professors(fetcher=fake_fetcher))
    assert summary["matched"] == 2

    row1 = _row("aaron jackson")  # ambiguous by name alone; get both via person_id
    async def by_id(pid):
        async with aiosqlite.connect(database.DB) as c:
            c.row_factory = aiosqlite.Row
            async with c.execute("SELECT * FROM professors WHERE id=?", (pid,)) as cur:
                return dict((await cur.fetchone()))

    r1 = asyncio.run(by_id(id1))
    r2 = asyncio.run(by_id(id2))
    assert r1["rmp_name"] == "Aaron Jackson"
    assert r1["avg_rating"] is not None
    assert r2["avg_rating"] is not None
    # Each should have matched a different profile via course overlap.
    assert {r1["match_evidence"], r2["match_evidence"]} != {None}
    assert r1["rmp_id"] != r2["rmp_id"]


def test_tie_or_insufficient_evidence_gives_null():
    # Two identical-looking candidates, no course overlap -> ambiguous -> no match.
    cands = [
        cand("Aaron", "Jackson", dept="Marketing"),
        cand("Aaron", "Jackson", dept="Religion"),
    ]
    res = match_professor("Aaron Jackson", cands, [], [])
    assert not res.matched
    assert res.ambiguous


def test_single_candidate_contradicting_dept_gives_null():
    res = match_professor(
        "Aaron Jackson",
        [cand("Aaron", "Jackson", dept="Biology")],
        ["MKTG 201"],
        ["MKTG"],
    )
    assert not res.matched
    assert "contradict" in res.reason or "department contradicts" in res.reason


# ── Migration (PART 1) ───────────────────────────────────────────────────────

def test_migration_preserves_ratings_and_is_idempotent(tmp_path):
    dbp = str(tmp_path / "mig.db")

    async def build_legacy():
        async with aiosqlite.connect(dbp) as c:
            await c.executescript(
                """
                CREATE TABLE professors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    byu_name TEXT NOT NULL UNIQUE,
                    rmp_id TEXT, rmp_name TEXT,
                    avg_difficulty REAL, avg_rating REAL,
                    would_take_again REAL, num_ratings INTEGER,
                    match_confidence REAL, last_fetched TEXT
                );
                INSERT INTO professors
                    (byu_name, rmp_id, rmp_name, avg_rating, num_ratings, match_confidence)
                VALUES ('melissa larson', 'T-x', 'Melissa Larson', 4.4, 350, 1.0);
                """
            )
            await c.commit()

    asyncio.run(build_legacy())

    orig = database.DB
    database.DB = dbp
    try:
        asyncio.run(database.migrate_professor_identity())
        asyncio.run(database.migrate_professor_identity())  # idempotent

        async def check():
            async with aiosqlite.connect(dbp) as c:
                c.row_factory = aiosqlite.Row
                async with c.execute(
                    "SELECT * FROM professors WHERE byu_name='melissa larson'"
                ) as cur:
                    return dict((await cur.fetchone()))

        row = asyncio.run(check())
        assert row["avg_rating"] == 4.4
        assert row["num_ratings"] == 350
        assert row["person_id"] is None  # legacy, awaiting person_id stamp
    finally:
        database.DB = orig

"""Tests for course-code normalization and fuzzy matching (no network/DB)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.matching import (  # noqa: E402
    closest_courses,
    collapse,
    match_course,
    normalize_course_code,
    normalize_name,
    parse_course_code,
)


def test_normalize_course_code_ignores_case_spaces_hyphens():
    assert normalize_course_code("mktg201") == "MKTG 201"
    assert normalize_course_code("MKTG 201") == "MKTG 201"
    assert normalize_course_code("strat-305") == "STRAT 305"
    assert normalize_course_code("  math 110 ") == "MATH 110"
    assert normalize_course_code("MKTG_201") == "MKTG 201"


def test_normalize_course_code_trailing_letter():
    assert normalize_course_code("cs 110r") == "CS 110R"
    assert normalize_course_code("CS110R") == "CS 110R"


def test_normalize_course_code_unparseable_best_effort():
    # Dept-only prefix (while typing) has no catalog number yet.
    assert normalize_course_code("mktg") == "MKTG"


def test_parse_course_code():
    assert parse_course_code("MKTG 201") == ("MKTG", "201")
    assert parse_course_code("mktg201") == ("MKTG", "201")
    assert parse_course_code("justdept") is None


def test_collapse():
    assert collapse("mktg-201") == "MKTG201"
    assert collapse("  STRAT 305 ") == "STRAT305"


def test_closest_courses_ranks_exact_highest():
    cands = ["MKTG 201", "MKTG 202", "STRAT 305", "ACC 200"]
    top = closest_courses("MKTG 201", cands, limit=3)
    assert top[0][0] == "MKTG 201"
    assert top[0][1] == 100


def test_match_course_exact_match():
    cands = ["MKTG 201", "MKTG 202", "STRAT 305"]
    exact, suggestions = match_course("mktg201", cands)
    assert exact == "MKTG 201"
    assert suggestions == []


def test_match_course_suggests_when_no_exact():
    cands = ["MKTG 201", "MKTG 202", "STRAT 305"]
    exact, suggestions = match_course("MKTG 210", cands)
    assert exact is None
    assert suggestions
    assert all(code in cands for code, _ in suggestions)


def test_normalize_name_for_rmp_key():
    assert normalize_name("  Sarah   Smith ") == "sarah smith"
    assert normalize_name("O'Hara-James") == "o hara james"
    # Idempotent + consistent: same person, same key regardless of spacing/case.
    assert normalize_name("John Doe") == normalize_name(" JOHN  doe ")
    assert normalize_name(normalize_name("John Doe")) == "john doe"

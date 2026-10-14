"""BYU section scraper for the AI schedule builder.

Enumerates *every* section for a term by department -> course -> section (not
by building), parses meeting times into per-day rows in minutes-since-midnight,
and detects online sections.

Data flow (mirrors the browser's own ajax calls — see js/desktop.js):

  1. GET  index.php                -> extracts `_session_id`
  2. POST ajax/getYeartermData.php -> `department_list` (teaching-area codes)
  3. POST ajax/getClasses.php      -> courses for a department
       (searchObject[teaching_areas][] + yearterm)
  4. POST ajax/getSections.php     -> sections with `times`, `instructors`,
       `availability`, `mode`, `title`, `credits`

Politeness: async httpx with a semaphore (max 10) and a 0.5s delay between
requests. Re-running is idempotent — sections are upserted and meetings are
replaced, so a term can be re-scraped without duplicating rows.

CLI:
    python -m backend.section_scraper --term 20271
    python -m backend.section_scraper --term 20271 --dept MKTG   # just one dept
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
from typing import Optional

import httpx

from .config import settings
from .database import (
    init_schedule_db,
    replace_section_meetings,
    upsert_professor,
    upsert_section,
)
from .matching import normalize_course_code, normalize_name
from .scraper import create_client, fetch_sections, get_session_id

logger = logging.getLogger("freeroomfinder.section_scraper")

MAX_CONCURRENCY = 10
DELAY_SECONDS = 0.5

# section `mode` values that mean "not a physical on-campus class"
ONLINE_MODES = {"Live Remote Delivery", "On-Demand Remote Delivery"}

# BYU weekday flags -> full day name (Thursday is "R" in the API).
_DAY_FLAGS: list[tuple[str, str]] = [
    ("mon", "Monday"),
    ("tue", "Tuesday"),
    ("wed", "Wednesday"),
    ("thu", "Thursday"),
    ("fri", "Friday"),
    ("sat", "Saturday"),
    ("sun", "Sunday"),
]


def _api_url(path: str) -> str:
    return f"{settings.byu_url.rstrip('/')}/{path.lstrip('/')}"


# ── Pure parsing helpers (unit-tested against saved fixtures) ───────────────

def hhmm_to_minutes(hhmm: Optional[str]) -> Optional[int]:
    """``"0800"`` -> 480; ``"0915"`` -> 555. None for null/TBA/malformed."""
    if not hhmm:
        return None
    digits = "".join(ch for ch in str(hhmm) if ch.isdigit())
    if not digits:
        return None
    digits = digits.zfill(4)
    hour, minute = int(digits[:2]), int(digits[2:])
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def is_online_section(section: dict) -> bool:
    """True when a section is a remote/online offering (not classroom)."""
    mode = (section.get("mode") or "").strip()
    return mode in ONLINE_MODES


_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_BYU_DATE_RE = re.compile(r"^\s*(\d{1,2})-([A-Za-z]{3})-(\d{2,4})\s*$")


def parse_byu_date(raw: Optional[str]) -> Optional[str]:
    """``"06-JAN-27"`` -> ``"2027-01-06"`` (ISO). None for missing/malformed.

    getSections.php returns ``start_date`` / ``end_date`` in ``DD-MON-YY``
    form; half-semester sections carry a narrower range (TERM 1 vs TERM 2).
    """
    if not raw:
        return None
    m = _BYU_DATE_RE.match(str(raw))
    if not m:
        return None
    day = int(m.group(1))
    mon = m.group(2).upper()
    year = int(m.group(3))
    if mon not in _MONTHS:
        return None
    if year < 100:
        year += 2000
    try:
        return f"{year:04d}-{_MONTHS[mon]:02d}-{day:02d}"
    except ValueError:
        return None


def meetings_from_times(
    times: list[dict] | None,
) -> list[tuple[str, int, int, str | None, str | None]]:
    """Flatten a section's meeting blocks into one row per weekday.

    Returns (day, start_min, end_min, building, room). Handles sections with
    multiple meeting blocks (lecture + lab). Blocks with no start/end time or
    no weekday flags (e.g. async "On-Demand Remote Delivery") produce no rows.
    """
    out: list[tuple[str, int, int, str | None, str | None]] = []
    for block in times or []:
        start = hhmm_to_minutes(block.get("begin_time"))
        end = hhmm_to_minutes(block.get("end_time"))
        if start is None or end is None:
            continue
        building = block.get("building") or None
        room = block.get("room") or None
        if room and room.upper() == "TBA":
            room = None
        for flag, day in _DAY_FLAGS:
            if block.get(flag):
                out.append((day, start, end, building, room))
    return out


def primary_instructor(section: dict) -> tuple[Optional[str], Optional[str]]:
    """Return (person_id, display_name) for the PRIMARY instructor.

    `person_id` is BYU's stable instructor identifier (present on every
    instructor object in getSections.php). Falls back to sort_name when
    preferred_first/preferred_surname are absent.
    """
    instructors = section.get("instructors") or []
    primary = next(
        (i for i in instructors if i.get("attribute_type") == "PRIMARY"),
        instructors[0] if instructors else None,
    )
    if not primary:
        return None, None
    person_id = primary.get("person_id") or None
    first = (primary.get("preferred_first_name") or "").strip()
    last = (primary.get("preferred_surname") or "").strip()
    full = f"{first} {last}".strip()
    name = full or (primary.get("sort_name") or "").strip() or None
    return person_id, name


def primary_instructor_name(section: dict) -> Optional[str]:
    """Display name of the PRIMARY instructor (or None)."""
    return primary_instructor(section)[1]


def section_seats_open(section: dict) -> Optional[int]:
    """``availability.seats_available`` as an int, or None."""
    avail = section.get("availability") or {}
    raw = avail.get("seats_available")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def section_credits(section: dict, course: dict | None) -> Optional[float]:
    """Credit hours as a float (section value first, then course fallback)."""
    raw = section.get("credit_hours")
    if raw is None and course is not None:
        raw = course.get("credit_hours") or course.get("minimum_credit_hours")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def course_code_for(course: dict) -> str:
    """Canonical 'DEPT NUM' for a getClasses course object."""
    dept = (course.get("dept_name") or "").strip()
    num = (course.get("catalog_number") or "").strip()
    suffix = (course.get("catalog_suffix") or "").strip()
    return normalize_course_code(f"{dept} {num}{suffix}")


# ── Async HTTP layer ─────────────────────────────────────────────────────────

async def fetch_departments(
    client: httpx.AsyncClient, session_id: str, year_term: str
) -> dict[str, str]:
    """Return {teaching_area_code: display_name} for a term (empty on failure)."""
    try:
        resp = await client.post(
            _api_url("ajax/getYeartermData.php"),
            data={"yearterm": year_term},
            timeout=settings.request_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("fetch_departments(%s): %s", year_term, e)
        return {}
    dl = data.get("department_list") if isinstance(data, dict) else None
    if not isinstance(dl, dict):
        return {}
    return {str(k): str(v) for k, v in dl.items()}


async def fetch_yearterm_options(
    client: httpx.AsyncClient, session_id: str
) -> list[dict]:
    """Return BYU's available yearterms as ``[{term, label}]`` (empty on failure).

    ``getYeartermData.php`` exposes the term list under a few possible keys; we
    accept the common shapes and normalize each entry to ``{term, label}``.
    """
    try:
        resp = await client.post(
            _api_url("ajax/getYeartermData.php"),
            data={},
            timeout=settings.request_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("fetch_yearterm_options: %s", e)
        return []
    if not isinstance(data, dict):
        return []
    raw = None
    for key in ("yearterm_list", "yearterms", "yearterm_options", "terms"):
        v = data.get(key)
        if isinstance(v, list) and v:
            raw = v
            break
    if raw is None:
        return []
    out: list[dict] = []
    for item in raw:
        if isinstance(item, dict):
            term = (
                item.get("yearterm") or item.get("term")
                or item.get("code") or item.get("id")
            )
            label = (
                item.get("year_term_desc") or item.get("label")
                or item.get("description") or item.get("name") or term
            )
        elif isinstance(item, str):
            term = label = item
        else:
            continue
        if term:
            out.append({"term": str(term), "label": str(label or term)})
    return out


async def fetch_courses_for_department(
    client: httpx.AsyncClient, session_id: str, year_term: str, dept: str
) -> dict[str, dict]:
    """Return {course_id: course} for one department (empty on failure/None)."""
    try:
        resp = await client.post(
            _api_url("ajax/getClasses.php"),
            data={
                "searchObject[yearterm]": year_term,
                "searchObject[teaching_areas][]": dept,
                "sessionId": session_id,
            },
            timeout=settings.request_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("fetch_courses_for_department(%s): %s", dept, e)
        return {}
    if isinstance(data, dict):
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    return {}


# ── Persistence ──────────────────────────────────────────────────────────────

async def _persist_course(
    term: str, course: dict, sections: list[dict]
) -> int:
    """Write one course's sections + meetings to the DB. Returns section count."""
    code = course_code_for(course)
    title = course.get("title")
    written = 0
    for section in sections:
        section_number = str(section.get("section_number") or "")
        if not section_number:
            continue

        person_id, instructor_name = primary_instructor(section)
        instructor_id = None
        if instructor_name:
            instructor_id = await upsert_professor(person_id, instructor_name)

        section_id = await upsert_section(
            term=term,
            course_code=code,
            section_number=section_number,
            title=title,
            credits=section_credits(section, course),
            instructor_id=instructor_id,
            is_online=is_online_section(section),
            seats_open=section_seats_open(section),
            start_date=parse_byu_date(section.get("start_date")),
            end_date=parse_byu_date(section.get("end_date")),
        )
        await replace_section_meetings(
            section_id, meetings_from_times(section.get("times"))
        )
        written += 1
    return written


async def scrape_term(
    year_term: str,
    departments: Optional[dict[str, str]] = None,
    only_departments: Optional[list[str]] = None,
    progress: Optional[dict] = None,
) -> dict:
    """Scrape every section for a term. Returns summary stats.

    ``progress`` (optional) is a mutable dict updated in place for the admin
    live-progress polling: keys ``phase``, ``departments_total``,
    ``departments_done``, ``courses``, ``sections``.
    """
    await init_schedule_db()
    stats = {"departments": 0, "courses": 0, "sections": 0}
    if progress is not None:
        progress.update({
            "phase": "starting", "departments_total": 0, "departments_done": 0,
            "courses": 0, "sections": 0,
        })
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async with create_client() as client:
        session_id = await get_session_id(client)
        if departments is None:
            departments = await fetch_departments(client, session_id, year_term)
        dept_codes = list(departments.keys())
        if only_departments:
            dept_codes = [d for d in dept_codes if d in set(only_departments)]
        stats["departments"] = len(dept_codes)
        if progress is not None:
            progress["departments_total"] = len(dept_codes)
            progress["phase"] = "courses"

        # Phase 1: enumerate courses per department.
        courses_by_id: dict[str, dict] = {}

        async def collect_courses(dept: str) -> None:
            async with sem:
                await asyncio.sleep(DELAY_SECONDS)
                found = await fetch_courses_for_department(
                    client, session_id, year_term, dept
                )
            for cid, course in found.items():
                courses_by_id[cid] = course
            if progress is not None:
                progress["departments_done"] += 1
                progress["courses"] = len(courses_by_id)

        await asyncio.gather(*(collect_courses(d) for d in dept_codes))
        stats["courses"] = len(courses_by_id)
        if progress is not None:
            progress["phase"] = "sections"
            progress["departments_done"] = len(dept_codes)

        # Phase 2: fetch + persist sections per course.
        async def persist_one(cid: str) -> int:
            course = courses_by_id[cid]
            async with sem:
                await asyncio.sleep(DELAY_SECONDS)
                sections = await fetch_sections(client, session_id, cid, year_term)
            written = await _persist_course(year_term, course, sections)
            if progress is not None:
                progress["sections"] += written
            return written

        written = await asyncio.gather(*(persist_one(cid) for cid in courses_by_id))
        stats["sections"] = sum(written)

    return stats


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Scrape BYU sections for a term")
    parser.add_argument(
        "--term", default=settings.current_term,
        help=f"BYU year-term code (default: {settings.current_term})",
    )
    parser.add_argument(
        "--dept", action="append", default=None,
        help="Restrict to one or more teaching-area codes (repeatable)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    logger.info("Scraping sections for term %s (depts: %s)", args.term, args.dept or "all")
    stats = await scrape_term(args.term, only_departments=args.dept)
    logger.info("Done: %s", stats)
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

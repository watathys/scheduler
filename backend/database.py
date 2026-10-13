import asyncio

import aiosqlite
from datetime import datetime, timezone
from typing import Optional
from .config import settings
from .matching import normalize_name

DB = settings.db_path

# Serializes professor upserts. The section scraper persists courses
# concurrently, and two courses taught by the same person can otherwise race
# the check-then-insert in `upsert_professor` and trip the `person_id` UNIQUE
# constraint.
_professor_lock = asyncio.Lock()

# ── Schedule builder: schema ────────────────────────────────────────────────
# Times are minutes-since-midnight; each class meeting is one row per weekday
# (`day` holds a full day name, e.g. "Monday").

SB_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sections (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    term           TEXT NOT NULL,
    course_code    TEXT NOT NULL,        -- canonical 'MKTG 201'
    section_number TEXT NOT NULL,
    title          TEXT,
    credits        REAL,
    instructor_id  INTEGER,              -- references professors(id)
    is_online      INTEGER NOT NULL DEFAULT 0,
    seats_open     INTEGER,
    last_scraped   TEXT NOT NULL,
    UNIQUE(term, course_code, section_number)
);

CREATE TABLE IF NOT EXISTS meetings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    section_id INTEGER NOT NULL REFERENCES sections(id),
    day        TEXT NOT NULL,            -- 'Monday' .. 'Sunday'
    start_min  INTEGER NOT NULL,         -- minutes since midnight
    end_min    INTEGER NOT NULL,
    building   TEXT,
    room       TEXT
);

CREATE TABLE IF NOT EXISTS professors (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id        TEXT UNIQUE,          -- BYU person_id (NULL for legacy rows)
    byu_name         TEXT NOT NULL,        -- normalized display name
    rmp_id           TEXT,
    rmp_name         TEXT,
    avg_difficulty   REAL,
    avg_rating       REAL,
    would_take_again REAL,
    num_ratings      INTEGER,
    match_confidence REAL,
    match_reason     TEXT,                 -- human-readable reason for the match
    match_evidence   TEXT,                 -- JSON: scoring breakdown for audit
    last_fetched     TEXT
);

CREATE INDEX IF NOT EXISTS idx_sections_term_course
    ON sections(term, course_code);
CREATE INDEX IF NOT EXISTS idx_meetings_section
    ON meetings(section_id);
"""


async def init_schedule_db() -> None:
    """Create the schedule-builder tables (idempotent), then migrate."""
    async with aiosqlite.connect(DB) as db:
        await db.executescript(SB_SCHEMA_SQL)
        await db.commit()
    await migrate_professor_identity()


async def migrate_professor_identity() -> None:
    """Migrate `professors` from byu_name-keyed to person_id-keyed, preserving RMP data.

    Legacy rows (keyed by unique `byu_name`) are copied verbatim with
    `person_id = NULL`; their RMP ratings survive. The scraper stamps
    `person_id` onto a legacy row the first time it sees that person, so
    re-running the scraper stays idempotent and no ratings are lost. Row `id`s
    are preserved so `sections.instructor_id` keeps pointing at the same rows.
    """
    async with aiosqlite.connect(DB) as db:
        async with db.execute("PRAGMA table_info(professors)") as cur:
            cols = [r[1] for r in await cur.fetchall()]
    if "person_id" in cols:
        return  # already migrated (or created fresh with the new schema)

    async with aiosqlite.connect(DB) as db:
        await db.executescript(
            """
            ALTER TABLE professors RENAME TO professors_legacy;

            CREATE TABLE professors (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id        TEXT UNIQUE,
                byu_name         TEXT NOT NULL,
                rmp_id           TEXT,
                rmp_name         TEXT,
                avg_difficulty   REAL,
                avg_rating       REAL,
                would_take_again REAL,
                num_ratings      INTEGER,
                match_confidence REAL,
                match_reason     TEXT,
                match_evidence   TEXT,
                last_fetched     TEXT
            );

            INSERT INTO professors
                (id, byu_name, rmp_id, rmp_name, avg_difficulty, avg_rating,
                 would_take_again, num_ratings, match_confidence, last_fetched)
            SELECT id, byu_name, rmp_id, rmp_name, avg_difficulty, avg_rating,
                   would_take_again, num_ratings, match_confidence, last_fetched
            FROM professors_legacy;

            DROP TABLE professors_legacy;
            """
        )
        await db.commit()


# ── Schedule builder: professors ────────────────────────────────────────────

async def upsert_professor(person_id: str | None, byu_name: str) -> int:
    """Upsert a professor keyed by BYU `person_id`; return its id.

    Three cases (idempotent across re-scrapes):
      1. `person_id` already exists -> update the display name, return its id.
      2. No such person, but a legacy row (person_id IS NULL) with the same
         display name exists -> stamp `person_id` onto it, preserving any RMP
         ratings already stored there.
      3. Otherwise -> create a new row.

    `byu_name` is normalized into the display field.
    """
    key = normalize_name(byu_name)
    async with _professor_lock:
        async with aiosqlite.connect(DB) as db:
            if person_id:
                async with db.execute(
                    "SELECT id FROM professors WHERE person_id = ?", (person_id,)
                ) as cur:
                    row = await cur.fetchone()
                if row:
                    await db.execute(
                        "UPDATE professors SET byu_name = ? WHERE id = ?", (key, row[0])
                    )
                    await db.commit()
                    return row[0]

            # Adopt a legacy (person_id-less) row with the same name, if any.
            async with db.execute(
                "SELECT id FROM professors WHERE person_id IS NULL AND byu_name = ?",
                (key,),
            ) as cur:
                row = await cur.fetchone()
            if row:
                await db.execute(
                    "UPDATE professors SET person_id = ? WHERE id = ?",
                    (person_id, row[0]),
                )
                await db.commit()
                return row[0]

            cur = await db.execute(
                "INSERT INTO professors (person_id, byu_name) VALUES (?, ?)",
                (person_id, key),
            )
            await db.commit()
            return cur.lastrowid


async def update_professor_ratings(
    professor_id: int,
    rmp_id: str | None,
    rmp_name: str | None,
    avg_difficulty: float | None,
    avg_rating: float | None,
    would_take_again: float | None,
    num_ratings: int | None,
    match_confidence: float | None,
    match_reason: str | None = None,
    match_evidence: str | None = None,
) -> None:
    """Record RMP ratings for a professor row, keyed by `professors.id`."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """
            UPDATE professors SET
                rmp_id = ?, rmp_name = ?, avg_difficulty = ?, avg_rating = ?,
                would_take_again = ?, num_ratings = ?, match_confidence = ?,
                match_reason = ?, match_evidence = ?, last_fetched = ?
            WHERE id = ?
            """,
            (
                rmp_id, rmp_name, avg_difficulty, avg_rating, would_take_again,
                num_ratings, match_confidence, match_reason, match_evidence,
                now, professor_id,
            ),
        )
        await db.commit()


async def get_professors_needing_refresh(
    cutoff: str, limit: int | None = None, force: bool = False
) -> list[dict]:
    """Professors due for an RMP lookup.

    A professor is due when never fetched (`last_fetched IS NULL`) or fetched
    more than `cutoff` ago. `force=True` returns everyone. Returns `id`,
    `person_id`, and `byu_name`.
    """
    sql = "SELECT id, person_id, byu_name FROM professors"
    params: list = []
    if not force:
        sql += " WHERE last_fetched IS NULL OR last_fetched < ?"
        params.append(cutoff)
    sql += " ORDER BY byu_name"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_professor_courses(instructor_id: int) -> list[str]:
    """Distinct canonical course codes this instructor teaches (e.g. 'MKTG 201')."""
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT DISTINCT course_code FROM sections WHERE instructor_id = ?",
            (instructor_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [r[0] for r in rows if r[0]]


async def mark_professor_fetched(professor_id: int) -> None:
    """Set `last_fetched` to now, leaving rating fields untouched (NULL).

    Used for 'not found' / ambiguous / low-confidence professors so they are
    not re-queried every run (retried after the 120-day window).
    """
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE professors SET last_fetched = ? WHERE id = ?",
            (now, professor_id),
        )
        await db.commit()


# ── Schedule builder: sections & meetings ───────────────────────────────────

async def upsert_section(
    term: str,
    course_code: str,
    section_number: str,
    title: str | None = None,
    credits: float | None = None,
    instructor_id: int | None = None,
    is_online: bool = False,
    seats_open: int | None = None,
) -> int:
    """Insert/update a section for a term; return its row id."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """
            INSERT INTO sections
                (term, course_code, section_number, title, credits,
                 instructor_id, is_online, seats_open, last_scraped)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(term, course_code, section_number) DO UPDATE SET
                title         = excluded.title,
                credits       = excluded.credits,
                instructor_id = excluded.instructor_id,
                is_online     = excluded.is_online,
                seats_open    = excluded.seats_open,
                last_scraped  = excluded.last_scraped
            """,
            (
                term, course_code, str(section_number), title, credits,
                instructor_id, int(is_online), seats_open, now,
            ),
        )
        await db.commit()
        async with db.execute(
            "SELECT id FROM sections WHERE term=? AND course_code=? AND section_number=?",
            (term, course_code, str(section_number)),
        ) as cur:
            row = await cur.fetchone()
    return row[0]


async def replace_section_meetings(
    section_id: int,
    meetings: list[tuple[str, int, int, str | None, str | None]],
) -> None:
    """Replace all meeting rows for a section (idempotent re-scrape).

    `meetings`: list of (day, start_min, end_min, building, room).
    """
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM meetings WHERE section_id = ?", (section_id,))
        if meetings:
            await db.executemany(
                """
                INSERT INTO meetings (section_id, day, start_min, end_min, building, room)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (section_id, day, int(start_min), int(end_min), building, room)
                    for (day, start_min, end_min, building, room) in meetings
                ],
            )
        await db.commit()


async def get_sections_for_term(term: str) -> list[dict]:
    """All sections for a term, with their meetings nested."""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM sections WHERE term=? ORDER BY course_code, section_number",
            (term,),
        ) as cur:
            sections = [dict(r) for r in await cur.fetchall()]
        for s in sections:
            async with db.execute(
                "SELECT day, start_min, end_min, building, room FROM meetings "
                "WHERE section_id=? ORDER BY day, start_min",
                (s["id"],),
            ) as cur:
                s["meetings"] = [dict(r) for r in await cur.fetchall()]
        return sections


async def get_section_meetings(section_id: int) -> list[dict]:
    """Meeting rows for one section (day, start_min, end_min, building, room)."""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT day, start_min, end_min, building, room FROM meetings "
            "WHERE section_id=? ORDER BY day, start_min",
            (section_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── Schedule builder: scheduler API helpers ─────────────────────────────────

async def get_latest_sections_term() -> str | None:
    """Newest term code present in the sections table, or None when empty.

    Term codes are ``YYYYT``; the leading year makes string ordering match
    calendar order (``"20271"`` Winter 2027 > ``"20265"`` Fall 2026).
    """
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT term FROM sections ORDER BY term DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def count_sections() -> int:
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT COUNT(*) FROM sections") as cur:
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def get_scheduler_stats() -> dict:
    """Aggregate counts + freshness timestamps for GET /api/v1/scheduler/status."""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        stats: dict = {}

        async with db.execute("SELECT COUNT(*) AS c FROM sections") as cur:
            stats["section_count"] = (await cur.fetchone())["c"]
        async with db.execute("SELECT COUNT(*) AS c FROM meetings") as cur:
            stats["meeting_count"] = (await cur.fetchone())["c"]
        async with db.execute("SELECT COUNT(*) AS c FROM professors") as cur:
            stats["professor_count"] = (await cur.fetchone())["c"]
        async with db.execute(
            "SELECT COUNT(*) AS c FROM professors WHERE num_ratings IS NOT NULL"
        ) as cur:
            stats["professors_with_ratings"] = (await cur.fetchone())["c"]
        async with db.execute("SELECT MAX(last_scraped) AS t FROM sections") as cur:
            row = await cur.fetchone()
            stats["last_scrape_time"] = row["t"] if row else None
        async with db.execute("SELECT MAX(last_fetched) AS t FROM professors") as cur:
            row = await cur.fetchone()
            stats["last_rmp_refresh_time"] = row["t"] if row else None
    return stats

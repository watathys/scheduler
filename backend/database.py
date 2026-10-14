import asyncio
import json
import os

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
    start_date     TEXT,                  -- ISO 'YYYY-MM-DD' (half-semester support)
    end_date       TEXT,                  -- ISO 'YYYY-MM-DD' or NULL (full term)
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

-- Registration Cart "envelopes": pre-bundled class groups a student must
-- register for together. The source of truth is data/envelopes/<term>/*.json
-- (parsed from the Registration Cart's "Choose an envelope" screens).
CREATE TABLE IF NOT EXISTS envelopes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    term          TEXT NOT NULL,
    slug          TEXT NOT NULL,
    title         TEXT NOT NULL,          -- as pasted (may be misspelled)
    display_title TEXT NOT NULL,          -- cleaned display title
    captured_at   TEXT NOT NULL,          -- ISO date the cart text was captured
    UNIQUE(term, slug)
);

CREATE TABLE IF NOT EXISTS envelope_bundles (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    envelope_id       INTEGER NOT NULL REFERENCES envelopes(id),
    bundle_index      INTEGER NOT NULL,   -- order within the envelope
    env_id            TEXT,               -- NULL when the cart lists no Env number
    capacity_snapshot INTEGER,
    seats_snapshot    INTEGER,
    ok                INTEGER,            -- 1 ok / 0 mismatch|missing / NULL unverified
    UNIQUE(envelope_id, bundle_index)
);

CREATE TABLE IF NOT EXISTS envelope_bundle_sections (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    bundle_id           INTEGER NOT NULL REFERENCES envelope_bundles(id),
    section_id          INTEGER REFERENCES sections(id),  -- NULL when not resolved
    course_code         TEXT NOT NULL,
    section_number      TEXT NOT NULL,
    instructor_snapshot TEXT,
    mode                TEXT,
    credits             REAL,
    part_of_term        TEXT,
    note_flag           INTEGER NOT NULL DEFAULT 0,
    UNIQUE(bundle_id, course_code, section_number)
);

-- Per-term refresh admin (see backend/admin.py).
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS job_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job         TEXT NOT NULL,          -- sections / rmp / envelopes_verify
    term        TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',  -- running / ok / error
    counts_json TEXT,
    error       TEXT                     -- generic message only (never the secret)
);

CREATE TABLE IF NOT EXISTS envelope_sources (
    term     TEXT PRIMARY KEY,
    raw_text TEXT NOT NULL,
    saved_at TEXT NOT NULL
);
"""


async def init_schedule_db() -> None:
    """Create the schedule-builder tables (idempotent), then migrate."""
    async with aiosqlite.connect(DB) as db:
        await db.executescript(SB_SCHEMA_SQL)
        await db.commit()
    await migrate_professor_identity()
    await migrate_section_dates()


async def migrate_section_dates() -> None:
    """Add ``sections.start_date`` / ``sections.end_date`` (additive).

    ``getSections.php`` returns ``start_date`` / ``end_date`` (e.g. ``06-JAN-27``)
    for every section; half-semester sections have narrower ranges (TERM 1 vs
    TERM 2). Existing databases lack these columns, so this migration adds them
    without disturbing existing rows (values stay NULL until the next scrape).
    """
    async with aiosqlite.connect(DB) as db:
        async with db.execute("PRAGMA table_info(sections)") as cur:
            cols = {r[1] for r in await cur.fetchall()}

    missing = [c for c in ("start_date", "end_date") if c not in cols]
    if not missing:
        return

    async with aiosqlite.connect(DB) as db:
        for col in missing:
            await db.execute(f"ALTER TABLE sections ADD COLUMN {col} TEXT")
        await db.commit()


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
    start_date: str | None = None,
    end_date: str | None = None,
) -> int:
    """Insert/update a section for a term; return its row id."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """
            INSERT INTO sections
                (term, course_code, section_number, title, credits,
                 instructor_id, is_online, seats_open, start_date, end_date,
                 last_scraped)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(term, course_code, section_number) DO UPDATE SET
                title         = excluded.title,
                credits       = excluded.credits,
                instructor_id = excluded.instructor_id,
                is_online     = excluded.is_online,
                seats_open    = excluded.seats_open,
                start_date    = excluded.start_date,
                end_date      = excluded.end_date,
                last_scraped  = excluded.last_scraped
            """,
            (
                term, course_code, str(section_number), title, credits,
                instructor_id, int(is_online), seats_open, start_date, end_date,
                now,
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


async def get_scheduler_stats(term: str) -> dict:
    """Aggregate counts + freshness timestamps for GET /api/v1/scheduler/status.

    Section/meeting counts are scoped to ``term`` (the published term) so a
    scraped-but-unpublished term stays invisible to students. Professor counts
    are global (professors are not term-scoped).
    """
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        stats: dict = {}

        async with db.execute(
            "SELECT COUNT(*) AS c FROM sections WHERE term = ?", (term,)
        ) as cur:
            stats["section_count"] = (await cur.fetchone())["c"]
        async with db.execute(
            "SELECT COUNT(*) AS c FROM meetings m "
            "JOIN sections s ON s.id = m.section_id WHERE s.term = ?",
            (term,),
        ) as cur:
            stats["meeting_count"] = (await cur.fetchone())["c"]
        async with db.execute("SELECT COUNT(*) AS c FROM professors") as cur:
            stats["professor_count"] = (await cur.fetchone())["c"]
        async with db.execute(
            "SELECT COUNT(*) AS c FROM professors WHERE num_ratings IS NOT NULL"
        ) as cur:
            stats["professors_with_ratings"] = (await cur.fetchone())["c"]
        async with db.execute(
            "SELECT MAX(last_scraped) AS t FROM sections WHERE term = ?", (term,)
        ) as cur:
            row = await cur.fetchone()
            stats["last_scrape_time"] = row["t"] if row else None
        async with db.execute("SELECT MAX(last_fetched) AS t FROM professors") as cur:
            row = await cur.fetchone()
            stats["last_rmp_refresh_time"] = row["t"] if row else None
    return stats


# ── Envelopes: load / verify / read ─────────────────────────────────────────

async def resolve_section_id(term: str, course_code: str, section_number: str) -> int | None:
    """Resolve a bundle section to ``sections.id`` by (term, course, number).

    The Registration Cart sometimes drops a trailing ``R`` suffix (e.g. "MBA 693"
    vs BYU's canonical "MBA 693R"), so we fall back to appending an ``R``.
    """
    candidates = [course_code]
    if not course_code.endswith("R"):
        candidates.append(course_code + "R")
    async with aiosqlite.connect(DB) as db:
        for code in candidates:
            async with db.execute(
                "SELECT id FROM sections WHERE term=? AND course_code=? AND section_number=?",
                (term, code, str(section_number)),
            ) as cur:
                row = await cur.fetchone()
            if row:
                return row[0]
    return None


async def replace_envelope(
    term: str,
    slug: str,
    title: str,
    display_title: str,
    captured_at: str,
    bundles: list[dict],
) -> int:
    """Idempotently replace one envelope's bundles/sections; return envelope id.

    ``bundles``: list of dicts with keys ``env_id``, ``capacity_snapshot``,
    ``seats_snapshot`` and ``sections`` (a list of dicts each with
    ``section_id``, ``course_code``, ``section_number``, ``instructor_snapshot``,
    ``mode``, ``credits``, ``part_of_term``, ``note_flag``).
    """
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """
            INSERT INTO envelopes (term, slug, title, display_title, captured_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(term, slug) DO UPDATE SET
                title         = excluded.title,
                display_title = excluded.display_title,
                captured_at   = excluded.captured_at
            """,
            (term, slug, title, display_title, captured_at),
        )
        async with db.execute(
            "SELECT id FROM envelopes WHERE term=? AND slug=?", (term, slug)
        ) as cur:
            row = await cur.fetchone()
        envelope_id = row[0]

        await db.execute(
            "DELETE FROM envelope_bundle_sections WHERE bundle_id IN "
            "(SELECT id FROM envelope_bundles WHERE envelope_id=?)",
            (envelope_id,),
        )
        await db.execute(
            "DELETE FROM envelope_bundles WHERE envelope_id=?", (envelope_id,)
        )

        for idx, bundle in enumerate(bundles):
            cur = await db.execute(
                """
                INSERT INTO envelope_bundles
                    (envelope_id, bundle_index, env_id, capacity_snapshot, seats_snapshot)
                VALUES (?, ?, ?, ?, ?)
                """,
                (envelope_id, idx, bundle.get("env_id"),
                 bundle.get("capacity_snapshot"), bundle.get("seats_snapshot")),
            )
            bundle_id = cur.lastrowid
            for sec in bundle.get("sections", []):
                await db.execute(
                    """
                    INSERT INTO envelope_bundle_sections
                        (bundle_id, section_id, course_code, section_number,
                         instructor_snapshot, mode, credits, part_of_term, note_flag)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bundle_id, sec.get("section_id"),
                        sec.get("course_code"), sec.get("section_number"),
                        sec.get("instructor_snapshot"), sec.get("mode"),
                        sec.get("credits"), sec.get("part_of_term"),
                        1 if sec.get("note_flag") else 0,
                    ),
                )
        await db.commit()
    return envelope_id


async def get_envelopes(term: str) -> list[dict]:
    """All envelopes for a term, with bundles and bundle sections nested."""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, slug, title, display_title, captured_at "
            "FROM envelopes WHERE term=? ORDER BY slug",
            (term,),
        ) as cur:
            envelopes = [dict(r) for r in await cur.fetchall()]

        for env in envelopes:
            async with db.execute(
                "SELECT id, bundle_index, env_id, capacity_snapshot, seats_snapshot, ok "
                "FROM envelope_bundles WHERE envelope_id=? ORDER BY bundle_index",
                (env["id"],),
            ) as cur:
                env["bundles"] = [dict(r) for r in await cur.fetchall()]
            for b in env["bundles"]:
                async with db.execute(
                    "SELECT section_id, course_code, section_number, instructor_snapshot, "
                    "mode, credits, part_of_term, note_flag "
                    "FROM envelope_bundle_sections WHERE bundle_id=? ORDER BY id",
                    (b["id"],),
                ) as cur:
                    b["sections"] = [dict(r) for r in await cur.fetchall()]
        return envelopes


async def update_bundle_ok(bundle_id: int, ok: int) -> None:
    """Stamp a bundle's verification status (1 = ok, 0 = mismatch/missing)."""
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE envelope_bundles SET ok=? WHERE id=?", (ok, bundle_id)
        )
        await db.commit()


async def get_sections_details(section_ids: list[int]) -> dict[int, dict]:
    """Actual meetings + instructor name for a set of section ids (for verify)."""
    out: dict[int, dict] = {}
    if not section_ids:
        return out
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        ph = ",".join("?" for _ in section_ids)
        async with db.execute(
            f"""
            SELECT s.id, p.byu_name AS instructor_name
            FROM sections s
            LEFT JOIN professors p ON p.id = s.instructor_id
            WHERE s.id IN ({ph})
            """,
            section_ids,
        ) as cur:
            for r in await cur.fetchall():
                out[r["id"]] = {
                    "instructor_name": r["instructor_name"],
                    "meetings": [],
                }
        async with db.execute(
            f"""
            SELECT section_id, day, start_min, end_min, building, room
            FROM meetings WHERE section_id IN ({ph})
            ORDER BY section_id, day, start_min
            """,
            section_ids,
        ) as cur:
            for r in await cur.fetchall():
                if r["section_id"] in out:
                    out[r["section_id"]]["meetings"].append(dict(r))
    return out


# ── Admin: settings (key/value) ─────────────────────────────────────────────

async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


# ── Admin: job_runs ─────────────────────────────────────────────────────────

async def create_job_run(job: str, term: str | None = None) -> int:
    """Append a running job_runs row and return its id."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "INSERT INTO job_runs (job, term, started_at, status) "
            "VALUES (?, ?, ?, 'running')",
            (job, term, now),
        )
        await db.commit()
        return cur.lastrowid


async def update_job_run(
    job_id: int,
    status: str,
    counts_json: dict | None = None,
    error: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE job_runs SET finished_at = ?, status = ?, counts_json = ?, "
            "error = ? WHERE id = ?",
            (
                now,
                status,
                json.dumps(counts_json) if counts_json is not None else None,
                error,
                job_id,
            ),
        )
        await db.commit()


async def list_job_runs(limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, job, term, started_at, finished_at, status, "
            "counts_json, error FROM job_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_last_successful_jobs() -> dict[str, str | None]:
    """Most recent 'ok' finished_at per job type (sections/rmp/envelopes_verify)."""
    out: dict[str, str | None] = {}
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        for job in ("sections", "rmp", "envelopes_verify"):
            async with db.execute(
                "SELECT finished_at FROM job_runs "
                "WHERE job = ? AND status = 'ok' ORDER BY id DESC LIMIT 1",
                (job,),
            ) as cur:
                row = await cur.fetchone()
            out[job] = row["finished_at"] if row else None
    return out


# ── Admin: envelope_sources ─────────────────────────────────────────────────

async def save_envelope_source(term: str, raw_text: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO envelope_sources (term, raw_text, saved_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(term) DO UPDATE SET raw_text = excluded.raw_text, "
            "saved_at = excluded.saved_at",
            (term, raw_text, now),
        )
        await db.commit()


async def get_envelope_source(term: str) -> dict | None:
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT term, raw_text, saved_at FROM envelope_sources WHERE term = ?",
            (term,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def has_envelopes_for_term(term: str) -> bool:
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT 1 FROM envelopes WHERE term = ? LIMIT 1", (term,)
        ) as cur:
            row = await cur.fetchone()
    return row is not None


async def delete_envelopes_for_term(term: str) -> None:
    """Remove all envelopes (and their bundles/sections) for a term."""
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "DELETE FROM envelope_bundle_sections WHERE bundle_id IN "
            "(SELECT b.id FROM envelope_bundles b "
            " JOIN envelopes e ON e.id = b.envelope_id WHERE e.term = ?)",
            (term,),
        )
        await db.execute(
            "DELETE FROM envelope_bundles WHERE envelope_id IN "
            "(SELECT id FROM envelopes WHERE term = ?)",
            (term,),
        )
        await db.execute("DELETE FROM envelopes WHERE term = ?", (term,))
        await db.commit()


# ── Admin: dashboard aggregates ─────────────────────────────────────────────

async def get_terms_with_counts() -> list[dict]:
    """Every term present in sections, with a section count (newest first)."""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT term, COUNT(*) AS section_count FROM sections "
            "GROUP BY term ORDER BY term DESC"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_envelope_counts(term: str) -> dict:
    """ok / withheld bundle counts for a term (withheld = not verified-ok)."""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN b.ok = 1 THEN 1 ELSE 0 END), 0) AS ok,
                COALESCE(SUM(CASE WHEN b.ok IS NULL OR b.ok != 1 THEN 1 ELSE 0 END), 0)
                    AS withheld
            FROM envelope_bundles b
            JOIN envelopes e ON e.id = b.envelope_id
            WHERE e.term = ?
            """,
            (term,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return {"ok": 0, "withheld": 0}
    return {"ok": row["ok"], "withheld": row["withheld"]}


async def get_rating_coverage() -> dict:
    """professors with >= 3 ratings vs total professor count (global)."""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT COUNT(*) AS total FROM professors") as cur:
            total = (await cur.fetchone())["total"]
        async with db.execute(
            "SELECT COUNT(*) AS rated FROM professors "
            "WHERE num_ratings IS NOT NULL AND num_ratings >= 3"
        ) as cur:
            rated = (await cur.fetchone())["rated"]
    return {"total": total, "rated": rated}


async def get_rating_coverage_for_term(term: str) -> dict:
    """professors with >= 3 ratings vs total professors appearing in a term."""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT COUNT(DISTINCT s.instructor_id) AS total FROM sections s "
            "WHERE s.term = ? AND s.instructor_id IS NOT NULL",
            (term,),
        ) as cur:
            total = (await cur.fetchone())["total"]
        async with db.execute(
            "SELECT COUNT(DISTINCT s.instructor_id) AS rated FROM sections s "
            "JOIN professors p ON p.id = s.instructor_id "
            "WHERE s.term = ? AND p.num_ratings IS NOT NULL AND p.num_ratings >= 3",
            (term,),
        ) as cur:
            rated = (await cur.fetchone())["rated"]
    return {"total": total, "rated": rated}


async def get_review_candidates() -> list[dict]:
    """Matched professors needing human review (low confidence or multiple
    candidates). Returns raw fields; the caller builds the RMP URL."""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, byu_name, rmp_id, rmp_name, match_confidence, "
            "match_reason, match_evidence FROM professors "
            "WHERE match_confidence IS NOT NULL ORDER BY byu_name"
        ) as cur:
            rows = await cur.fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        conf = d.get("match_confidence")
        if conf is None:
            continue
        num_candidates = 1
        if d.get("match_evidence"):
            try:
                evidence = json.loads(d["match_evidence"])
                num_candidates = int(evidence.get("num_candidates") or 1)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        if conf >= 0.8 and num_candidates <= 1:
            continue
        d["num_candidates"] = num_candidates
        out.append(d)
    return out


def db_file_size() -> int:
    """Size of the SQLite DB file in bytes (0 when absent)."""
    try:
        return os.path.getsize(DB)
    except OSError:
        return 0

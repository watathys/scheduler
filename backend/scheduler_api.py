"""Scheduler API: course autocomplete, schedule generation, and status.

This is the only API surface; all routes live under ``/api/v1/scheduler``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import aiosqlite
from fastapi import APIRouter, HTTPException, Query, Request
from rapidfuzz import fuzz

from . import database, llm, solver
from .config import settings
from .matching import collapse, normalize_course_code
from .models import (
    CourseSearchResult,
    EnvelopeOut,
    GenerateRequest,
    GenerateResponse,
    GeneratedSchedule,
    SchedulerStatusOut,
    SolverRequest,
)

logger = logging.getLogger("freeroomfinder.scheduler_api")

router = APIRouter(prefix="/api/v1/scheduler", tags=["scheduler"])

# ── Tunable constants ─────────────────────────────────────────────────────────
# Per-request ceiling on the (CPU-bound) solver. The solver itself stops after
# ~5s of search; this is the backstop on the whole DB+solve round trip.
SCHEDULER_TIMEOUT_SECONDS = float(os.getenv("SCHEDULER_TIMEOUT_SECONDS", "20"))

# ── Rate limiting (in-memory, per IP) ────────────────────────────────────────
# TODO: this limiter is per-process and resets on restart / does not span
# multiple instances. For a multi-instance deployment, replace with a shared
# store (e.g. Redis) keyed by IP.
GENERATE_RATE_LIMIT = int(os.getenv("GENERATE_RATE_LIMIT", "10"))
GENERATE_RATE_WINDOW = 60.0
_generate_rate: dict[str, list[float]] = {}


def _check_generate_rate(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    hits = [t for t in _generate_rate.get(ip, []) if now - t < GENERATE_RATE_WINDOW]
    if len(hits) >= GENERATE_RATE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {GENERATE_RATE_LIMIT} schedule generations per minute.",
        )
    hits.append(now)
    _generate_rate[ip] = hits


# ── Term selection ───────────────────────────────────────────────────────────

async def get_active_term() -> str:
    """The term students register for.

    Priority: the admin-published ``settings.active_term`` (if set), then the
    newest term present in the sections table (the historical behavior), then
    the config's current term only when the table is empty.
    """
    term = await database.get_setting("active_term")
    if term:
        return term
    term = await database.get_latest_sections_term()
    return term or settings.current_term


async def sections_exist() -> bool:
    return await database.count_sections() > 0


# ── Autocomplete ─────────────────────────────────────────────────────────────

async def _load_courses(term: str) -> list[dict]:
    async with aiosqlite.connect(database.DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT course_code AS code,
                   MAX(title)   AS title,
                   MAX(credits) AS credits,
                   COUNT(*)     AS section_count
            FROM sections
            WHERE term = ?
            GROUP BY course_code
            """,
            (term,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


def _rank_courses(query: str, courses: list[dict], limit: int) -> list[CourseSearchResult]:
    """Prefix matches first (code or title), then fuzzy on the remainder."""
    qc = collapse(query)          # "mktg-201" -> "MKTG201"
    ql = query.lower()
    prefix: list[dict] = []
    rest: list[dict] = []
    for c in courses:
        code = c["code"] or ""
        title = c["title"] or ""
        if collapse(code).startswith(qc) or title.lower().startswith(ql):
            prefix.append(c)
        else:
            rest.append(c)

    prefix.sort(key=lambda c: (c["code"] or "", c["title"] or ""))

    if rest:
        scored: list[tuple[int, dict]] = []
        for c in rest:
            code_score = fuzz.ratio(qc, collapse(c["code"] or ""))
            title_score = (
                fuzz.partial_ratio(ql, (c["title"] or "").lower())
                if c["title"] else 0
            )
            scored.append((max(code_score, title_score), c))
        scored.sort(key=lambda t: (-t[0], t[1]["code"] or "", t[1]["title"] or ""))
        rest = [c for _, c in scored]

    out: list[CourseSearchResult] = []
    for c in (prefix + rest)[:limit]:
        out.append(CourseSearchResult(
            code=c["code"],
            title=c["title"],
            credits=c["credits"],
            section_count=c["section_count"],
        ))
    return out


@router.get("/courses/search", response_model=list[CourseSearchResult])
async def search_courses(
    q: str = Query(..., description="Course code or title fragment (min 2 chars)"),
    limit: int = Query(default=10, ge=1, le=50),
):
    text = q.strip()
    if len(text) < 2:
        return []
    term = await get_active_term()
    courses = await _load_courses(term)
    return _rank_courses(text, courses, limit)


# ── Schedule generation ──────────────────────────────────────────────────────

def _solve_sync(req: SolverRequest):
    """Run the async solver in a worker thread (it is CPU-bound + does DB I/O)."""
    return asyncio.run(solver.solve_schedule(req))


@router.post("/schedules/generate", response_model=GenerateResponse)
async def generate_schedules(request: Request, body: GenerateRequest):
    _check_generate_rate(request)

    if not await sections_exist():
        raise HTTPException(
            status_code=503,
            detail="Schedule data is still loading. Please try again in a moment.",
        )

    term = await get_active_term()

    # Normalize + de-duplicate course codes (case/spaces/hyphens-insensitive).
    seen: set[str] = set()
    courses: list[str] = []
    for raw in body.courses:
        code = normalize_course_code(raw)
        if code not in seen:
            seen.add(code)
            courses.append(code)

    warnings: list[str] = []
    if len(courses) != len(body.courses):
        warnings.append("Duplicate courses were removed.")

    # Parse free-text constraints. Never raises; falls back to a warning.
    parsed = await llm.parse_constraints(body.extra_constraints_text or "")
    if parsed.warning:
        warnings.append(parsed.warning)

    req = SolverRequest(
        term=term,
        courses=courses,
        preferences=body.preferences,
        days_off=body.days_off,
        earliest_start_min=body.earliest_start_min,
        latest_end_min=body.latest_end_min,
        allow_online=body.allow_online,
        blocked_times=parsed.blocked_times,
        envelopes=body.envelopes,
    )

    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(_solve_sync, req),
            timeout=SCHEDULER_TIMEOUT_SECONDS,
        )
    except solver.SolverError as e:
        # Unknown course code(s): surface the fuzzy suggestions to the caller.
        raise HTTPException(status_code=422, detail={
            "message": str(e),
            "course": e.course,
            "suggestions": e.suggestions,
        })
    except asyncio.TimeoutError:
        logger.error("solver timed out for term=%s courses=%s", term, courses)
        raise HTTPException(
            status_code=503,
            detail="Schedule generation timed out. Please try again.",
        )
    except Exception:  # noqa: BLE001 — never leak internals to the client
        logger.exception("unexpected solver error for term=%s courses=%s", term, courses)
        raise HTTPException(
            status_code=500,
            detail="Something went wrong while building schedules. Please try again.",
        )

    if not resp.schedules:
        return GenerateResponse(
            status="no_schedule",
            term=term,
            warnings=warnings,
            assumptions=parsed.assumptions,
            cap_hit=resp.cap_hit,
            bottleneck=resp.bottleneck,
        )

    # Explain the schedules (never raises; deterministic templates on failure).
    explanations = await llm.explain_schedules(resp.schedules, body.preferences)

    schedules_out: list[GeneratedSchedule] = []
    for i, s in enumerate(resp.schedules):
        schedules_out.append(GeneratedSchedule(
            sections=s.sections,
            async_online=s.async_online,
            total_score=s.total_score,
            metrics=s.metrics,
            explanation=explanations[i] if i < len(explanations) else "",
        ))

    return GenerateResponse(
        status="ok",
        term=term,
        schedules=schedules_out,
        warnings=warnings,
        assumptions=parsed.assumptions,
        cap_hit=resp.cap_hit,
    )


# ── Envelopes ────────────────────────────────────────────────────────────────

@router.get("/envelopes", response_model=list[EnvelopeOut])
async def list_envelopes():
    """Enrollment envelopes available for the active term.

    Only verified-ok bundles are offered to students; the ``ok_bundles`` count
    here reflects that (a program with zero ok bundles lists zero and is
    effectively unavailable).
    """
    term = await get_active_term()
    envelopes = await database.get_envelopes(term)
    out: list[EnvelopeOut] = []
    for env in envelopes:
        ok_bundles = sum(1 for b in env["bundles"] if b["ok"] == 1)
        courses = sorted({s["course_code"] for b in env["bundles"] for s in b["sections"]})
        out.append(EnvelopeOut(
            slug=env["slug"],
            title=env["display_title"],
            ok_bundles=ok_bundles,
            total_bundles=len(env["bundles"]),
            courses=courses,
            captured_at=env["captured_at"],
        ))
    return out


# ── Status ───────────────────────────────────────────────────────────────────

@router.get("/status", response_model=SchedulerStatusOut)
async def scheduler_status():
    term = await get_active_term()
    stats = await database.get_scheduler_stats(term)
    return SchedulerStatusOut(
        status="ready" if stats["section_count"] > 0 else "loading",
        term=term,
        section_count=stats["section_count"],
        meeting_count=stats["meeting_count"],
        professor_count=stats["professor_count"],
        professors_with_ratings=stats["professors_with_ratings"],
        last_scrape_time=stats["last_scrape_time"],
        last_rmp_refresh_time=stats["last_rmp_refresh_time"],
        llm_configured=bool(os.getenv("OPENAI_API_KEY")),
    )

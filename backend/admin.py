"""Secret per-term refresh admin page and API (no login).

Access model
------------
* ``ADMIN_PATH_SECRET`` env var. When unset or shorter than 24 characters every
  ``/next-term/*`` route 404s (the feature is off).
* The page lives at ``/next-term/<secret>/`` and its API under
  ``/next-term/<secret>/api/...``. The path secret is compared with
  ``hmac.compare_digest``; a wrong secret returns the same 404 as a
  nonexistent route.
* Wrong guesses are rate-limited (20/min/IP, honoring ``TRUST_PROXY``).
* All state-changing endpoints are POST-only; no cookies/sessions/login.

This module never imports the room-finder or MCP code.
"""

from __future__ import annotations

import hmac
import logging
import os
import time
from datetime import date
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.datastructures import MutableHeaders

from . import database, envelopes, jobs, rmp, scheduler_api
from .envelopes import (
    VerifyReport,
    envelope_to_dict,
    envelopes_to_text,
    parse_cart_text,
)

logger = logging.getLogger("freeroomfinder.admin")

router = APIRouter(prefix="/next-term/{secret}", tags=["admin"])

ADMIN_HTML = Path(__file__).resolve().parents[1] / "frontend" / "admin.html"

_SECURITY_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "Cache-Control": "no-store",
}

_CORS_HEADERS = (
    "access-control-allow-origin",
    "access-control-allow-methods",
    "access-control-allow-headers",
    "access-control-allow-credentials",
    "access-control-expose-headers",
    "access-control-max-age",
)


class AdminSecurityMiddleware:
    """Headers + no-CORS for every ``/next-term/*`` response.

    Registered *after* the app's CORS middleware so it runs outermost and can
    strip any ``Access-Control-*`` headers CORS added, guaranteeing the admin
    routes never advertise cross-origin access.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/next-term"):
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for k, v in _SECURITY_HEADERS.items():
                    headers[k] = v
                for k in _CORS_HEADERS:
                    try:
                        del headers[k]
                    except KeyError:
                        pass
            await send(message)

        await self.app(scope, receive, send_wrapper)

# ── Access control ───────────────────────────────────────────────────────────

_RATE_LIMIT = 20
_RATE_WINDOW = 60.0
_guess_rate: dict[str, list[float]] = {}


def admin_secret() -> Optional[str]:
    s = os.getenv("ADMIN_PATH_SECRET", "")
    return s if len(s) >= 24 else None


def admin_enabled() -> bool:
    return admin_secret() is not None


def client_ip(request: Request) -> str:
    if os.getenv("TRUST_PROXY", "").strip().lower() in ("1", "true", "yes", "on"):
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _over_guess_limit(ip: str) -> bool:
    """True when this IP has already used its per-minute wrong-guess budget."""
    now = time.monotonic()
    hits = [t for t in _guess_rate.get(ip, []) if now - t < _RATE_WINDOW]
    if len(hits) >= _RATE_LIMIT:
        _guess_rate[ip] = hits
        return True
    hits.append(now)
    _guess_rate[ip] = hits
    return False


async def require_secret(secret: str, request: Request) -> None:
    """Reject unknown secrets with a 404 identical to a nonexistent route.

    Wrong guesses are rate-limited to 20/min/IP (then 429). The correct secret
    always passes.
    """
    expected = admin_secret()
    if expected is None:
        raise HTTPException(status_code=404, detail="Not Found")
    if not hmac.compare_digest(secret, expected):
        if _over_guess_limit(client_ip(request)):
            raise HTTPException(status_code=429, detail="Too Many Requests")
        raise HTTPException(status_code=404, detail="Not Found")


# ── Request models ───────────────────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    term: str


class EnvelopeTextRequest(BaseModel):
    term: str
    raw_text: str


class EnvelopeSaveRequest(BaseModel):
    term: str
    raw_text: str
    mode: Literal["term", "programs"] = "term"
    slugs: list[str] = []
    titles: dict[str, str] = {}


class PublishRequest(BaseModel):
    term: str
    override: bool = False


# ── Shared helpers ───────────────────────────────────────────────────────────

def _parse(req: EnvelopeTextRequest) -> list[envelopes.Envelope]:
    return parse_cart_text(req.raw_text, req.term, date.today().isoformat())


def _preview_programs(parsed: list[envelopes.Envelope]) -> list[dict]:
    out = []
    for env in parsed:
        bundles = [
            {
                "env_id": b.env_id,
                "courses": sorted({s.course_code for s in b.sections}),
                "section_count": len(b.sections),
            }
            for b in env.bundles
        ]
        out.append({
            "slug": env.slug,
            "title": env.title,
            "display_title": env.display_title,
            "bundle_count": len(env.bundles),
            "courses": sorted({s.course_code for b in env.bundles for s in b.sections}),
            "warnings": env.warnings,
            "bundles": bundles,
        })
    return out


def _report_to_dict(report: VerifyReport) -> dict:
    return {
        "term": report.term,
        "ok_bundles": report.ok_bundles,
        "mismatch_bundles": report.mismatch_bundles,
        "missing_bundles": report.missing_bundles,
        "problems": report.problems,
        "warnings": report.warnings,
        "envelopes": [
            {
                "slug": e.slug,
                "display_title": e.display_title,
                "status": e.status,
                "warnings": e.warnings,
                "bundles": [
                    {
                        "bundle_index": b.bundle_index,
                        "env_id": b.env_id,
                        "status": b.status,
                    }
                    for b in e.bundles
                ],
            }
            for e in report.envelopes
        ],
    }


def _jobs_out(rows: list[dict]) -> list[dict]:
    import json

    out = []
    for r in rows:
        counts = None
        if r.get("counts_json"):
            try:
                counts = json.loads(r["counts_json"])
            except (json.JSONDecodeError, TypeError):
                counts = None
        out.append({
            "id": r["id"],
            "job": r["job"],
            "term": r["term"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "status": r["status"],
            "counts": counts,
            "error": r["error"],
        })
    return out


# ── Page ─────────────────────────────────────────────────────────────────────

@router.get("/", include_in_schema=False)
async def admin_page(secret: str, request: Request):
    await require_secret(secret, request)
    return FileResponse(ADMIN_HTML)


# ── Dashboard ────────────────────────────────────────────────────────────────

@router.get("/api/status")
async def admin_status(
    secret: str,
    request: Request,
    term: Optional[str] = Query(default=None),
):
    await require_secret(secret, request)
    published = await scheduler_api.get_active_term()
    selected = term or published

    terms = await database.get_terms_with_counts()
    coverage = await database.get_rating_coverage()

    return {
        "published_term": published,
        "selected_term": selected,
        "terms": [
            {
                "term": t["term"],
                "section_count": t["section_count"],
                "published": t["term"] == published,
            }
            for t in terms
        ],
        "last_jobs": await database.get_last_successful_jobs(),
        "rating_coverage": coverage,
        "envelope_counts": await database.get_envelope_counts(selected),
        "llm_configured": bool(os.getenv("OPENAI_API_KEY")),
        "llm_model": os.getenv("OPENAI_MODEL") or os.getenv("OPENAI_PARSE_MODEL"),
        "db_file_size": database.db_file_size(),
        "running": {
            "sections": jobs.job_running("sections"),
            "rmp": jobs.job_running("rmp"),
        },
    }


# ── Sections ─────────────────────────────────────────────────────────────────

@router.get("/api/yearterm")
async def yearterm(secret: str, request: Request):
    await require_secret(secret, request)
    return {"terms": await _yearterm_options()}


async def _yearterm_options() -> list[dict]:
    from .scraper import create_client, get_session_id
    from .section_scraper import fetch_yearterm_options

    try:
        async with create_client() as client:
            session_id = await get_session_id(client)
            return await fetch_yearterm_options(client, session_id)
    except Exception:  # noqa: BLE001 — the picker falls back to DB terms
        logger.warning("yearterm fetch failed", exc_info=True)
        return []


@router.post("/api/sections/scrape")
async def scrape(secret: str, request: Request, body: ScrapeRequest):
    await require_secret(secret, request)
    term = body.term.strip()
    if not term:
        raise HTTPException(status_code=422, detail="term is required")
    try:
        return await jobs.start_scrape_job(term)
    except jobs.JobBusyError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/api/sections/refresh")
async def refresh_published(secret: str, request: Request):
    await require_secret(secret, request)
    term = await scheduler_api.get_active_term()
    try:
        return await jobs.start_scrape_job(term)
    except jobs.JobBusyError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/api/progress")
async def progress(secret: str, request: Request):
    await require_secret(secret, request)
    return {
        "sections": jobs.job_progress("sections"),
        "rmp": jobs.job_progress("rmp"),
        "running": {
            "sections": jobs.job_running("sections"),
            "rmp": jobs.job_running("rmp"),
        },
    }


# ── Ratings ──────────────────────────────────────────────────────────────────

@router.post("/api/ratings/refresh")
async def ratings_refresh(secret: str, request: Request):
    await require_secret(secret, request)
    try:
        return await jobs.start_rmp_job(force=False)
    except jobs.JobBusyError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/api/ratings/rematch")
async def ratings_rematch(secret: str, request: Request):
    await require_secret(secret, request)
    try:
        return await jobs.start_rmp_job(force=True)
    except jobs.JobBusyError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/api/ratings/review")
async def ratings_review(secret: str, request: Request):
    await require_secret(secret, request)
    rows = await database.get_review_candidates()
    out = [
        {
            "byu_name": r["byu_name"],
            "rmp_name": r.get("rmp_name"),
            "confidence": r.get("match_confidence"),
            "reason": r.get("match_reason"),
            "num_candidates": r.get("num_candidates", 1),
            "rmp_url": rmp.rmp_url_from_id(r.get("rmp_id")),
        }
        for r in rows
    ]
    return {"professors": out}


async def _stamp_bundle_ok(term: str, report: VerifyReport) -> None:
    """Copy a parsed-verification report's bundle statuses onto the DB rows."""
    db_envelopes = await database.get_envelopes(term)
    by_slug = {e["slug"]: e for e in db_envelopes}
    for ev in report.envelopes:
        db_env = by_slug.get(ev.slug)
        if not db_env:
            continue
        for bv in ev.bundles:
            for b in db_env["bundles"]:
                if b["bundle_index"] == bv.bundle_index:
                    await database.update_bundle_ok(
                        b["id"], 1 if bv.status == "ok" else 0
                    )


async def _verify_current(term: str) -> VerifyReport:
    """Verify the *saved* envelopes for a term, stamping bundle ok flags.

    Admin-saved terms keep their meeting snapshot in ``envelope_sources``, so
    we re-parse the raw text and verify that (meeting drift included). Seeded
    terms (JSON-file-backed, no raw text) fall back to ``verify_envelopes``.
    """
    src = await database.get_envelope_source(term)
    if src and src.get("raw_text"):
        captured = (src.get("saved_at") or date.today().isoformat())[:10]
        parsed = parse_cart_text(src["raw_text"], term, captured)
        report = await envelopes.verify_parsed_envelopes(term, parsed)
        await _stamp_bundle_ok(term, report)
        return report
    return await envelopes.verify_envelopes(term)


# ── Envelopes ────────────────────────────────────────────────────────────────

@router.post("/api/envelopes/preview")
async def envelopes_preview(secret: str, request: Request, body: EnvelopeTextRequest):
    await require_secret(secret, request)
    parsed = _parse(body)
    return {"programs": _preview_programs(parsed)}


@router.post("/api/envelopes/verify")
async def envelopes_verify(secret: str, request: Request, body: EnvelopeTextRequest):
    await require_secret(secret, request)
    parsed = _parse(body)
    report = await envelopes.verify_parsed_envelopes(body.term, parsed)
    return _report_to_dict(report)


@router.post("/api/envelopes/save")
async def envelopes_save(secret: str, request: Request, body: EnvelopeSaveRequest):
    await require_secret(secret, request)
    parsed = _parse(body)
    by_slug = {e.slug: e for e in parsed}

    # Apply edited display titles before saving.
    for slug, title in (body.titles or {}).items():
        t = (title or "").strip()
        if slug in by_slug and t:
            by_slug[slug].display_title = t

    if body.mode == "term":
        await database.delete_envelopes_for_term(body.term)
        selected = list(by_slug.values())
        merged_text = body.raw_text
    else:
        selected = [by_slug[s] for s in body.slugs if s in by_slug]
        replaced_slugs = {e.slug for e in selected}
        existing = await database.get_envelope_source(body.term)
        kept: list[envelopes.Envelope] = []
        if existing and existing.get("raw_text"):
            captured = (existing.get("saved_at") or date.today().isoformat())[:10]
            for e in parse_cart_text(existing["raw_text"], body.term, captured):
                if e.slug not in replaced_slugs:
                    kept.append(e)
        merged_text = envelopes_to_text(kept + selected)

    await envelopes.save_parsed_envelopes(body.term, selected)
    await database.save_envelope_source(body.term, merged_text)

    # Verify (stamps ok/mismatch/missing) and record the run.
    job_id = await database.create_job_run("envelopes_verify", body.term)
    report = await _verify_current(body.term)
    await database.update_job_run(job_id, "ok", {
        "ok_bundles": report.ok_bundles,
        "mismatch_bundles": report.mismatch_bundles,
        "missing_bundles": report.missing_bundles,
        "warnings": len(report.warnings),
    })
    return _report_to_dict(report)


@router.get("/api/envelopes/download")
async def envelopes_download(
    secret: str,
    request: Request,
    term: str = Query(...),
):
    await require_secret(secret, request)
    src = await database.get_envelope_source(term)
    if src and src.get("raw_text"):
        captured = (src.get("saved_at") or date.today().isoformat())[:10]
        parsed = parse_cart_text(src["raw_text"], term, captured)
        # Overlay any admin-edited display titles stored in the DB.
        titles = {
            e["slug"]: e["display_title"]
            for e in await database.get_envelopes(term)
        }
        for env in parsed:
            if env.slug in titles:
                env.display_title = titles[env.slug]
        data = [envelope_to_dict(e) for e in parsed]
    else:
        data = await _envelopes_from_db(term)
    return JSONResponse(
        data,
        headers={
            "Content-Disposition": f'attachment; filename="envelopes-{term}.json"',
        },
    )


async def _envelopes_from_db(term: str) -> list[dict]:
    db_envelopes = await database.get_envelopes(term)
    out = []
    for env in db_envelopes:
        bundles = []
        for b in env["bundles"]:
            sections = [
                {
                    "course_code": s["course_code"],
                    "section_number": s["section_number"],
                    "instructor_snapshot": s["instructor_snapshot"],
                    "mode": s["mode"],
                    "credits": s["credits"],
                    "part_of_term": s["part_of_term"],
                    "note_flag": bool(s["note_flag"]),
                }
                for s in b["sections"]
            ]
            bundles.append({
                "env_id": b["env_id"],
                "capacity_snapshot": b["capacity_snapshot"],
                "seats_snapshot": b["seats_snapshot"],
                "sections": sections,
            })
        out.append({
            "title": env["title"],
            "display_title": env["display_title"],
            "slug": env["slug"],
            "term": term,
            "captured_at": env["captured_at"],
            "bundles": bundles,
        })
    return out


# ── Publish ──────────────────────────────────────────────────────────────────

@router.post("/api/publish")
async def publish(secret: str, request: Request, body: PublishRequest):
    await require_secret(secret, request)
    checklist = await _publish_checklist(body.term)
    if checklist["blockers"] and not body.override:
        return JSONResponse(
            status_code=409,
            content={"published": False, "checklist": checklist},
        )
    await database.set_setting("active_term", body.term)
    return {"published": True, "active_term": body.term, "checklist": checklist}


async def _publish_checklist(term: str) -> dict:
    published = await scheduler_api.get_active_term()
    terms = {t["term"]: t["section_count"] for t in await database.get_terms_with_counts()}

    sections_exist = terms.get(term, 0) > 0

    cur_count = terms.get(published, 0)
    new_count = terms.get(term, 0)
    if cur_count > 0 and new_count > 0:
        within_30 = 0.7 <= (new_count / cur_count) <= 1.3
    else:
        within_30 = True

    report = await _verify_current(term)
    no_hard = not report.has_problems

    cur_cov = await database.get_rating_coverage_for_term(published)
    new_cov = await database.get_rating_coverage_for_term(term)
    cur_pct = (cur_cov["rated"] / cur_cov["total"] * 100) if cur_cov["total"] else 0
    new_pct = (new_cov["rated"] / new_cov["total"] * 100) if new_cov["total"] else 0
    coverage_ok = new_pct >= cur_pct - 20

    no_job = not (jobs.job_running("sections") or jobs.job_running("rmp"))

    checks = {
        "sections_exist": sections_exist,
        "section_count_within_30_percent": within_30,
        "no_hard_envelope_problems": no_hard,
        "ratings_coverage_ok": coverage_ok,
        "no_job_running": no_job,
    }
    blockers = [name for name, ok in checks.items() if not ok]
    return {
        "checks": checks,
        "blockers": blockers,
        "published_term": published,
        "target_term": term,
        "current_section_count": cur_count,
        "target_section_count": new_count,
        "current_coverage_pct": round(cur_pct, 1),
        "target_coverage_pct": round(new_pct, 1),
    }


# ── Job log ──────────────────────────────────────────────────────────────────

@router.get("/api/jobs")
async def job_log(secret: str, request: Request):
    await require_secret(secret, request)
    return {"jobs": _jobs_out(await database.list_job_runs(50))}

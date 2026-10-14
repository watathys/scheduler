"""Background jobs for the schedule builder (section scrape + RMP refresh).

Every job is safe to skip or fail: errors are logged and swallowed, and a
per-job ``asyncio.Lock`` prevents two copies of the same job from running at
once (e.g. a startup scrape racing the periodic loop).

Configurable via env vars (see README):
  DISABLE_BACKGROUND_JOBS       disable all jobs (local development)
  SECTION_SCRAPE_INTERVAL_SECONDS   weekly scrape cadence (default 7d)
  DAILY_SCRAPE_INTERVAL_SECONDS     cadence inside the registration window
  REGISTRATION_WINDOW_START         ISO date (YYYY-MM-DD) — daily scrape start
  REGISTRATION_WINDOW_END           ISO date (YYYY-MM-DD) — daily scrape end
  RMP_REFRESH_INTERVAL_SECONDS      RMP refresh cadence (default 7d)
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date
from typing import Awaitable, Callable

from . import rmp

logger = logging.getLogger("freeroomfinder.jobs")

WEEK_SECONDS = 7 * 24 * 3600
DAY_SECONDS = 24 * 3600

# Prevent two copies of the same job from running at once.
_scrape_lock = asyncio.Lock()
_rmp_lock = asyncio.Lock()

# Admin job registry: running flags + live progress (polled by the admin page).
_running: dict[str, bool] = {"sections": False, "rmp": False}
_progress: dict[str, dict] = {}


class JobBusyError(Exception):
    """Raised when a job of the same kind is already running."""


def job_running(name: str) -> bool:
    return _running.get(name, False)


def job_progress(name: str) -> dict | None:
    return _progress.get(name)


def background_jobs_disabled() -> bool:
    return os.getenv("DISABLE_BACKGROUND_JOBS", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return default


def _env_date(name: str) -> date | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r (expected YYYY-MM-DD)", name, raw)
        return None


def _in_registration_window() -> bool:
    start = _env_date("REGISTRATION_WINDOW_START")
    end = _env_date("REGISTRATION_WINDOW_END")
    if start is None or end is None:
        return False
    return start <= date.today() <= end


def scrape_interval_seconds() -> int:
    """Daily inside the registration window, otherwise weekly."""
    if _in_registration_window():
        return _env_int("DAILY_SCRAPE_INTERVAL_SECONDS", DAY_SECONDS)
    return _env_int("SECTION_SCRAPE_INTERVAL_SECONDS", WEEK_SECONDS)


async def run_section_scrape(term: str, progress: dict | None = None) -> dict:
    """Scrape every section for ``term``. Never raises; returns summary stats."""
    from .section_scraper import scrape_term

    async with _scrape_lock:
        try:
            stats = await scrape_term(term, progress=progress)
            logger.info("section scrape complete for %s: %s", term, stats)
            return stats
        except Exception:
            logger.exception("section scrape failed for term %s", term)
            return {}


async def run_rmp_refresh(force: bool = False, progress: dict | None = None) -> dict:
    """Refresh stale RMP ratings. Never raises; returns a summary dict."""
    async with _rmp_lock:
        try:
            summary = await rmp.refresh_professors(force=force, progress=progress)
            logger.info("RMP refresh complete: %s", summary)
            return summary
        except Exception:
            logger.exception("RMP refresh failed")
            return {}


# ── Admin job runners (record job_runs + expose progress) ────────────────────

async def _scrape_job_body(term: str, job_id: int) -> None:
    from . import database
    _running["sections"] = True
    progress: dict = {}
    _progress["sections"] = progress
    try:
        stats = await run_section_scrape(term, progress=progress)
        await database.update_job_run(job_id, "ok", stats)
    except Exception as e:  # noqa: BLE001 — generic message only
        await database.update_job_run(job_id, "error", None, str(e))
        logger.exception("admin section scrape failed for term %s", term)
    finally:
        _running["sections"] = False
        _progress.pop("sections", None)


async def start_scrape_job(term: str) -> dict:
    """Start a background scrape for ``term``; raises JobBusyError if running."""
    from . import database
    if _running["sections"] or _scrape_lock.locked():
        raise JobBusyError("a section scrape is already running")
    _running["sections"] = True
    job_id = await database.create_job_run("sections", term)
    asyncio.create_task(_scrape_job_body(term, job_id))
    return {"job_id": job_id, "job": "sections", "term": term, "status": "running"}


async def _rmp_job_body(force: bool, job_id: int) -> None:
    from . import database
    _running["rmp"] = True
    progress: dict = {}
    _progress["rmp"] = progress
    try:
        summary = await run_rmp_refresh(force=force, progress=progress)
        await database.update_job_run(job_id, "ok", summary)
    except Exception as e:  # noqa: BLE001 — generic message only
        await database.update_job_run(job_id, "error", None, str(e))
        logger.exception("admin RMP refresh failed")
    finally:
        _running["rmp"] = False
        _progress.pop("rmp", None)


async def start_rmp_job(force: bool) -> dict:
    """Start a background RMP refresh; raises JobBusyError if running."""
    from . import database
    if _running["rmp"] or _rmp_lock.locked():
        raise JobBusyError("an RMP refresh is already running")
    _running["rmp"] = True
    job_id = await database.create_job_run("rmp", None)
    asyncio.create_task(_rmp_job_body(force, job_id))
    return {"job_id": job_id, "job": "rmp", "term": None, "status": "running"}


async def scraper_loop(get_term: Callable[[], Awaitable[str]]) -> None:
    """Periodically re-scrape sections (daily in the registration window)."""
    if background_jobs_disabled():
        logger.info("background section scraper disabled (DISABLE_BACKGROUND_JOBS)")
        return
    while True:
        await asyncio.sleep(scrape_interval_seconds())
        try:
            term = await get_term()
            await run_section_scrape(term)
        except Exception:
            logger.exception("scraper loop iteration failed")


async def rmp_loop() -> None:
    """Periodically refresh RMP ratings for new/stale professors."""
    if background_jobs_disabled():
        logger.info("background RMP refresh disabled (DISABLE_BACKGROUND_JOBS)")
        return
    while True:
        await asyncio.sleep(_env_int("RMP_REFRESH_INTERVAL_SECONDS", WEEK_SECONDS))
        try:
            await run_rmp_refresh()
        except Exception:
            logger.exception("RMP loop iteration failed")

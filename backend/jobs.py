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


async def run_section_scrape(term: str) -> dict:
    """Scrape every section for ``term``. Never raises; returns summary stats."""
    from .section_scraper import scrape_term

    async with _scrape_lock:
        try:
            stats = await scrape_term(term)
            logger.info("section scrape complete for %s: %s", term, stats)
            return stats
        except Exception:
            logger.exception("section scrape failed for term %s", term)
            return {}


async def run_rmp_refresh() -> dict:
    """Refresh stale RMP ratings. Never raises; returns a summary dict."""
    async with _rmp_lock:
        try:
            summary = await rmp.refresh_professors()
            logger.info("RMP refresh complete: %s", summary)
            return summary
        except Exception:
            logger.exception("RMP refresh failed")
            return {}


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

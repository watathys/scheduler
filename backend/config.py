import os
from dataclasses import dataclass, field
from datetime import date


# ── Algorithmic term estimator ───────────────────────────────────────────────
# Used when the scheduler has no scraped section data yet (e.g. the very first
# startup before scraping finishes) to pick a fallback term.
#
# BYU semester approximate start windows (based on historic calendar):
#   Winter  ~ Jan  5        Fall   ~ Sep  2
#   Spring  ~ Apr 27        Summer ~ Jun 22

_APPROX: list[tuple[int, int, str]] = [
    # (month, day, term_suffix) — in ascending calendar order
    (1,  1, "1"),   # Winter  (generous: treat Jan 1 as the floor)
    (4, 27, "3"),   # Spring
    (6, 22, "4"),   # Summer
    (9,  2, "5"),   # Fall
]


def algorithmic_term(today: date | None = None) -> str:
    """Best-guess BYU term code for *today* using fixed approximate start dates."""
    if today is None:
        today = date.today()
    year = today.year
    suffix = "1"
    for month, day, term_suffix in _APPROX:
        if (today.month, today.day) >= (month, day):
            suffix = term_suffix
    return f"{year}{suffix}"


def compute_current_term(today: date | None = None) -> str:
    """Return the active BYU term code.

    Priority order:
      1. YEAR_TERM env var override (useful for local testing)
      2. Algorithmic approximation
    """
    override = os.getenv("YEAR_TERM")
    if override:
        return override
    return algorithmic_term(today)


# ── App settings ─────────────────────────────────────────────────────────────
@dataclass
class Settings:
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8000")))
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "data/freeroomfinder.db"))
    # Populated at startup as a best guess; the scheduler prefers the newest
    # term actually present in the sections table.
    current_term: str = field(default_factory=lambda: compute_current_term())
    # Public class-search backend (no auth). The legacy
    # y.byu.edu/class_schedule CGI was decommissioned (HTTP 404 as of ~Aug 2026).
    byu_url: str = "https://commtech.byu.edu/noauth/classSchedule"
    request_timeout: float = 15.0


settings = Settings()

"""BYU class-schedule client.

Data source: BYU's public class-search backend at
``https://commtech.byu.edu/noauth/classSchedule`` (no auth required).
Uses the ``YYYYT`` term codes (e.g. ``20265`` = Fall 2026).
"""

import logging
import re

import httpx

from .config import settings

logger = logging.getLogger("freeroomfinder")

_SESSION_ID_RE = re.compile(r'_session_id\s*=\s*"([^"]+)"')

# Browser UA: the API sits behind BYU's bot-mitigation (Dynatrace Ruxit);
# the default "python-httpx/..." UA is treated with suspicion.
_CLIENT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    )
}


def _api_url(path: str) -> str:
    return f"{settings.byu_url.rstrip('/')}/{path.lstrip('/')}"


async def get_session_id(client: httpx.AsyncClient) -> str:
    """Fetch the class-search homepage and extract its session token."""
    resp = await client.get(_api_url("index.php"), timeout=settings.request_timeout)
    resp.raise_for_status()
    m = _SESSION_ID_RE.search(resp.text)
    if not m:
        raise RuntimeError("could not find _session_id on BYU class-search page")
    return m.group(1)


async def fetch_sections(
    client: httpx.AsyncClient,
    session_id: str,
    course_id: str,
    year_term: str,
) -> list[dict]:
    """Return the raw section dicts for *course_id* (empty list on failure)."""
    try:
        resp = await client.post(
            _api_url("ajax/getSections.php"),
            data={
                "courseId": course_id,
                "sessionId": session_id,
                "yearterm": year_term,
            },
            timeout=settings.request_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.debug("fetch_sections(%s): request failed: %s", course_id, e)
        return []
    if isinstance(data, dict):
        sections = data.get("sections", [])
        return sections if isinstance(sections, list) else []
    return []


def create_client() -> httpx.AsyncClient:
    """Shared client factory (browser UA so BYU serves the API calls)."""
    return httpx.AsyncClient(headers=_CLIENT_HEADERS)

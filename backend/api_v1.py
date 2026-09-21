"""Versioned public read-only JSON API. All handlers call availability.* (shared core)."""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from . import availability
from .availability import AvailabilityError

router = APIRouter(prefix="/api/v1", tags=["v1"])

# ── 5-minute response cache (protects upstream BYU systems + DB) ──
_TTL = 300.0
_cache: dict[str, tuple[float, dict]] = {}


def _cache_get(key: str) -> dict | None:
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]
    _cache.pop(key, None)
    return None


def _cache_set(key: str, value: dict) -> None:
    if len(_cache) > 500:
        _cache.clear()
    _cache[key] = (time.monotonic(), value)


# ── Basic per-IP rate limiting: 120 GET/min ──
_RATE: dict[str, list[float]] = {}
_LIMIT = 120
_WINDOW = 60.0


def _check_rate(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    hits = [t for t in _RATE.get(ip, []) if now - t < _WINDOW]
    if len(hits) >= _LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded: 120 requests/min.")
    hits.append(now)
    _RATE[ip] = hits


def _err(e: AvailabilityError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(e))


@router.get("/rooms/available")
async def rooms_available(
    request: Request,
    date: Optional[str] = Query(default=None, description="YYYY-MM-DD, default today (America/Denver)"),
    start: str = Query(..., description="HH:MM 24-hour (required)"),
    duration: int = Query(default=60, ge=15, le=960, description="Minutes"),
    group_size: Optional[int] = Query(default=None, ge=1),
    building: Optional[str] = Query(default=None),
    amenities: Optional[str] = Query(default=None, description="Comma-separated; any value matches nothing (no amenity data)"),
):
    _check_rate(request)
    key = f"avail|{date}|{start}|{duration}|{group_size}|{building}|{amenities}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        result = await availability.find_open_rooms(
            date=date,
            start_time=start,
            duration_minutes=duration,
            group_size=group_size,
            building=building,
            amenities=amenities,
        )
    except AvailabilityError as e:
        raise _err(e)
    _cache_set(key, result)
    return result


@router.get("/rooms/{room_id}")
async def room_details(request: Request, room_id: str, date: Optional[str] = None):
    """Details + today's availability. room_id is URL-encoded BUILDING:room."""
    _check_rate(request)
    key = f"room|{room_id}|{date}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        result = await availability.get_room_details(room_id, date)
    except AvailabilityError as e:
        code = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=code, detail=str(e))
    _cache_set(key, result)
    return result


@router.get("/rooms/{room_id}/availability")
async def room_availability(request: Request, room_id: str, date: Optional[str] = None):
    _check_rate(request)
    try:
        detail = await availability.get_room_details(room_id, date)
    except AvailabilityError as e:
        code = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=code, detail=str(e))
    return {
        "id": detail["id"],
        "date": detail["date"],
        "open_windows": detail["open_windows"],
        "booking_url": detail["booking_url"],
        "as_of": detail["as_of"],
        "timezone": detail["timezone"],
    }


@router.get("/buildings")
async def buildings(request: Request):
    _check_rate(request)
    key = "buildings"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    result = {
        "buildings": await availability.list_bookable_buildings(),
        "timezone": availability.TIMEZONE,
    }
    _cache_set(key, result)
    return result


DOCS_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>FreeRoomFinder API v1</title>
<style>body{font-family:system-ui,sans-serif;max-width:800px;margin:2rem auto;padding:0 1rem}code,pre{background:#f4f4f4;padding:.2rem .4rem;border-radius:4px}pre{padding:1rem;overflow:auto}</style>
</head><body>
<h1>FreeRoomFinder API v1 (read-only, public)</h1>
<p>Classroom occupancy inferred from BYU's public class schedule. <b>Not</b> a booking system:
capacity is usually <code>null</code>, <code>amenities</code> is always <code>[]</code>,
<code>booking_url</code> is the generic class-search page, <code>requires_login</code> is false.
All times ISO 8601 in <code>America/Denver</code>. Date defaults to today in Denver.</p>
<h2>Endpoints</h2>
<pre>GET /api/v1/rooms/available?date=YYYY-MM-DD&amp;start=HH:MM&amp;duration=60&amp;group_size=4&amp;building=HBLL&amp;amenities=whiteboard
GET /api/v1/rooms/{id}              (id = BUILDING:room, URL-encoded)
GET /api/v1/rooms/{id}/availability?date=YYYY-MM-DD
GET /api/v1/buildings</pre>
<h2>Examples</h2>
<pre>curl "http://localhost:8080/api/v1/rooms/available?start=15:00&amp;duration=120&amp;building=HBLL"
curl "http://localhost:8080/api/v1/rooms/available?date=2026-09-23&amp;start=09:00&amp;duration=60"
curl "http://localhost:8080/api/v1/buildings"
curl "http://localhost:8080/api/v1/rooms/HBLL:2214"</pre>
<h2>Errors</h2>
<p>Bad params return <code>400 {"detail": "..."}</code>. Rate limit is 120 req/min per IP (429).
Responses cached ~5 minutes server-side. CORS open for GET.</p>
<p>Upstream: BYU public class search (no auth, no credentials collected). Respect BYU IT
appropriate-use policy; this service caches aggressively and never hammers upstream.</p>
</body></html>"""


@router.get("/docs", response_class=HTMLResponse, include_in_schema=False)
async def v1_docs():
    return DOCS_HTML

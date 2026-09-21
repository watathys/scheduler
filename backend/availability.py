"""Shared core: room-availability logic used by BOTH the REST API and MCP tools.

Wraps the existing scheduler/database (class-occupancy data from BYU's public
class-search backend) and normalizes results into the public v1 room objects.

IMPORTANT — data reality (no mocking):
- Source is class-meeting occupancy, NOT a bookable-room system.
- capacity/description are NULL upstream -> returned as None / [].
- amenities are unknown -> always [] ; requesting amenities yields no matches.
- booking_url is the generic public class-search page (no per-room pre-fill
  exists); requires_login is False (class search needs no login, and there is
  no booking flow — user just walks in).
- date maps to its weekday in America/Denver; schedule is recurring weekly
  within the current term, not date-specific events.
"""

from __future__ import annotations

import re
from datetime import date as _date, datetime as _datetime, time as _time, timezone
from zoneinfo import ZoneInfo

import aiosqlite

from .buildings import ALL_BUILDINGS, get_display_name
from .config import settings
from .database import get_rooms_for_building, get_schedules_for_rooms
from .discovery import day_matches
from .scheduler import compute_free_windows

TIMEZONE = "America/Denver"
_DENVER = ZoneInfo(TIMEZONE)
_BOOKING_URL = "https://commtech.byu.edu/noauth/classSchedule/"

_START_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_SUNDAY = "Sunday"


class AvailabilityError(ValueError):
    """Raised for bad user params; callers map to HTTP 400 / MCP errors."""


def parse_date(date_str: str | None) -> _date:
    """YYYY-MM-DD, or today in America/Denver when None/empty."""
    if not date_str:
        return _datetime.now(_DENVER).date()
    try:
        return _date.fromisoformat(date_str)
    except ValueError:
        raise AvailabilityError(f"Invalid date '{date_str}': expected YYYY-MM-DD.")


def parse_start(start: str) -> _time:
    if not start or not _START_RE.match(start):
        raise AvailabilityError(f"Invalid start '{start}': expected HH:MM 24-hour.")
    h, m = map(int, start.split(":"))
    return _time(h, m)


def parse_duration(duration: int | str | None) -> int:
    try:
        d = int(duration)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise AvailabilityError(f"Invalid duration '{duration}': expected minutes 15-720.")
    if d < 15 or d > 960:
        raise AvailabilityError(f"Invalid duration '{duration}': expected minutes 15-960.")
    return d


def _add_minutes(t: _time, mins: int) -> _time:
    total = t.hour * 60 + t.minute + mins
    if total > 24 * 60:
        raise AvailabilityError("Requested window extends past midnight.")
    if total == 24 * 60:
        return _time(23, 59)  # clamp; end exclusive handled by caller
    return _time(total // 60, total % 60)


def _fmt(t: _time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def _iso(date_obj: _date, t: _time) -> str:
    return _datetime.combine(date_obj, t).replace(tzinfo=_DENVER).isoformat()


def make_room_id(building: str, room_number: str) -> str:
    return f"{building}:{room_number}"


def split_room_id(room_id: str) -> tuple[str, str]:
    if ":" not in room_id:
        raise AvailabilityError(
            f"Invalid room id '{room_id}': expected BUILDING:room (e.g. HBLL:2214)."
        )
    building, room = room_id.split(":", 1)
    building = building.strip().upper()
    room = room.strip()
    if building not in ALL_BUILDINGS or not room:
        raise AvailabilityError(f"Unknown room id '{room_id}'.")
    return building, room


def get_booking_link(room_id: str, date_str: str | None = None) -> str:
    """Official page where the user looks up / books. No per-room pre-fill exists."""
    return _BOOKING_URL


async def get_as_of(buildings: list[str] | None = None) -> str:
    """When the underlying schedule data was last fetched (ISO UTC)."""
    try:
        async with aiosqlite.connect(settings.db_path) as db:
            if buildings:
                ph = ",".join("?" for _ in buildings)
                q = (
                    "SELECT MAX(fetched_at) FROM schedules "
                    f"WHERE building IN ({ph}) AND year_term = ?"
                )
                params: list = [*buildings, settings.current_term]
            else:
                q = "SELECT MAX(fetched_at) FROM schedules WHERE year_term = ?"
                params = [settings.current_term]
            async with db.execute(q, params) as cur:
                row = await cur.fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return _datetime.now(timezone.utc).isoformat()


async def _cached_buildings() -> list[str]:
    """Building codes that actually have rooms in the DB (bookable-searchable)."""
    try:
        async with aiosqlite.connect(settings.db_path) as db:
            async with db.execute("SELECT DISTINCT building FROM rooms ORDER BY building") as cur:
                rows = await cur.fetchall()
        codes = [r[0] for r in rows if r[0] in ALL_BUILDINGS]
        if codes:
            return codes
    except Exception:
        pass
    return []


async def list_bookable_buildings() -> list[dict]:
    """All known buildings + whether they have cached rooms (for mapping names)."""
    cached = set(await _cached_buildings())
    return [
        {
            "code": code,
            "name": get_display_name(code),
            "has_cached_rooms": code in cached,
        }
        for code in ALL_BUILDINGS
    ]


async def find_open_rooms(
    date: str | None = None,
    start_time: str = "",
    duration_minutes: int | str = 60,
    group_size: int | str | None = None,
    building: str | None = None,
    amenities: list[str] | str | None = None,
) -> dict:
    """Single shared core. Both REST and MCP call this.

    - date: YYYY-MM-DD, default today in America/Denver.
    - start_time: HH:MM required. duration_minutes: 15-720, default 60.
    - group_size: rooms have no capacity data; unknown capacity is treated as
      a potential match (never filtered out).
    - amenities: no amenity data exists; any non-empty request matches nothing.
    """
    if not start_time:
        raise AvailabilityError("Missing required param: start (HH:MM).")
    date_obj = parse_date(date)
    start_t = parse_start(start_time)
    duration = parse_duration(duration_minutes)

    total_end = start_t.hour * 60 + start_t.minute + duration
    if total_end > 24 * 60:
        raise AvailabilityError("Requested window extends past midnight.")
    end_t = _time(total_end // 60 % 24, total_end % 60)
    # Handle exact-midnight end
    end_str = "24:00" if total_end == 24 * 60 else _fmt(end_t)
    start_str = _fmt(start_t)
    # compute_free_windows needs HH:MM; clamp 24:00 -> 23:59 for computation,
    # then fix display below (midnight end == full coverage to 24:00).
    comp_end = "23:59" if end_str == "24:00" else end_str
    weekday = date_obj.strftime("%A")

    # Normalize filters
    buildings: list[str]
    if building:
        b = building.strip().upper()
        if b not in ALL_BUILDINGS:
            raise AvailabilityError(f"Unknown building '{building}'. See /api/v1/buildings.")
        buildings = [b]
    else:
        cached = await _cached_buildings()
        buildings = cached  # empty -> no results (nothing crawled yet)

    amen_list: list[str] = []
    if amenities:
        if isinstance(amenities, str):
            amen_list = [a.strip().lower() for a in amenities.split(",") if a.strip()]
        else:
            amen_list = [str(a).strip().lower() for a in amenities if str(a).strip()]
    if amen_list:
        # No amenity data upstream: honest empty result, not mocked.
        return {
            "rooms": [],
            "as_of": await get_as_of(buildings or None),
            "timezone": TIMEZONE,
            "date": date_obj.isoformat(),
            "weekday": weekday,
            "note": "Amenity filtering is not supported: BYU class-schedule data "
            "has no amenity info, so any amenities filter matches nothing.",
        }

    group_n: int | None = None
    if group_size is not None and str(group_size).strip() != "":
        try:
            group_n = int(group_size)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise AvailabilityError(f"Invalid group_size '{group_size}': expected integer.")
        if group_n < 1:
            raise AvailabilityError(f"Invalid group_size '{group_size}': expected >= 1.")

    rooms_out: list[dict] = []
    if weekday != _SUNDAY and buildings:
        for b in buildings:
            rooms = await get_rooms_for_building(b)
            if not rooms:
                continue
            pairs = [(r["building"], r["room_number"]) for r in rooms]
            schedules = await get_schedules_for_rooms(pairs, settings.current_term)
            booked: dict[tuple[str, str], list[tuple[str, str]]] = {}
            for s in schedules:
                if day_matches(s["days"], weekday):
                    booked.setdefault(
                        (s["building"], s["room_number"]), []
                    ).append((s["start_time"], s["end_time"]))
            for r in rooms:
                cap = r.get("capacity")
                if group_n is not None and cap is not None and cap < group_n:
                    continue
                # Unknown capacity (None) always kept as potential match.
                windows = compute_free_windows(
                    booked.get((r["building"], r["room_number"]), []),
                    start_str,
                    comp_end,
                )
                if not windows:
                    continue
                open_windows = []
                for w in windows:
                    ws = w.start_time
                    we = end_str if (end_str == "24:00" and w.end_time == "23:59") else w.end_time
                    sh, sm = map(int, ws.split(":"))
                    start_iso = _iso(date_obj, _time(sh, sm))
                    if we == "24:00":
                        # Midnight end: next-day 00:00 in Denver
                        from datetime import timedelta as _td

                        mid = (_datetime.combine(date_obj, _time(0, 0)) + _td(days=1)).replace(
                            tzinfo=_DENVER
                        )
                        end_iso = mid.isoformat()
                    else:
                        eh, em = map(int, we.split(":"))
                        end_iso = _iso(date_obj, _time(eh, em))
                    open_windows.append({"start": start_iso, "end": end_iso})
                rid = make_room_id(r["building"], r["room_number"])
                rooms_out.append({
                    "id": rid,
                    "name": f"{r['building']} {r['room_number']}",
                    "building": get_display_name(r["building"]),
                    "building_code": r["building"],
                    "capacity": cap,
                    "amenities": [],
                    "open_windows": open_windows,
                    "booking_url": get_booking_link(rid, date_obj.isoformat()),
                    "requires_login": False,
                })

    # Sort: fully-covering first, then by room name
    def _covers_full(rm: dict) -> bool:
        if len(rm["open_windows"]) != 1:
            return False
        w = rm["open_windows"][0]
        return w["start"] == _iso(date_obj, start_t) and (
            w["end"] == (_iso(date_obj, end_t) if end_str != "24:00" else w["end"])
            or end_str == "24:00"
        )

    rooms_out.sort(key=lambda r: (not _covers_full(r), r["name"]))
    return {
        "rooms": rooms_out,
        "as_of": await get_as_of(buildings or None),
        "timezone": TIMEZONE,
        "date": date_obj.isoformat(),
        "weekday": weekday,
    }


async def get_room_details(room_id: str, date: str | None = None) -> dict:
    """Details + availability for one room. date defaults to today (Denver)."""
    building, room_number = split_room_id(room_id)
    date_obj = parse_date(date)
    # Full-day availability 07:00-22:00 (site hours) for the details view
    full = await find_open_rooms(
        date=date_obj.isoformat(),
        start_time="07:00",
        duration_minutes=900,  # 07:00 -> 22:00
        building=building,
    )
    match = next((r for r in full["rooms"] if r["id"] == make_room_id(building, room_number)), None)
    rooms = await get_rooms_for_building(building)
    meta = next((r for r in rooms if r["room_number"] == room_number), None)
    if meta is None:
        raise AvailabilityError(f"Room '{room_id}' not found (building not yet cached?).")
    return {
        "id": make_room_id(building, room_number),
        "name": f"{building} {room_number}",
        "building": get_display_name(building),
        "building_code": building,
        "capacity": meta.get("capacity"),
        "amenities": [],
        "open_windows": match["open_windows"] if match else [],
        "booking_url": get_booking_link(room_id, date_obj.isoformat()),
        "requires_login": False,
        "as_of": full["as_of"],
        "timezone": TIMEZONE,
        "date": date_obj.isoformat(),
        "weekday": date_obj.strftime("%A"),
    }

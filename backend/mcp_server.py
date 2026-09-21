"""Remote MCP server (Streamable HTTP) — all tools call availability.* (shared core).

No auth for v1 (read-only public data). Mounted at /mcp in main.py.
Compatible with MCP Python SDK v2 (MCPServer) and v1 (FastMCP).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Optional

try:  # SDK v2 (current): FastMCP renamed to MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # SDK v1 fallback
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore

try:
    from mcp.types import ToolAnnotations
except ImportError:  # very old SDKs
    ToolAnnotations = None  # type: ignore

from . import availability

_READONLY = ToolAnnotations(readOnlyHint=True) if ToolAnnotations else None

mcp = _Server(
    name="freeroomfinder",
    title="BYU Free Room Finder",
    description=(
        "Finds BYU classrooms that are unoccupied (no class meeting) for a "
        "given date/time window. Data comes from BYU's public class schedule, "
        "not a booking system."
    ),
    instructions=(
        "All times are America/Denver (Mountain Time). "
        "Building codes like HBLL = Harold B. Lee Library; use list_buildings "
        "to map names like 'the library' to codes. "
        "Rooms are walk-in study/meeting spaces inferred from class times; "
        "booking must be completed by the user on the official BYU site with "
        "their own BYU login — never ask for or handle credentials."
    ),
)


def _summary(result: dict) -> str:
    rooms = result.get("rooms", [])
    if not rooms:
        note = result.get("note", "")
        base = (
            f"No open rooms found for {result.get('date')} "
            f"({result.get('weekday')}). Data as of {result.get('as_of')} "
            f"({result.get('timezone')})."
        )
        return f"{base} {note}" if note else base
    lines = [
        f"{len(rooms)} open room(s) for {result.get('date')} "
        f"({result.get('weekday')}), as of {result.get('as_of')} "
        f"({result.get('timezone')}):"
    ]
    for r in rooms[:10]:
        wins = "; ".join(f"{w['start']} to {w['end']}" for w in r["open_windows"])
        lines.append(f"- {r['name']} ({r['building']}): {wins}")
    if len(rooms) > 10:
        lines.append(f"... and {len(rooms) - 10} more.")
    return "\n".join(lines)


def _tool_kwargs():
    return {"annotations": _READONLY} if _READONLY is not None else {}


@mcp.tool(
    name="find_open_rooms",
    description=(
        "Find BYU classrooms with no class meeting during a date/time window. "
        "Use for prompts like 'room for 4 in the library tomorrow at 3pm for 2 hours'. "
        "Times are America/Denver (Mountain Time); date defaults to today in Denver. "
        "date is YYYY-MM-DD and maps to its weekday (schedule is recurring weekly). "
        "start_time is HH:MM 24-hour and REQUIRED; duration_minutes 15-720. "
        "building is an optional code (e.g. HBLL); omit to search all cached buildings. "
        "group_size is advisory only — capacity data does not exist upstream so it never "
        "excludes rooms. amenities always match nothing (no amenity data exists). "
        "Returns rooms with id, name, building, building_code, capacity (usually null), "
        "amenities (always []), open_windows (ISO datetimes), booking_url (generic BYU "
        "class-search page — no per-room booking; user walks in or checks the site "
        "themselves with their BYU login), requires_login=false, plus as_of/timezone. "
        "Never collect or proxy BYU credentials."
    ),
    **_tool_kwargs(),
)
async def find_open_rooms(
    start_time: str,
    date: Optional[str] = None,
    duration_minutes: int = 60,
    group_size: Optional[int] = None,
    building: Optional[str] = None,
    amenities: Optional[str] = None,
) -> str:
    """MCP wrapper — delegates to the shared core."""
    try:
        result = await availability.find_open_rooms(
            date=date,
            start_time=start_time,
            duration_minutes=duration_minutes,
            group_size=group_size,
            building=building,
            amenities=amenities,
        )
    except availability.AvailabilityError as e:
        return json.dumps({"error": str(e)})
    return json.dumps({"result": result, "summary": _summary(result)}, indent=2)


@mcp.tool(
    name="get_room_details",
    description=(
        "Details and availability for one room by id (BUILDING:room, e.g. HBLL:2214). "
        "Times America/Denver; date YYYY-MM-DD defaults to today. Same walk-in / "
        "no-booking caveats as find_open_rooms; user finishes on the official BYU site."
    ),
    **_tool_kwargs(),
)
async def get_room_details(room_id: str, date: Optional[str] = None) -> str:
    try:
        detail = await availability.get_room_details(room_id, date)
    except availability.AvailabilityError as e:
        return json.dumps({"error": str(e)})
    return json.dumps({"result": detail, "summary": _summary({
        "rooms": [{"name": detail["name"], "building": detail["building"],
                   "open_windows": detail["open_windows"]}],
        "date": detail["date"], "weekday": detail["weekday"],
        "as_of": detail["as_of"], "timezone": detail["timezone"],
    }) if detail["open_windows"] else {"rooms": [], "date": detail["date"],
        "weekday": detail["weekday"], "as_of": detail["as_of"],
        "timezone": detail["timezone"]}}, indent=2)


@mcp.tool(
    name="list_buildings",
    description=(
        "List BYU building codes and names with bookable-searchable rooms, so the model "
        "can map phrases like 'the library' to HBLL (Harold B. Lee Library). "
        "has_cached_rooms=false means no schedule cached yet (searches return nothing)."
    ),
    **_tool_kwargs(),
)
async def list_buildings() -> str:
    buildings = await availability.list_bookable_buildings()
    return json.dumps({"buildings": buildings, "timezone": availability.TIMEZONE}, indent=2)


@mcp.tool(
    name="get_booking_link",
    description=(
        "Return the official BYU page for looking up a room/date/time. There is no "
        "per-room online booking — the link opens BYU's public class search and the "
        "user completes anything there themselves with their BYU login."
    ),
    **_tool_kwargs(),
)
async def get_booking_link(
    room_id: str, date: Optional[str] = None, start_time: Optional[str] = None
) -> str:
    try:
        if room_id:
            availability.split_room_id(room_id)
        if date:
            availability.parse_date(date)
        if start_time:
            availability.parse_start(start_time)
    except availability.AvailabilityError as e:
        return json.dumps({"error": str(e)})
    return json.dumps({
        "booking_url": availability.get_booking_link(room_id, date),
        "requires_login": False,
        "note": "Walk-in study space inferred from class times; confirm on the official site.",
    }, indent=2)


def get_mcp_app():
    """Starlette sub-app serving Streamable HTTP at its root (mount at /mcp)."""
    try:
        return mcp.streamable_http_app(streamable_http_path="/")
    except TypeError:
        return mcp.streamable_http_app()


@asynccontextmanager
async def mcp_lifespan(app):
    """Run the MCP session manager alongside the main lifespan (stateful mode)."""
    mgr = getattr(mcp, "session_manager", None)
    run = getattr(mgr, "run", None) if mgr else None
    if run is None:
        yield
        return
    async with run():
        yield

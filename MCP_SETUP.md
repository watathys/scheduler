# BYU Free Room Finder — MCP Setup (share with a friend)

Connect Claude to live BYU classroom availability. Read-only: Claude can look up open rooms, never books anything or touches your BYU login.

## What you need
- A Claude account (Pro/Max, or Team/Enterprise with Connectors enabled)
- This URL: `https://freeroomfinder-production.up.railway.app/mcp`
- No API key, no login, nothing to install

## Add it to Claude (takes ~1 minute)
1. Go to **Claude → Settings → Connectors → Add custom connector**
2. Paste exactly: `https://freeroomfinder-production.up.railway.app/mcp`
3. Name it `BYU Rooms` (or anything you like) → **Add / Connect**
4. Open a **new chat**. If Claude asks for permission to use the connector on first use, click **Allow**.

That's it. If you don't see "Connectors" in Settings, your plan/workspace doesn't allow custom connectors — try Claude Code instead (below).

## Try these 3 prompts
1. `Find a room for 4 people in the library tomorrow at 3pm for 2 hours`
2. `What's open in the HBLL this Saturday morning for an hour?`
3. `Get details and today's availability for HBLL:2214, and give me the official link`

## What Claude can do
- `find_open_rooms` — open classrooms for a date/time/duration, filterable by building (e.g. HBLL). All times are Mountain Time (America/Denver).
- `get_room_details` — one room's availability for a day.
- `list_buildings` — maps names ("the library") to codes ("HBLL").
- `get_booking_link` — official BYU class-search page. You finish there yourself with your own BYU login if needed.

Heads-up: these are walk-in study spaces inferred from the class schedule (no class meeting = probably free). Capacity is usually unknown and there are no amenity filters — Claude will tell you when a filter can't be applied.

## Troubleshooting
- **"Couldn't connect" / 404**: make sure the URL ends in `/mcp` with no trailing spaces. Retry in a new chat.
- **No results**: the building may not be cached yet, or it's Sunday (no class data). Try another building or day.
- **Stale times**: every answer includes `as_of` — data refreshes from BYU on a ~5-minute cache.
- **Still stuck**: open `https://freeroomfinder-production.up.railway.app/api/v1/docs` in a browser — if that loads, the server is up and it's a connector setting on your account.

## Alternatives (optional)
- **Claude Code**: `claude mcp add --transport http byu-rooms https://freeroomfinder-production.up.railway.app/mcp`
- **Test with MCP Inspector**: `npx @modelcontextprotocol/inspector`, transport Streamable HTTP, URL above.

## Privacy
Read-only and public. It never asks for, stores, or proxies BYU credentials. Booking (if ever needed) happens by you on BYU's official site.

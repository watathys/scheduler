# FreeRoomFinder – BYU

Find unoccupied classrooms at BYU for any day and time window. Great for grabbing a quiet study space or an open room for a group meeting.

**Live at:** [freeroomfinder-production.up.railway.app](https://freeroomfinder-production.up.railway.app)

---

## What it does

1. You pick a **building**, a **day**, and a **time range** (or use the "Right Now" quick-select)
2. The app searches BYU's class schedule and returns every room that has free time during that window
3. Rooms are sorted: fully free ones first, then partially free ones with their available sub-windows shown
4. Click any room number to open its full schedule on BYU's scheduling site

---

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | Vanilla HTML / CSS / JS (no framework) |
| Backend | Python 3.12 · FastAPI · Uvicorn |
| Database | SQLite via aiosqlite (persisted on a Railway volume) |
| Scraping | httpx · BeautifulSoup4 · lxml |
| Hosting | [Railway](https://railway.app) |

---

## Project structure

```
freeroomfinder/
├── frontend/
│   ├── index.html          # Single-page UI
│   ├── app.js              # All frontend logic
│   └── style.css           # Styles (dark mode, responsive)
├── backend/
│   ├── main.py             # FastAPI app, routes, lifespan tasks
│   ├── config.py           # Settings, term-code computation
│   ├── calendar_scraper.py # Scrapes academiccalendar.byu.edu for semester dates
│   ├── scraper.py          # Scrapes BYU's room schedule pages
│   ├── discovery.py        # Crawls all rooms in a building concurrently
│   ├── scheduler.py        # Free-window computation logic
│   ├── database.py         # SQLite schema + all DB queries
│   ├── buildings.py        # Master list of BYU building codes + display names
│   └── models.py           # Pydantic request/response models
├── requirements.txt
└── railway.toml
```

---

## Running locally

**Prerequisites:** Python 3.12+

```powershell
# 1. Create and activate a virtual environment (first time only)
python -m venv .venv
.venv\Scripts\Activate.ps1

# 2. Install dependencies (first time only)
pip install -r requirements.txt

# 3. Start the dev server
.venv\Scripts\uvicorn backend.main:app --reload --port 8080
```

Open [http://localhost:8080](http://localhost:8080).

`--reload` watches for Python file changes and restarts automatically. Frontend changes (HTML/CSS/JS) take effect on browser refresh.

> **Note:** Port 8000 may be blocked on some Windows machines. Use `--port 8080` or any other free port.

---

## How the cache works

BYU's public class schedule doesn't expose a room-based API, so room schedules are collected on demand (courses using a building are searched, then their meeting times are grouped by room) and stored in SQLite.

- **First search of a building:** triggers a full crawl of every course scheduled in that building. Takes a few seconds. A live progress bar is shown.
- **Subsequent searches:** instant — data is served from the local DB cache.
- **Cache is shared:** if one user crawls a building, everyone benefits immediately.

The `cached` / `not cached` badge next to each building name shows its status at a glance.

---

## Automatic semester management

No manual maintenance is required between semesters. The app is fully self-managing:

### Term detection

On startup, the app scrapes [academiccalendar.byu.edu](https://academiccalendar.byu.edu) to get the exact start date of every upcoming semester (current year + 2 years ahead). This data is stored in the DB and refreshed weekly. The current BYU term code (`YYYYT`) is computed from today's date against that schedule.

If BYU's calendar site is unreachable, the app falls back to an algorithmic estimate based on approximate semester windows.

### Automatic cache wipe

The day before each new semester starts, the app clears all cached room and schedule data so users always search the current semester's schedule. A background task checks once per day — no redeploy or manual action needed.

### Term code format

BYU uses `YYYYT` where `T` is:

| Suffix | Semester | Approx. dates |
|---|---|---|
| `1` | Winter | Jan – Apr |
| `3` | Spring | Apr – Jun |
| `4` | Summer | Jun – Aug |
| `5` | Fall | Sep – Dec |

Example: `20261` = Winter 2026.

---

## Environment variables (Railway)

| Variable | Required | Description |
|---|---|---|
| `PORT` | Auto-set by Railway | Port the server listens on |
| `DB_PATH` | Yes | Path to SQLite file on the mounted volume (e.g. `/data/freeroomfinder.db`) |
| `YEAR_TERM` | No | Override the auto-detected term code (useful for local testing only) |
| `DISCOVERY_SEMAPHORE` | No | Max concurrent room-scrape requests (default: `15`) |
| `CRAWL_DELAY` | No | Seconds between requests during discovery (default: `0.5`) |

### Schedule-builder env vars (Step 5)

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | No | Enables LLM constraint parsing + schedule explanations (falls back to deterministic templates when unset) |
| `OPENAI_MODEL` | No | LLM model name (e.g. `gpt-5.6-terra`) |
| `DISABLE_BACKGROUND_JOBS` | No | `1` disables the background section-scrape / RMP-refresh loops (local dev) |
| `SECTION_SCRAPE_INTERVAL_SECONDS` | No | Weekly re-scrape cadence (default `604800` = 7 days) |
| `RMP_REFRESH_INTERVAL_SECONDS` | No | RMP refresh cadence (default `604800` = 7 days) |
| `REGISTRATION_WINDOW_START` | No | `YYYY-MM-DD` — start of the daily-scrape window around registration |
| `REGISTRATION_WINDOW_END` | No | `YYYY-MM-DD` — end of the daily-scrape window |
| `DAILY_SCRAPE_INTERVAL_SECONDS` | No | Scrape cadence inside the registration window (default `86400` = daily) |
| `SCHEDULER_TIMEOUT_SECONDS` | No | Per-request ceiling on the solver (default `20`) |
| `GENERATE_RATE_LIMIT` | No | `/schedules/generate` requests/min/IP (default `10`) |

> `YEAR_TERM` does not need to be set in production. The app detects the current term automatically.

A local `.env` file is read at startup (`python-dotenv`); copy any of the above into it. `.env` is git-ignored.

---

## Deployment (Railway)

The app is configured for Railway via `railway.toml`. It uses:
- A **GitHub-connected service** for the app code (auto-deploys on push to `main`)
- A **volume** mounted at the `DB_PATH` location for SQLite persistence

To deploy changes:
```bash
git add .
git commit -m "your message"
git push
```

Railway picks up the push and redeploys automatically.

---

## Public API v1 (read-only JSON)

One shared core (`backend/availability.py::find_open_rooms`) powers both the
REST API and the MCP tools, so they always agree. Human docs page:
`GET /api/v1/docs`.

```bash
curl "http://localhost:8080/api/v1/rooms/available?start=15:00&duration=120&building=HBLL"
curl "http://localhost:8080/api/v1/rooms/available?date=2026-09-23&start=09:00&duration=60"
curl "http://localhost:8080/api/v1/buildings"
curl "http://localhost:8080/api/v1/rooms/HBLL:2214"   # id = BUILDING:room, URL-encoded
```

- Params: `date` YYYY-MM-DD (default today, America/Denver) → mapped to its
  weekday; `start` HH:MM required; `duration` 15–960 min; `group_size` advisory
  only (capacity data doesn't exist, never excludes); `building` code;
  `amenities` comma-separated — always matches nothing (no amenity data).
- Every response includes `as_of` + `timezone: America/Denver`; times ISO 8601.
- Bad params → `400 {"detail": "..."}`. Rate limit 120 req/min/IP (429).
  Server-side response cache ~5 min. CORS open for GET.
- Honest nulls: `capacity` usually null, `amenities` always `[]`,
  `booking_url` = generic BYU class-search page, `requires_login: false`.
  Rooms are walk-in spaces inferred from class times, not bookable reservations.

## Schedule-builder API (`/api/v1/scheduler`)

The AI schedule builder adds a student-facing API alongside the room finder.
It serves the **next** term (the newest term present in the `sections` table),
not a date-derived default. On a cold start with no sections, `/status` reports
`"loading"` and `/schedules/generate` returns a friendly `503` while a
background scrape populates data.

```bash
# Autocomplete over course codes + titles (prefix first, then fuzzy)
curl "http://localhost:8080/api/v1/scheduler/courses/search?q=mktg&limit=10"

# Generate up to 3 conflict-free schedules
curl -X POST "http://localhost:8080/api/v1/scheduler/schedules/generate" \
     -H "Content-Type: application/json" \
     -d '{
           "courses": ["MKTG 201", "STRAT 305", "FIN 201"],
           "preferences": ["easy", "days_off", "no_gaps"],
           "days_off": ["Friday"],
           "earliest_start": "08:00",
           "latest_end": "22:00",
           "allow_online": true,
           "extra_constraints_text": "no classes Tuesday before 10am"
         }'

# Data freshness + term + whether the LLM key is configured
curl "http://localhost:8080/api/v1/scheduler/status"
```

- `GET /courses/search?q=…&limit=10` — case-insensitive, ignores spaces/hyphens;
  prefix matches ranked first, then fuzzy; empty list for queries `< 2` chars.
  Returns `code`, `title`, `credits`, `section_count`.
- `POST /schedules/generate` — body: `courses` (1–8), `preferences` (ordered
  list of exactly 3 from `easy`/`days_off`/`no_gaps`), `days_off` (default
  `["Friday"]`), `earliest_start`/`latest_end` (`HH:MM`), `allow_online`,
  `extra_constraints_text` (≤ 500 chars). Returns the active term, up to 3
  schedules (sections with meetings, instructor names, ease score + confidence,
  async-online list, metrics, score, explanation), parser `warnings` and
  `assumptions`, and `cap_hit`.
- Errors: unknown course codes → `422` with fuzzy `suggestions`; no feasible
  schedule → `200` with `"status": "no_schedule"` and a structured
  `bottleneck` diagnosis; bad input → `422`; data still loading → `503`.
- Rate limit: `10` `/schedules/generate` requests/min/IP (`429`).

Run the script `python scripts/try_api.py` for these examples, or
`python scripts/try_api.py --run` to hit a live server.

## Remote MCP server (Claude custom connector)

- URL after deploy: `https://<your-railway-domain>/mcp` (Streamable HTTP,
  official `mcp` SDK v2, no auth — read-only public data).
- Tools (all read-only): `find_open_rooms`, `get_room_details`,
  `list_buildings`, `get_booking_link`. Verify locally:
  `npx @modelcontextprotocol/inspector` → transport Streamable HTTP →
  URL `http://localhost:8080/mcp`.
- Claude setup: Settings → Connectors → Add custom connector → paste the
  `/mcp` URL.
- Example prompts: "Find a room for 4 people in the library tomorrow at 3pm
  for 2 hours" · "What's open in the HBLL this Saturday morning?" ·
  "Get details and today's availability for HBLL:2214."

## Upstream respect / BYU terms note

Source is BYU's public `noauth` class search (no login, no credentials ever
collected/proxied/stored). We cache DB-side indefinitely + 5-min API cache,
limit discovery concurrency (15, 0.5s delay), rate-limit callers, and send a
normal browser UA. BYU's IT appropriate-use policy has no explicit scraping
license — `noauth` implies public, but if OIT objects, throttle further or
remove the building. No commercial use; site use is incidental/personal.

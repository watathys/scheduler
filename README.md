# BYU Schedule Builder

Build conflict-free BYU class schedules in seconds. Tell the app your courses,
your preferences, and any days you want off — it returns up to 3 ranked
schedules, each with an ease score (and a professor-by-professor breakdown) and
a plain-English explanation.

**Live at:** [freeroomfinder-production.up.railway.app](https://freeroomfinder-production.up.railway.app)

---

## What it does

1. **Courses** — search and add the classes you want (autocomplete on course
   code or title).
2. **Preferences** — rank what matters to you: *easy professors* vs
   *no time gaps*.
3. **Questions** — do you want any days off? How early/late are you willing to
   start/end? Online classes okay? Any extra constraints in plain English?
4. **Build** — submit and get up to 3 schedules. Click a schedule's **Ease**
   score to see why it scored the way it did, listing every professor.

If a schedule is impossible, the app tells you exactly why (e.g.
"`ECON 110` is impossible because every section meets on Friday, which you
wanted off").

---

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | Vanilla HTML / CSS / JS (no framework) |
| Backend | Python 3.12 · FastAPI · Uvicorn |
| Database | SQLite via aiosqlite |
| Scraping | httpx · BeautifulSoup4 · lxml |
| Course search | rapidfuzz (prefix + fuzzy matching) |
| AI parsing | OpenAI (optional; deterministic fallback when no key is set) |
| Hosting | [Railway](https://railway.app) |

---

## Project structure

```
freeroomfinder/
├── frontend/
│   ├── scheduler.html     # Single-page UI
│   ├── scheduler.js       # All frontend logic (3-step wizard)
│   ├── scheduler.css      # Styles
│   └── favicon.svg
├── backend/
│   ├── main.py            # FastAPI app, lifespan, static serving
│   ├── config.py          # Settings, term-code computation
│   ├── models.py          # Pydantic request/response models
│   ├── scheduler_api.py   # /api/v1/scheduler/* routes
│   ├── solver.py          # Constraint solver + scoring + bottleneck diagnosis
│   ├── llm.py             # Constraint parsing + schedule explanations
│   ├── matching.py        # Course-code normalization + dedup
│   ├── scraper.py         # BYU class-schedule client
│   ├── section_scraper.py # Section/meeting/professor ingestion
│   ├── rmp.py             # RateMyProfessors ease ratings
│   ├── jobs.py            # Background scrape/refresh loops
│   └── database.py        # SQLite schema + queries
├── tests/
├── scripts/               # try_api.py, try_solver.py, try_llm.py
├── requirements.txt
└── railway.toml
```

---

## Running locally

**Prerequisites:** Python 3.12+

```bash
# 1. Create and activate a virtual environment (first time only)
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies (first time only)
pip install -r requirements.txt

# 3. Start the dev server
uvicorn backend.main:app --reload --port 8080
```

Open [http://localhost:8080](http://localhost:8080).

`--reload` watches for Python file changes and restarts automatically. Frontend
changes (HTML/CSS/JS) take effect on browser refresh.

On first run the sections table is empty, so a background scrape kicks off;
`/api/v1/scheduler/status` reports `"loading"` until data is available. To skip
the background jobs entirely (e.g. when testing against a pre-populated DB),
set `DISABLE_BACKGROUND_JOBS=1`.

---

## Term detection

The scheduler serves the **newest term present in the `sections` table**, so it
always targets the upcoming registration term once real section data exists.
Before any data is scraped it falls back to an algorithmic estimate of the
current term (`YYYYT`, e.g. `20265` = Fall 2026).

| Suffix | Semester | Approx. dates |
|---|---|---|
| `1` | Winter | Jan – Apr |
| `3` | Spring | Apr – Jun |
| `4` | Summer | Jun – Aug |
| `5` | Fall | Sep – Dec |

---

## Environment variables

A local `.env` file is read at startup (`python-dotenv`); copy any of the
following into it. `.env` is git-ignored.

| Variable | Required | Description |
|---|---|---|
| `PORT` | Auto-set by Railway | Port the server listens on |
| `DB_PATH` | No | Path to SQLite file (default `data/freeroomfinder.db`) |
| `YEAR_TERM` | No | Override the auto-detected term code (local testing only) |
| `OPENAI_API_KEY` | No | Enables LLM constraint parsing + explanations (deterministic fallback when unset) |
| `OPENAI_MODEL` | No | Shared LLM model name, used when a job-specific var is unset |
| `OPENAI_PARSE_MODEL` | No | Model for `parse_constraints` (falls back to `OPENAI_MODEL`) |
| `OPENAI_EXPLAIN_MODEL` | No | Model for `explain_schedules` (falls back to `OPENAI_MODEL`) |
| `DISABLE_BACKGROUND_JOBS` | No | `1` disables the background section-scrape / RMP-refresh loops |
| `SECTION_SCRAPE_INTERVAL_SECONDS` | No | Weekly re-scrape cadence (default `604800`) |
| `RMP_REFRESH_INTERVAL_SECONDS` | No | RMP refresh cadence (default `604800`) |
| `REGISTRATION_WINDOW_START` | No | `YYYY-MM-DD` — start of the daily-scrape window around registration |
| `REGISTRATION_WINDOW_END` | No | `YYYY-MM-DD` — end of the daily-scrape window |
| `DAILY_SCRAPE_INTERVAL_SECONDS` | No | Scrape cadence inside the registration window (default `86400`) |
| `SCHEDULER_TIMEOUT_SECONDS` | No | Per-request ceiling on the solver (default `20`) |
| `GENERATE_RATE_LIMIT` | No | `/schedules/generate` requests/min/IP (default `10`) |

---

## API (`/api/v1/scheduler`)

```bash
# Autocomplete over course codes + titles (prefix first, then fuzzy)
curl "http://localhost:8080/api/v1/scheduler/courses/search?q=mktg&limit=10"

# Generate up to 3 conflict-free schedules
curl -X POST "http://localhost:8080/api/v1/scheduler/schedules/generate" \
     -H "Content-Type: application/json" \
     -d '{
           "courses": ["MKTG 201", "STRAT 305", "FIN 201"],
           "preferences": ["easy", "no_gaps"],
           "days_off": ["Friday"],
           "earliest_start": "08:00",
           "latest_end": "22:00",
           "allow_online": true,
           "extra_constraints_text": "no classes Tuesday before 10am"
         }'

# Data freshness + term + whether the LLM key is configured
curl "http://localhost:8080/api/v1/scheduler/status"
```

- `GET /courses/search?q=…&limit=10` — case-insensitive, ignores
  spaces/hyphens; prefix matches ranked first, then fuzzy; empty list for
  queries `< 2` chars. Returns `code`, `title`, `credits`, `section_count`.
- `POST /schedules/generate` — body: `courses` (1–8), `preferences` (ordered
  list of exactly 2 from `easy`/`no_gaps`), `days_off` (default `["Friday"]`),
  `earliest_start`/`latest_end` (`HH:MM`), `allow_online`,
  `extra_constraints_text` (≤ 500 chars). Returns the active term, up to 3
  schedules (sections with meetings, instructor names, ease score +
  confidence, per-professor `ease_breakdown`, metrics, score, explanation),
  parser `warnings` and `assumptions`, and `cap_hit`.
- Errors: unknown course codes → `422` with fuzzy `suggestions`; no feasible
  schedule → `200` with `"status": "no_schedule"` and a structured
  `bottleneck` diagnosis; bad input → `422`; data still loading → `503`.
- Rate limit: `10` `/schedules/generate` requests/min/IP (`429`).

Run `python scripts/try_api.py` for these examples, or
`python scripts/try_api.py --run` to hit a live server.

---

## Deployment (Railway)

Configured via `railway.toml`. To deploy:

```bash
git add .
git commit -m "your message"
git push
```

Railway picks up the push and redeploys automatically.

---

## Data source / upstream respect

Source is BYU's public `noauth` class search (no login, no credentials ever
collected/proxied/stored). We rate-limit callers and send a normal browser UA.
BYU's IT appropriate-use policy has no explicit scraping license — `noauth`
implies public, but if OIT objects we throttle further. No commercial use; site
use is incidental/personal.

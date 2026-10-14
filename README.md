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
│   ├── admin.html         # Secret per-term refresh admin page (no login)
│   └── favicon.svg
├── backend/
│   ├── main.py            # FastAPI app, lifespan, static serving
│   ├── config.py          # Settings, term-code computation
│   ├── models.py          # Pydantic request/response models
│   ├── scheduler_api.py   # /api/v1/scheduler/* routes
│   ├── admin.py           # /next-term/<secret>/* admin routes + security
│   ├── logging_filter.py  # Redacts ADMIN_PATH_SECRET from all log records
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

The scheduler serves the **published term**: `settings.active_term` if an admin
has set it, otherwise the newest term present in the `sections` table. Terms
that are scraped but not yet published are invisible to students. Before any
data is scraped it falls back to an algorithmic estimate of the current term
(`YYYYT`, e.g. `20265` = Fall 2026).

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
| `ADMIN_PATH_SECRET` | No | Secret path token (≥ 24 chars) that enables the `/next-term/*` admin page |
| `TRUST_PROXY` | No | `1` to trust `X-Forwarded-For` for per-IP rate limits (set behind Railway's proxy) |

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

## Per-term refresh (secret admin page)

The admin page lives at a secret URL and is used to scrape a new term, refresh
RateMyProfessors ratings, paste and verify Registration Cart envelopes, and
publish the new term. There is **no login, cookie, or session** — the secret is
part of the URL itself, and the page uses relative URLs so the secret is never
hardcoded in a file.

### Generating the secret

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

Copy the output into your environment as `ADMIN_PATH_SECRET`. If the variable
is unset or shorter than 24 characters, every `/next-term/*` route returns a
plain 404 (the feature is off).

### Setting it in Railway

1. Open your Railway project → **Variables**.
2. Add `ADMIN_PATH_SECRET` with the generated token.
3. (Recommended) add `TRUST_PROXY=1` so per-IP rate limits see the real client
   IP through Railway's proxy.
4. Redeploy.

### Bookmark URL

```
https://<your-domain>/next-term/<ADMIN_PATH_SECRET>/
```

For example, if your secret is `AbCd1234_...`, bookmark:

```
https://freeroomfinder-production.up.railway.app/next-term/AbCd1234_.../
```

### The per-term process (top to bottom)

1. **Sections** — pick the new term from BYU's yearterm list, click **Scrape
   term** (a background job with live progress), or **Refresh published term
   now** to re-scrape the current one.
2. **Ratings** — click **Refresh new &amp; stale professors** (default,
   ~1 request/sec) or **Re-match everything** (a long-running pass; confirm
   dialog). Review the **Needs review** table (low confidence / multiple
   candidates) before publishing.
3. **Envelopes** — paste the Registration Cart text for the term (or upload the
   `.txt`), **Parse &amp; preview**, then **Verify against schedule**. Fix any
   hard problems, edit display titles if needed, then **Save**. The database is
   the source of truth; the raw text is also stored in `envelope_sources` for
   backup, and **Download envelopes as JSON** gives you a portable backup.
4. **Publish** — the pre-publish checklist (sections exist, section count within
   30% of the current term, no hard envelope problems, rating coverage within 20
   points, no job running) blocks publishing unless it passes; an explicit
   **Override** checkbox bypasses it.
5. **Job log** — the last 50 `job_runs` rows show what ran and its counts.

### Rotating the secret

Generate a new token, update `ADMIN_PATH_SECRET` in Railway, and redeploy. The
old URL stops working immediately (404). Update your bookmark.

### ⚠️ Keep the URL private

Anyone with the URL can scrape terms, refresh ratings, edit envelopes, and
publish. Never share it, log it, or commit it. The app redacts the secret from
its own logs, but your browser history still contains it.

---

## Envelopes (Registration Cart bundles)

Some programs (Marriott junior cores, SFL 221/222, Learning Communities, Math
213/215, …) require registering for a **pre-bundled envelope** rather than
individual sections. BYU's schedule data carries no envelope flag and the
bundles can't be derived by rules, so the source of truth is the text of the
Registration Cart's "Choose an envelope" screens, pasted once per term.

The envelope data lives in `data/envelopes/<term>/<slug>.json` (parsed from the
raw paste), loaded idempotently into the DB at startup, and verified against
the scraped schedule so only `ok` bundles are offered to students.

### Refreshing envelopes each term

1. Open the Registration Cart's envelope screens in myBYU.
2. Copy the text of each program's list into `data/envelopes/raw/<term>.txt`,
   with a title line per program and blank lines between programs (keep the
   titles exactly as they appear, even if misspelled).
3. Parse the paste into committed JSON:

   ```bash
   python scripts/import_cart_text.py data/envelopes/raw/20271.txt --term 20271
   ```

4. Verify every bundle against the scraped schedule:

   ```bash
   python scripts/check_envelopes.py --term 20271
   ```

   Any `mismatch` (meeting/room/instructor drift) or `missing` (a section not
   in the DB) bundle fails the check with a non-zero exit; only `ok` bundles
   are offered to students.
5. Commit the generated `data/envelopes/<term>/*.json` files. Production starts
   from an empty DB, so the committed JSON is what ships.

The CLI scripts (`scripts/import_cart_text.py`, `scripts/check_envelopes.py`)
still work and remain the offline alternative; the admin page wraps the same
verify logic with a live preview. `scripts/check_envelopes.py --strict` also
fails on soft warnings (instructor-name changes), matching the admin checklist.

---

## Data source / upstream respect

Source is BYU's public `noauth` class search (no login, no credentials ever
collected/proxied/stored). We rate-limit callers and send a normal browser UA.
BYU's IT appropriate-use policy has no explicit scraping license — `noauth`
implies public, but if OIT objects we throttle further. No commercial use; site
use is incidental/personal.

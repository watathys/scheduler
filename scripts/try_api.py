"""Try the scheduler API endpoints (Step 5).

Without ``--run``, prints copy-paste curl examples for every endpoint.
With ``--run``, actually calls a running server (default http://localhost:8080)
and prints the JSON responses.

    python scripts/try_api.py                 # just print curl examples
    python scripts/try_api.py --run           # hit the running server
    python scripts/try_api.py --run --base http://localhost:8080
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

# This is a pure HTTP client, but read .env for parity with the other scripts.
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

DEFAULT_BASE = os.getenv("SCHEDULER_API_BASE", "http://localhost:8080")

CURL_EXAMPLES = [
    (
        "1. Course autocomplete (prefix + fuzzy)",
        'curl "http://localhost:8080/api/v1/scheduler/courses/search?q=mktg&limit=10"',
    ),
    (
        "2. Course autocomplete (spaces/hyphens ignored)",
        'curl "http://localhost:8080/api/v1/scheduler/courses/search?q=mktg-201"',
    ),
    (
        "3. Generate schedules (happy path)",
        'curl -X POST "http://localhost:8080/api/v1/scheduler/schedules/generate" \\\n'
        '     -H "Content-Type: application/json" \\\n'
        "     -d '{\n"
        '           "courses": ["MKTG 201", "STRAT 305", "FIN 201"],\n'
        '           "preferences": ["easy", "days_off", "no_gaps"],\n'
        '           "days_off": ["Friday"],\n'
        '           "earliest_start": "08:00",\n'
        '           "latest_end": "22:00",\n'
        '           "allow_online": true,\n'
        '           "extra_constraints_text": "no classes Tuesday before 10am"\n'
        "         }'",
    ),
    (
        "4. Scheduler status (term + data freshness)",
        'curl "http://localhost:8080/api/v1/scheduler/status"',
    ),
]


def _call(base: str, method: str, path: str, body: dict | None = None):
    import httpx

    url = base.rstrip("/") + path
    resp = httpx.request(method, url, json=body, timeout=30.0)
    return resp


def main() -> int:
    parser = argparse.ArgumentParser(description="Try the scheduler API endpoints")
    parser.add_argument("--run", action="store_true", help="call the running server")
    parser.add_argument("--base", default=DEFAULT_BASE, help="server base URL")
    args = parser.parse_args()

    if not args.run:
        print("Scheduler API — curl examples\n")
        for title, curl in CURL_EXAMPLES:
            print(title)
            print(curl)
            print()
        print("Run with --run to execute these against a live server.")
        return 0

    print(f"Base URL: {args.base}\n")
    # 1. Autocomplete
    r = _call(args.base, "GET", "/api/v1/scheduler/courses/search?q=mktg&limit=10")
    print("GET /courses/search?q=mktg", "->", r.status_code)
    print(json.dumps(r.json(), indent=2))
    print()

    # 2. Status
    r = _call(args.base, "GET", "/api/v1/scheduler/status")
    print("GET /status", "->", r.status_code)
    print(json.dumps(r.json(), indent=2))
    print()

    # 3. Generate
    r = _call(args.base, "POST", "/api/v1/scheduler/schedules/generate", {
        "courses": ["MKTG 201", "STRAT 305", "FIN 201"],
        "preferences": ["easy", "days_off", "no_gaps"],
        "days_off": ["Friday"],
        "earliest_start": "08:00",
        "latest_end": "22:00",
        "allow_online": True,
        "extra_constraints_text": "no classes Tuesday before 10am",
    })
    print("POST /schedules/generate", "->", r.status_code)
    print(json.dumps(r.json(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

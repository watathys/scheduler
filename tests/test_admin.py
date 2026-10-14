"""Tests for the secret per-term refresh admin page and API.

No network: background jobs are disabled and the yearterm picker is mocked.
A temp SQLite DB backs every test.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.main as main  # noqa: E402
from backend import admin, database, envelopes, jobs, scheduler_api  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.logging_filter import SecretRedactionFilter  # noqa: E402

TERM = "20271"
SECRET = "correct-horse-battery-staple-123456"  # >= 24 chars


# ── Fixtures & seeding helpers ───────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DISABLE_BACKGROUND_JOBS", "1")
    monkeypatch.setenv("YEAR_TERM", TERM)
    monkeypatch.setenv("ADMIN_PATH_SECRET", SECRET)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    db_path = str(tmp_path / "admin_test.db")
    monkeypatch.setattr(database, "DB", db_path)
    monkeypatch.setattr(settings, "db_path", db_path)
    monkeypatch.setattr(settings, "current_term", TERM)
    monkeypatch.setattr(envelopes, "ENVELOPES_DIR", tmp_path / "envelopes")

    # Global in-memory state: start each test clean.
    admin._guess_rate.clear()
    scheduler_api._generate_rate.clear()
    jobs._running.update({"sections": False, "rmp": False})
    jobs._progress.clear()

    # The yearterm picker hits BYU; mock it so tests never touch the network.
    monkeypatch.setattr(admin, "_yearterm_options", _fake_yearterms)

    with TestClient(main.app) as c:
        yield c


async def _fake_yearterms():
    return [{"term": "20265", "label": "Fall 2026"}, {"term": "20271", "label": "Winter 2027"}]


def _seed_section(code, number, meetings, *, term=TERM, instructor="Prof A"):
    async def run():
        pid = await database.upsert_professor(None, instructor)
        sid = await database.upsert_section(term, code, number, instructor_id=pid)
        await database.replace_section_meetings(
            sid, [(m[0], m[1], m[2], m[3], m[4]) for m in meetings]
        )
        return sid
    return asyncio.run(run())


def M(day, start, end, building="TNRB", room="101"):
    return (day, start, end, building, room)


CART_TEXT = """Test program
Alice Jones
OK 101
Section 001
10/10 seats left
3 CREDITS
M
9a - 10a
ROOM 1
Env 20001
Select
"""


# ── Access model ─────────────────────────────────────────────────────────────

def test_admin_off_when_secret_unset(client, monkeypatch):
    monkeypatch.setenv("ADMIN_PATH_SECRET", "")
    assert client.get(f"/next-term/{SECRET}/api/status").status_code == 404
    assert client.get(f"/next-term/{SECRET}/").status_code == 404


def test_admin_off_when_secret_short(client, monkeypatch):
    monkeypatch.setenv("ADMIN_PATH_SECRET", "too-short")
    assert client.get("/next-term/too-short/api/status").status_code == 404


def test_wrong_secret_404_indistinguishable(client):
    wrong = client.get("/next-term/not-the-real-secret/api/status")
    missing = client.get("/this-route-does-not-exist")
    assert wrong.status_code == 404
    assert missing.status_code == 404
    assert wrong.json() == missing.json()
    assert wrong.json()["detail"] == "Not Found"


def test_right_secret_200_and_security_headers(client):
    r = client.get(f"/next-term/{SECRET}/api/status")
    assert r.status_code == 200
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-robots-tag"] == "noindex, nofollow"
    assert r.headers["cache-control"] == "no-store"
    assert "set-cookie" not in r.headers


def test_admin_page_serves_html(client):
    r = client.get(f"/next-term/{SECRET}/")
    assert r.status_code == 200
    body = r.text
    assert '<meta name="robots" content="noindex">' in body
    # No third-party scripts/fonts/images are loaded.
    for needle in ("http://", "https://", "cdn.", "@import", "googleapis", "gstatic"):
        assert needle not in body


def test_no_cors_on_admin_routes(client):
    r = client.get(
        f"/next-term/{SECRET}/api/status",
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers
    assert "access-control-allow-methods" not in r.headers


def test_rate_limit_wrong_guesses_then_429(client):
    url = "/next-term/wrong-secret-guess/api/status"
    codes = [client.get(url).status_code for _ in range(admin._RATE_LIMIT)]
    assert codes == [404] * admin._RATE_LIMIT
    assert client.get(url).status_code == 429
    # The correct secret still passes regardless of the guess budget.
    assert client.get(f"/next-term/{SECRET}/api/status").status_code == 200


def test_state_changing_routes_reject_get(client):
    for path in (
        "api/sections/scrape", "api/sections/refresh",
        "api/ratings/refresh", "api/ratings/rematch",
        "api/envelopes/save", "api/publish",
    ):
        r = client.get(f"/next-term/{SECRET}/{path}")
        assert r.status_code == 405, path


def test_client_ip_respects_trust_proxy(monkeypatch):
    from starlette.requests import Request

    def make(headers, client="1.2.3.4"):
        scope = {
            "type": "http",
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
            "client": (client, 1234),
        }
        return Request(scope)

    monkeypatch.setenv("TRUST_PROXY", "1")
    req = make({"x-forwarded-for": "9.9.9.9, 10.0.0.1"})
    assert admin.client_ip(req) == "9.9.9.9"

    monkeypatch.setenv("TRUST_PROXY", "0")
    req = make({"x-forwarded-for": "9.9.9.9, 10.0.0.1"})
    assert admin.client_ip(req) == "1.2.3.4"


# ── Logging redaction ────────────────────────────────────────────────────────

def test_secret_redaction_filter(monkeypatch):
    monkeypatch.setenv("ADMIN_PATH_SECRET", SECRET)
    flt = SecretRedactionFilter()

    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0,
        f'GET /next-term/{SECRET}/api/status', (), None,
    )
    assert flt.filter(rec) is True
    assert SECRET not in rec.getMessage()
    assert "***" in rec.getMessage()

    # Format args are redacted too.
    rec2 = logging.LogRecord(
        "uvicorn.error", logging.ERROR, "", 0,
        "requested path %s", (f"/next-term/{SECRET}/",), None,
    )
    assert flt.filter(rec2) is True
    assert SECRET not in rec2.getMessage()


# ── Terms: publishing & student isolation ────────────────────────────────────

def test_unpublished_term_invisible_and_publish_switches(client):
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)], term="20271")
    _seed_section("MKTG 201", "002", [M("Monday", 600, 660)], term="20271")
    _seed_section("STRAT 999", "001", [M("Monday", 480, 540)], term="20265")

    # Fallback term = newest in DB (20271). 20265 must be invisible.
    r = client.get("/api/v1/scheduler/courses/search", params={"q": "strat"})
    assert r.status_code == 200
    assert "STRAT 999" not in [i["code"] for i in r.json()]

    s = client.get("/api/v1/scheduler/status").json()
    assert s["term"] == "20271"
    assert s["section_count"] == 2

    # Publish 20265 (override any failing checks).
    r = client.post(f"/next-term/{SECRET}/api/publish",
                    json={"term": "20265", "override": True})
    assert r.status_code == 200
    assert r.json()["published"] is True

    r = client.get("/api/v1/scheduler/courses/search", params={"q": "strat"})
    assert "STRAT 999" in [i["code"] for i in r.json()]
    assert client.get("/api/v1/scheduler/status").json()["term"] == "20265"


def test_get_active_term_fallback_when_setting_unset(client):
    _seed_section("MKTG 201", "001", [M("Monday", 480, 540)], term="20271")
    # No settings.active_term -> newest term wins.
    assert asyncio.run(scheduler_api.get_active_term()) == "20271"


# ── Job locks ────────────────────────────────────────────────────────────────

def test_second_scrape_refused_while_running(client, monkeypatch):
    monkeypatch.setitem(jobs._running, "sections", True)
    r = client.post(f"/next-term/{SECRET}/api/sections/scrape", json={"term": TERM})
    assert r.status_code == 409


# ── Envelopes: preview / save / verify / seed ────────────────────────────────

def test_preview_does_not_save(client):
    r = client.post(f"/next-term/{SECRET}/api/envelopes/preview",
                    json={"term": TERM, "raw_text": CART_TEXT})
    assert r.status_code == 200
    assert len(r.json()["programs"]) == 1
    assert asyncio.run(database.get_envelopes(TERM)) == []
    assert asyncio.run(database.get_envelope_source(TERM)) is None


def test_save_persists_and_writes_source(client):
    _seed_section("OK 101", "001", [M("Monday", 540, 600, "ROOM", "1")],
                  instructor="Bob Williams")
    r = client.post(f"/next-term/{SECRET}/api/envelopes/save",
                    json={"term": TERM, "raw_text": CART_TEXT, "mode": "term"})
    assert r.status_code == 200

    db_envs = asyncio.run(database.get_envelopes(TERM))
    assert len(db_envs) == 1
    assert db_envs[0]["bundles"][0]["ok"] == 1  # verified ok (instructor warning only)

    src = asyncio.run(database.get_envelope_source(TERM))
    assert src is not None
    assert src["raw_text"] == CART_TEXT


def test_verify_reports_hard_problems_and_warnings(client):
    _seed_section("OK 101", "001", [M("Monday", 540, 600, "ROOM", "1")],
                  instructor="Bob Williams")
    _seed_section("BAD 101", "001", [M("Monday", 600, 660, "ROOM", "2")],
                  instructor="Carol Smith")

    text = """Test program
Alice Jones
OK 101
Section 001
10/10 seats left
3 CREDITS
M
9a - 10a
ROOM 1
Env 20001
Select
Carol Smith
BAD 101
Section 001
10/10 seats left
3 CREDITS
M
9a - 10a
ROOM 2
Env 20002
Select
"""
    r = client.post(f"/next-term/{SECRET}/api/envelopes/verify",
                    json={"term": TERM, "raw_text": text})
    assert r.status_code == 200
    d = r.json()
    assert d["ok_bundles"] == 1
    assert d["mismatch_bundles"] == 1
    assert d["missing_bundles"] == 0
    assert len(d["problems"]) == 1          # BAD 101 time difference
    assert len(d["warnings"]) == 1          # OK 101 instructor change
    assert any("Alice Jones" in w and "bob williams" in w for w in d["warnings"])


def test_startup_seed_does_not_overwrite_admin_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB", str(tmp_path / "seed.db"))
    monkeypatch.setattr(envelopes, "ENVELOPES_DIR", tmp_path / "envelopes")
    asyncio.run(database.init_schedule_db())

    # Admin saved a program with an edited title.
    env = envelopes.parse_cart_text(CART_TEXT, TERM, "2026-10-13")[0]
    env.display_title = "Admin Kept Title"
    asyncio.run(envelopes.save_parsed_envelopes(TERM, [env]))

    # A JSON file with the SAME slug but a different title would overwrite it
    # if seeding ran; seeding must skip terms that already have envelopes.
    d = envelopes.json_dir_for_term(TERM)
    d.mkdir(parents=True, exist_ok=True)
    (d / "test-program.json").write_text(json.dumps({
        "title": "Test program", "display_title": "JSON Overwrite Title",
        "slug": "test-program", "term": TERM, "captured_at": "2026-10-13",
        "bundles": [],
    }), encoding="utf-8")

    seeded = asyncio.run(envelopes.seed_envelopes_from_json())
    assert seeded == []  # term already had envelopes -> not seeded

    db_envs = asyncio.run(database.get_envelopes(TERM))
    assert db_envs[0]["display_title"] == "Admin Kept Title"


# ── Publish checklist ────────────────────────────────────────────────────────

def test_publish_checklist_blocks_and_override(client):
    # No sections at all -> "sections exist" fails and blocks publish.
    r = client.post(f"/next-term/{SECRET}/api/publish",
                    json={"term": TERM, "override": False})
    assert r.status_code == 409
    body = r.json()
    assert body["published"] is False
    assert "sections_exist" in body["checklist"]["blockers"]

    # The override bypasses the failing check.
    r = client.post(f"/next-term/{SECRET}/api/publish",
                    json={"term": TERM, "override": True})
    assert r.status_code == 200
    assert r.json()["active_term"] == TERM

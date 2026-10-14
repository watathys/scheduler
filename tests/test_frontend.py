"""Tests for the scheduler frontend routing.

Covers:
  * ``/`` serves the scheduler UI (schedule builder).
  * The room-finder is gone: ``/rooms`` and the old room-finder API no longer
    resolve.
  * A tiny, JS-free sanity check that the scheduler HTML only references static
    asset files that actually exist on disk.
"""

import os
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.main as main  # noqa: E402
from backend import database  # noqa: E402
from backend.config import settings  # noqa: E402

TERM = "20271"
FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DISABLE_BACKGROUND_JOBS", "1")
    monkeypatch.setenv("YEAR_TERM", TERM)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    db_path = str(tmp_path / "frontend_test.db")
    monkeypatch.setattr(database, "DB", db_path)
    monkeypatch.setattr(settings, "db_path", db_path)
    monkeypatch.setattr(settings, "current_term", TERM)

    with TestClient(main.app) as c:
        yield c


# ── Routing ──────────────────────────────────────────────────────────────────

def test_root_serves_scheduler_page(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "BYU Schedule Builder" in body
    assert 'id="course-input"' in body
    assert 'id="btn-next"' in body


def test_room_finder_is_gone(client):
    # The room finder page and its API are removed: scheduling only.
    assert client.get("/rooms").status_code == 404
    assert client.get("/api/v1/buildings").status_code == 404
    assert client.get("/api/status").status_code == 404


# ── JS-free sanity check: HTML references only existing static files ─────────

def test_html_static_references_exist_on_disk():
    html = (FRONTEND_DIR / "scheduler.html").read_text(encoding="utf-8")
    refs = set(re.findall(r'(?:src|href)="/static/([^"]+)"', html))
    assert refs, "scheduler.html should reference at least one /static/ asset"
    for ref in refs:
        path = FRONTEND_DIR / ref
        assert path.is_file(), f"scheduler.html references missing asset /static/{ref}"


# ── Escaping: hostile names can never inject HTML ────────────────────────────

def test_frontend_renders_data_via_textcontent_not_innerhtml():
    """The DOM helper must use textContent, and innerHTML may only clear.

    Hostile instructor names (e.g. ``<img src=x onerror=...>``) flow through
    ``el()`` -> ``textContent``, which cannot parse markup, so they stay inert.
    We enforce that structurally: every ``innerHTML`` assignment in scheduler.js
    must be to an empty string (a container clear), never to dynamic data.
    """
    js = (FRONTEND_DIR / "scheduler.js").read_text(encoding="utf-8")

    # The shared element builder writes text via textContent.
    assert "node.textContent = text" in js

    # innerHTML is only ever used to empty a container.
    for m in re.finditer(r"\.innerHTML\s*=\s*([^;]+);", js):
        value = m.group(1).strip()
        assert value in ('""', "''"), (
            f"scheduler.js assigns non-empty innerHTML: {value}"
        )


def test_frontend_professor_row_builds_with_create_element():
    """The professor breakdown must construct rows with DOM APIs, not string
    concatenation into innerHTML, so a hostile display_name cannot break out."""
    js = (FRONTEND_DIR / "scheduler.js").read_text(encoding="utf-8")
    assert "function buildProfessorRow" in js
    # No `.innerHTML =` should appear inside the breakdown builders' region;
    # the global check above already guarantees that, but be explicit that the
    # name is attached via textContent (through the `el` helper).
    assert "el(\"span\", \"prof-name\"" in js
    # The RMP link is an <a> built with createElement, not string HTML.
    assert "a.rel = \"noopener noreferrer\"" in js


# ── Envelopes ────────────────────────────────────────────────────────────────

def test_html_references_envelope_picker():
    html = (FRONTEND_DIR / "scheduler.html").read_text(encoding="utf-8")
    assert 'id="envelope-input"' in html
    assert 'id="envelope-suggestions"' in html
    assert 'id="envelope-chips"' in html


def test_frontend_envelope_notice_copy_present():
    """The results view must carry the required envelope registration copy."""
    js = (FRONTEND_DIR / "scheduler.js").read_text(encoding="utf-8")
    assert "Register in the Registration Cart by choosing the envelope that lists exactly these sections" in js
    assert "Env numbers are specific to" in js
    assert "Some envelopes are restricted to certain programs or students. Confirm eligibility in myBYU." in js
    # The envelope sections are built with DOM APIs (via the `el` helper), not
    # string-concatenated into innerHTML.
    assert "function buildEnvelopeNotice" in js

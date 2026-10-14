"""Vercel entrypoint: re-export the FastAPI app.

Vercel's Python runtime detects this FastAPI app and serves it as a single
Vercel Function (Fluid compute). Every request is sent to this app and matched
by FastAPI's own router, so no rewrites are needed. The app already serves:

    ``/`` and ``/static/*``  — the frontend (from ``frontend/``)
    ``/api/v1/scheduler/*``  — the scheduler API
    ``/next-term/*``         — the per-term admin page

Required Vercel environment variables:

    DB_PATH=/tmp/schedule.db    writable path; restored from ``data/seed.db``
                                at startup (see ``backend.database``).
    DISABLE_BACKGROUND_JOBS=1   (optional — also auto-detected via VERCEL=1)

Optional: ``ADMIN_PATH_SECRET``, ``OPENAI_API_KEY``, ``TRUST_PROXY=1``.
"""

import os
import sys

# Make the project root importable so ``backend.*`` resolves regardless of how
# Vercel positions the function on ``sys.path``.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.main import app  # noqa: E402  (import after sys.path bootstrap)

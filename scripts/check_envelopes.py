#!/usr/bin/env python
"""Verify every envelope bundle against the scraped schedule.

Loads the committed ``data/envelopes/<term>/*.json`` files into the DB
(idempotently), resolves each bundle section to a ``sections`` row, then
compares the DB's meetings (days + times), room, and instructor with the
snapshot. Each bundle is classified ok / mismatch / missing; only "ok" bundles
are offered to students.

    python scripts/check_envelopes.py --term 20271

Hard problems (a section missing from the DB, or any difference in course,
section number, meeting days/times, or room) withhold a bundle and fail the
check. An instructor-name difference alone is a SOFT warning: the bundle stays
ok and is offered, but the name change is reported under "Warnings".

Exit status is non-zero only on hard problems; pass ``--strict`` to also fail
on warnings.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from backend import database, envelopes  # noqa: E402
from backend.config import settings  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--term", default=None,
                        help="BYU year-term code (default: newest term in the DB)")
    parser.add_argument("--strict", action="store_true",
                        help="also exit non-zero on soft warnings (instructor changes)")
    args = parser.parse_args()

    if os.getenv("DB_PATH"):
        database.DB = os.getenv("DB_PATH")

    await database.init_schedule_db()

    term = args.term or await database.get_latest_sections_term() or settings.current_term
    print(f"Verifying envelopes for term {term} (DB: {database.DB})")

    loaded = await envelopes.load_envelopes(term)
    if not loaded:
        print(f"No envelope JSON files found under {envelopes.json_dir_for_term(term)}/.")
        return 1

    report = await envelopes.verify_envelopes(term)

    # Per-program totals.
    print()
    print(f"{'program':<30}  {'ok':>3}  {'mismatch':>9}  {'missing':>7}")
    print(f"{'-' * 30}  {'-' * 3}  {'-' * 9}  {'-' * 7}")
    for env in report.envelopes:
        ok = sum(1 for b in env.bundles if b.status == "ok")
        mis = sum(1 for b in env.bundles if b.status == "mismatch")
        miss = sum(1 for b in env.bundles if b.status == "missing")
        print(f"{env.display_title:<30}  {ok:>3}  {mis:>9}  {miss:>7}")

    print()
    print(f"Totals — ok {report.ok_bundles}, mismatch {report.mismatch_bundles}, "
          f"missing {report.missing_bundles}")

    if report.warnings:
        print()
        print("Warnings (bundle stays ok; offered to students):")
        for w in report.warnings:
            print(f"  - {w}")

    if report.problems:
        print()
        print("Problems:")
        for p in report.problems:
            print(f"  - {p}")
        return 1

    if args.strict and report.warnings:
        print()
        print(f"Strict mode: {len(report.warnings)} warning(s) treated as failure.")
        return 1

    print()
    print("All bundles verified ok." if not report.warnings
          else f"All bundles verified ok ({len(report.warnings)} warning(s)).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

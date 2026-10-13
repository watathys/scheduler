"""Try the LLM layer for real.

    python scripts/try_llm.py "no classes Tuesday at 4"
    python scripts/try_llm.py "nothing before 10 on Fridays" --explain

Without --explain it prints the parsed constraints (blocked times, assumptions,
warning). With --explain it also runs the solver on a few sample courses and
prints the schedule explanations.

Requires OPENAI_API_KEY and OPENAI_MODEL to be set for a real LLM call; without
them the fallbacks kick in (so it still runs).
"""

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

# Read a local .env file (if present) before importing backend modules.
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from backend import database, llm, solver  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.models import SolverRequest  # noqa: E402
from backend.solver import SolverError  # noqa: E402

DEFAULT_COURSES = ["MKTG 201", "STRAT 325"]


def fmt_min(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


async def _detect_term() -> str:
    import aiosqlite
    async with aiosqlite.connect(database.DB) as db:
        async with db.execute(
            "SELECT DISTINCT term FROM sections ORDER BY term DESC"
        ) as cur:
            rows = await cur.fetchall()
    return rows[0][0] if rows else settings.current_term


async def main() -> int:
    parser = argparse.ArgumentParser(description="Try the LLM constraint parser / explainer")
    parser.add_argument("constraint", help="free-text constraints, e.g. 'no classes Tuesday at 4'")
    parser.add_argument("--explain", action="store_true",
                        help="also run the solver on sample courses and print explanations")
    parser.add_argument("--courses", nargs="*", default=DEFAULT_COURSES,
                        help="courses to solve when --explain (default sample set)")
    parser.add_argument("--term", default=None, help="BYU term code (default: latest in DB)")
    parser.add_argument("--prefs", nargs="+", default=["easy", "no_gaps", "fewer_days"],
                        help="ranked preferences")
    args = parser.parse_args()

    print(f"constraint text: {args.constraint!r}")
    parsed = await llm.parse_constraints(args.constraint)
    print(json.dumps(parsed.model_dump(), indent=2))

    if not args.explain:
        return 0

    if os.getenv("DB_PATH"):
        database.DB = os.getenv("DB_PATH")
    term = args.term or await _detect_term()

    req = SolverRequest(
        term=term,
        courses=args.courses,
        preferences=args.prefs,
        days_off=["Friday"],
        blocked_times=parsed.blocked_times,
        allow_online=False,
    )
    try:
        res = await solver.solve_schedule(req)
    except SolverError as e:
        print(f"solver error: {e}")
        return 1

    if not res.schedules:
        print("\nNo schedules produced (bottleneck):")
        if res.bottleneck and res.bottleneck.message:
            print("  ", res.bottleneck.message)
        return 1

    explanations = await llm.explain_schedules(res.schedules, req.preferences)
    print("\nSchedules + explanations:")
    for i, (sched, expl) in enumerate(zip(res.schedules, explanations)):
        print(f"\n--- Schedule {i + 1} (score {sched.total_score}) ---")
        print(f"  days on campus: {sched.metrics.days_on_campus} | "
              f"gaps: {sched.metrics.total_gap_minutes} min | "
              f"avg ease: {sched.metrics.average_ease}")
        print(f"  {expl}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

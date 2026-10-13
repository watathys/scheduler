"""Run the schedule solver against a real database from the command line.

Usage (DB_PATH points at the populated SQLite DB):
    python scripts/try_solver.py MKTG201 STRAT305 \
        --earliest 9:00 --latest 17:00 --prefs easy days_off no_gaps

Extra options:
    --term 20271                BYU term code (default: settings.current_term)
    --days-off Friday           repeatable; weekdays you want free
    --allow-online              permit online/remote sections
    --include-full              do NOT skip sections with zero open seats
    --blocked 'Mon 12:00-13:00' repeatable; a time you cannot attend
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

# Read a local .env file (if present) before importing backend modules.
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from backend import database, solver  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.models import BlockedTime, SolverRequest  # noqa: E402
from backend.solver import SolverError  # noqa: E402

_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def parse_hhmm(s: str) -> int:
    """'9:00' / '09:00' / '900' -> minutes since midnight."""
    digits = "".join(ch for ch in s if ch.isdigit())
    digits = digits.zfill(4)
    hour, minute = int(digits[:2]), int(digits[2:])
    if hour > 23 or minute > 59:
        raise argparse.ArgumentTypeError(f"bad time: {s!r}")
    return hour * 60 + minute


def fmt_min(m: int | None) -> str:
    if m is None:
        return "—"
    return f"{m // 60:02d}:{m % 60:02d}"


def parse_blocked(s: str) -> BlockedTime:
    try:
        day_part, time_part = s.strip().split(maxsplit=1)
    except ValueError:
        raise argparse.ArgumentTypeError(f"blocked must be 'DAY HH:MM-HH:MM', got {s!r}")
    day = day_part.strip().title()
    if day not in _DAYS:
        raise argparse.ArgumentTypeError(f"unknown day {day_part!r} (use full weekday name)")
    start_s, _, end_s = time_part.partition("-")
    if not end_s:
        raise argparse.ArgumentTypeError(f"blocked must be 'DAY HH:MM-HH:MM', got {s!r}")
    return BlockedTime(day=day, start_min=parse_hhmm(start_s), end_min=parse_hhmm(end_s))


def _describe_schedule(sched, idx: int) -> str:
    lines = [f"Schedule {idx + 1}  (score {sched.total_score})"]
    m = sched.metrics
    lines.append(
        f"  days on campus: {m.days_on_campus} | total gap: {m.total_gap_minutes} min | "
        f"avg ease: {m.average_ease} | {m.professors_without_ratings} prof(s) unrated | "
        f"window {fmt_min(m.earliest_start)}–{fmt_min(m.latest_end)}"
    )

    # Merge sections + async into per-day buckets.
    by_day: dict[str, list] = {}
    for s in sched.sections:
        for mt in s.meetings:
            by_day.setdefault(mt.day, []).append((mt.start_min, mt, s))
    for s in sched.async_online:
        by_day.setdefault("Online / no set time", []).append((None, None, s))

    for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
                "Sunday", "Online / no set time"]:
        items = by_day.get(day)
        if not items:
            continue
        lines.append(f"  {day}:")
        items.sort(key=lambda t: (t[0] is None, t[0] or 0))
        for start, mt, s in items:
            if mt is None:  # async
                lines.append(
                    f"      {s.course_code}-{s.section_number}  (async, no set time)  "
                    f"{s.instructor_name or 'staff'}  ease {s.ease_score}"
                )
            else:
                room = f"{mt.building or ''} {mt.room or ''}".strip() or "—"
                lines.append(
                    f"      {fmt_min(mt.start_min)}-{fmt_min(mt.end_min)}  "
                    f"{s.course_code}-{s.section_number}  {room:12s}  "
                    f"{s.instructor_name or 'staff'}  ease {s.ease_score}"
                )
    return "\n".join(lines)


async def _detect_term() -> str:
    """Pick the term to solve for: the latest term actually present in the DB."""
    import aiosqlite
    async with aiosqlite.connect(database.DB) as db:
        async with db.execute(
            "SELECT DISTINCT term FROM sections ORDER BY term DESC"
        ) as cur:
            rows = await cur.fetchall()
    if rows:
        return rows[0][0]
    return settings.current_term


async def main() -> int:
    parser = argparse.ArgumentParser(description="Try the AI schedule solver on a real DB")
    parser.add_argument("courses", nargs="+", help="course codes, e.g. MKTG201 STRAT305")
    parser.add_argument("--term", default=None, help="BYU term code (default: latest in DB)")
    parser.add_argument("--earliest", default="7:00", help="earliest class start (HH:MM)")
    parser.add_argument("--latest", default="22:00", help="latest class end (HH:MM)")
    parser.add_argument("--prefs", nargs=3, default=["easy", "days_off", "no_gaps"],
                        help="three ranked preferences from easy/days_off/no_gaps")
    parser.add_argument("--days-off", nargs="*", default=["Friday"],
                        help="weekdays you want free (default: Friday)")
    parser.add_argument("--allow-online", action="store_true", help="allow online/remote sections")
    parser.add_argument("--include-full", action="store_true",
                        help="do not skip sections with zero open seats")
    parser.add_argument("--blocked", action="append", default=[], type=parse_blocked,
                        help="blocked time 'DAY HH:MM-HH:MM' (repeatable)")
    args = parser.parse_args()

    if os.getenv("DB_PATH"):
        database.DB = os.getenv("DB_PATH")

    term = args.term or await _detect_term()

    req = SolverRequest(
        term=term,
        courses=args.courses,
        preferences=args.prefs,
        days_off=args.days_off,
        earliest_start_min=parse_hhmm(args.earliest),
        latest_end_min=parse_hhmm(args.latest),
        allow_online=args.allow_online,
        blocked_times=args.blocked,
        skip_full_sections=not args.include_full,
    )

    try:
        res = await solver.solve_schedule(req)
    except SolverError as e:
        print(f"error: {e}")
        if e.suggestions:
            print("  closest matches:", ", ".join(e.suggestions))
        return 1

    if res.bottleneck is not None:
        print("No valid schedule could be built.")
        if res.bottleneck.message:
            print("  reason:", res.bottleneck.message)
        for r in res.bottleneck.no_sections:
            print(f"  {r.course}: no sections left ({r.removed_by})")
        for a, b in res.bottleneck.always_conflicting_pairs:
            print(f"  {a} always conflicts with {b}")
        return 1

    print(f"Term {req.term} | courses: {', '.join(args.courses)}")
    print(f"prefs: {args.prefs} | days off: {args.days_off} | "
          f"online {'OK' if args.allow_online else 'no'} | "
          f"{len(res.schedules)} schedule(s) returned"
          + (" (search capped)" if res.cap_hit else ""))
    print()
    for i, sched in enumerate(res.schedules):
        print(_describe_schedule(sched, i))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

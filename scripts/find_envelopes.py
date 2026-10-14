#!/usr/bin/env python
"""READ-ONLY discovery: how do BYU "envelopes" show up in the schedule data?

An *envelope* is a pre-bundled set of classes a student must register for as a
group (e.g. the Marriott junior-core envelopes). This script only *reads* BYU's
public class-search backend and the schedule site's own static JS. It never
writes to the database and never touches the room-finder / MCP code.

What it does
------------
1. Dumps the raw ``getSections.php`` response (every field, unmodified) for the
   target courses, plus the per-course ``catalog`` object.
2. Dumps the ``section_type_list`` from ``getYeartermData.php`` and the
   ``browse_data`` program tree + the raw ``getClasses.php`` course objects.
3. Greps the schedule site's own ``js/desktop.js`` / ``js/search.js`` for how it
   renders sections (``section_type``, ``headers``, program links).
4. Scans every string field of the fetched sections for envelope-ish keywords
   (envelope / core / cohort / reserved / ...) and prints matches.
5. Pattern analysis: for the junior-core groups, checks whether sections that
   share a section number (or occupy the same clock slot) have matching
   meeting patterns / back-to-back times / shared seats.
6. Fetches ``getCoursesForProgram.php`` (public, referenced by the schedule
   site) for the relevant Marriott programs and prints the course list, which
   is the closest public proxy for "envelope contents".

Usage
-----
    python scripts/find_envelopes.py                      # active term
    python scripts/find_envelopes.py --term 20271         # winter core
    python scripts/find_envelopes.py --no-site --no-programs   # offline-ish
    python scripts/find_envelopes.py --out /tmp/env.json  # save raw dump

Everything printed here is evidence. Anything inferred from timing patterns is
labelled "(PATTERN — guess)".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Iterable, Optional

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:  # reuse the project's term logic, but never import the DB layer
    from backend.config import settings  # noqa: E402
except Exception:  # pragma: no cover - script must still run standalone
    settings = None  # type: ignore

BASE = "https://commtech.byu.edu/noauth/classSchedule"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# ── Targets ──────────────────────────────────────────────────────────────────
# (dept, catalog_number) — exactly the courses in the discovery request.
TARGET_COURSES: list[tuple[str, str]] = [
    ("STRAT", "401"), ("STRAT", "402"), ("STRAT", "431"),
    ("STRAT", "411"), ("STRAT", "412"), ("STRAT", "432"),
    ("ACC", "401"), ("IS", "401"), ("MKTG", "411"), ("HRM", "401"),
]

# Companion courses that the target courses' own catalog text points at. IS 401
# says "enrollment in an IS Core envelope", and IS 403 says "concurrent
# enrollment in same section of IS 401, 402, and 414" — so the IS Core can only
# be seen by also looking at 402 / 403 / 404. Fetched + pattern-analysed, but
# not part of the full per-field dump of the 10 requested courses.
COMPANION_COURSES: list[tuple[str, str]] = [
    ("IS", "402"), ("IS", "403"), ("IS", "404"), ("IS", "414"),
]

# Departments we must query to resolve the target course IDs.
ALL_COURSES = TARGET_COURSES + [c for c in COMPANION_COURSES if c not in TARGET_COURSES]
TARGET_DEPTS = sorted({d for d, _ in ALL_COURSES})

# Marriott programs (program_id -> label) used by step 6.
MARRIOTT_PROGRAMS: dict[str, str] = {
    "35104": "Accounting (MAJOR)",
    "35106": "Accounting: Professional Accounting Emphasis",
    "35283": "Information Systems (MAJOR)",
    "35284": "Information Systems (MINOR)",
    "35291": "Strategic Management (MAJOR)",
    "35292": "Strategy (MINOR)",
    "35352": "Business Management (MAJOR)",
    "35285": "Entrepreneurial Management (MAJOR)",
    "35404": "Human Resource Management (MAJOR)",
    "35117": "Marketing (MAJOR)",
    "34942": "Global Supply Chain Management (MAJOR)",
}

# Keyword scan for step 4.
ENVELOPE_KEYWORDS = re.compile(
    r"envelope|cohort|core\b|reserved|bundle|must register|concurrent enrollment"
    r"|same section|block|cluster",
    re.I,
)

# Fields the current scraper reads (backend/section_scraper.py) — used to show
# what we currently ignore.
SCRAPER_FIELDS = {
    "section_number", "mode", "credit_hours", "instructors", "times",
    "availability",  # section-level
    "dept_name", "catalog_number", "catalog_suffix", "title",
    "minimum_credit_hours",  # course-level
}

DAY_LABELS = [("mon", "M"), ("tue", "T"), ("wed", "W"), ("thu", "R"),
              ("fri", "F"), ("sat", "S"), ("sun", "U")]


# ── Small HTTP helper ────────────────────────────────────────────────────────
class Byu:
    def __init__(self, delay: float = 0.4) -> None:
        self.client = httpx.Client(headers={"User-Agent": UA}, timeout=30.0)
        self.delay = delay
        self.session_id: Optional[str] = None

    def close(self) -> None:
        self.client.close()

    def _sleep(self) -> None:
        if self.delay:
            time.sleep(self.delay)

    def session(self) -> str:
        if self.session_id:
            return self.session_id
        resp = self.client.get(f"{BASE}/index.php")
        resp.raise_for_status()
        m = re.search(r'_session_id\s*=\s*"([^"]+)"', resp.text)
        if not m:
            raise RuntimeError("could not find _session_id on class-search page")
        self.session_id = m.group(1)
        return self.session_id

    def post_json(self, path: str, data: dict) -> Any:
        self._sleep()
        resp = self.client.post(
            f"{BASE}/{path.lstrip('/')}", data=data,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        resp.raise_for_status()
        return resp.json()

    def get_text(self, path: str) -> str:
        self._sleep()
        resp = self.client.get(f"{BASE}/{path.lstrip('/')}")
        resp.raise_for_status()
        return resp.text

    def sections(self, course_id: str, yearterm: str) -> dict:
        return self.post_json("ajax/getSections.php", {
            "courseId": course_id, "sessionId": self.session(),
            "yearterm": yearterm,
        })

    def classes_for(self, dept: str, yearterm: str) -> dict[str, dict]:
        data = self.post_json("ajax/getClasses.php", {
            "searchObject[yearterm]": yearterm,
            "searchObject[teaching_areas][]": dept,
            "sessionId": self.session(),
        })
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}


# ── Formatting helpers ───────────────────────────────────────────────────────
def hr(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def day_str(block: dict) -> str:
    return "".join(lbl for flag, lbl in DAY_LABELS if block.get(flag)) or "-"


def section_lines(section: dict) -> list[str]:
    """One human line per meeting block for quick pattern reading."""
    out = []
    blocks = section.get("times") or []
    if not blocks:
        out.append("        (no meeting time)")
    for b in blocks:
        loc = f"{b.get('building') or 'TBA'} {b.get('room') or ''}".strip()
        out.append(
            f"        seq={b.get('sequence_number')} {day_str(b):<5} "
            f"{b.get('begin_time')}-{b.get('end_time')}  {loc}"
        )
    return out


def walk_strings(obj: Any, path: str = "") -> Iterable[tuple[str, str]]:
    """Yield (dotted_path, string) for every string leaf in a nested object."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk_strings(v, f"{path}.{k}".lstrip("."))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_strings(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj


# ── Steps ────────────────────────────────────────────────────────────────────
def step1_resolve_targets(byu: Byu, term: str
                          ) -> dict[tuple[str, str], tuple[str, dict]]:
    """Resolve (dept, catalog_number) -> (course_key, course_obj)."""
    hr(f"STEP 1 — resolve target course IDs via getClasses.php (term {term})")
    resolved: dict[tuple[str, str], tuple[str, dict]] = {}
    wanted = set(ALL_COURSES)
    for dept in TARGET_DEPTS:
        try:
            classes = byu.classes_for(dept, term)
        except Exception as e:  # pragma: no cover
            print(f"  !! getClasses.php failed for {dept}: {e}")
            continue
        for key, course in classes.items():
            tag = (course.get("dept_name"), course.get("catalog_number"))
            if tag in wanted:
                resolved[tag] = (key, course)
    print("  requested courses:")
    for tag in TARGET_COURSES:
        if tag in resolved:
            key, course = resolved[tag]
            print(f"    FOUND   {tag[0]} {tag[1]:<4} key={key:<12} {course.get('title')}")
        else:
            print(f"    MISSING {tag[0]} {tag[1]:<4} (not offered in term {term})")
    extras = [c for c in COMPANION_COURSES if c in resolved]
    if extras:
        print("  companion courses fetched for context (IS Core envelope):")
        for tag in extras:
            key, course = resolved[tag]
            print(f"    FOUND   {tag[0]} {tag[1]:<4} key={key:<12} {course.get('title')}")
    return resolved


def step2_dump_sections(byu: Byu, term: str,
                        resolved: dict[tuple[str, str], tuple[str, dict]]
                        ) -> dict[tuple[str, str], dict]:
    """Print EVERY field of EVERY section + the catalog object."""
    hr(f"STEP 2 — raw getSections.php for each target (term {term})")
    raw: dict[tuple[str, str], dict] = {}

    def fetch(tag: tuple[str, str]) -> Optional[dict]:
        key, course = resolved[tag]
        try:
            data = byu.sections(key, term)
        except Exception as e:  # pragma: no cover
            print(f"  !! getSections.php failed for {tag}: {e}")
            return None
        raw[tag] = data
        return data

    # Full per-field dump for the 10 requested courses.
    for tag in TARGET_COURSES:
        if tag not in resolved:
            continue
        data = fetch(tag)
        if data is None:
            continue
        key, course = resolved[tag]
        sections = data.get("sections") or []
        print()
        print("-" * 78)
        print(f"{tag[0]} {tag[1]}  — {len(sections)} section(s) "
              f"(courseId={key}, title={course.get('title')!r})")
        print("-" * 78)
        for section in sections:
            print(json.dumps(section, indent=2))
        catalog = data.get("catalog")
        if catalog:
            print("  --- catalog object (course-level) ---")
            print(json.dumps(catalog, indent=2))
        print("  --- extra top-level keys besides 'sections'/'catalog':",
              [k for k in data.keys() if k not in ("sections", "catalog")], "---")

    # Concise context dump for companions (their catalog text is the evidence).
    companions = [t for t in COMPANION_COURSES if t in resolved]
    if companions:
        hr("STEP 2c — companion courses (IS Core context) — catalog text + times")
        for tag in companions:
            data = fetch(tag)
            if data is None:
                continue
            cat = data.get("catalog") or {}
            print()
            print(f"{tag[0]} {tag[1]} — {cat.get('title')!r}")
            print(f"    catalog.note            : {cat.get('note')!r}")
            print(f"    catalog.prerequisite    : {cat.get('prerequisite')!r}")
            print(f"    catalog.untracked_prereq: {cat.get('untracked_prereq')!r}")
            print(f"    catalog.when_taught     : {cat.get('when_taught')!r}")
            for s in data.get("sections") or []:
                blocks = "; ".join(
                    f"{day_str(t)} {t.get('begin_time')}-{t.get('end_time')}"
                    for t in (s.get("times") or [])
                ) or "no time"
                print(f"      sec {s.get('section_number')} type={s.get('section_type')} "
                      f"reg={s.get('reg_method')} {blocks} "
                      f"avail={(s.get('availability') or {}).get('seats_available')}"
                      f"/{(s.get('availability') or {}).get('class_size')} "
                      f"headers={s.get('headers')}")
    return raw


def step2b_union_of_fields(raw: dict[tuple[str, str], dict]) -> None:
    hr("STEP 2b — union of all section-level fields seen + what we ignore")
    keys: set[str] = set()
    for data in raw.values():
        for section in data.get("sections") or []:
            keys.update(section.keys())
    print("  all section-level keys:", sorted(keys))
    print("  read by current scraper:", sorted(SCRAPER_FIELDS & keys))
    print("  IGNORED by current scraper:", sorted(keys - SCRAPER_FIELDS))
    cat_keys: set[str] = set()
    for data in raw.values():
        if isinstance(data.get("catalog"), dict):
            cat_keys.update(data["catalog"].keys())
    print("  catalog keys:", sorted(cat_keys))


def step3_keyword_scan(raw: dict[tuple[str, str], dict]) -> None:
    hr("STEP 3 — keyword scan of every string field (envelope? core? cohort? ...)")
    hits = 0
    for tag, data in raw.items():
        # sections
        for i, section in enumerate(data.get("sections") or []):
            for path, text in walk_strings(section, "section"):
                if ENVELOPE_KEYWORDS.search(text):
                    hits += 1
                    print(f"  [{tag[0]} {tag[1]} sec "
                          f"{section.get('section_number')}] {path}: {text!r}")
        # catalog
        for path, text in walk_strings(data.get("catalog") or {}, "catalog"):
            if ENVELOPE_KEYWORDS.search(text):
                hits += 1
                print(f"  [{tag[0]} {tag[1]}] {path}: {text!r}")
    if not hits:
        print("  (no keyword matches in the target courses)")
    print(f"  -> {hits} matching string(s)")


def step4_site_inspection(byu: Byu) -> None:
    hr("STEP 4 — how the schedule site's own JS renders sections")
    try:
        desktop = byu.get_text("js/desktop.js")
        search = byu.get_text("js/search.js")
    except Exception as e:  # pragma: no cover
        print(f"  !! could not fetch site JS: {e}")
        return
    needle = re.compile(
        r"envelope|section_type|header_text|dept_headers|course_headers|"
        r"getCoursesForProgram|seats_available|class_size|reserved",
        re.I,
    )
    for name, src in (("js/desktop.js", desktop), ("js/search.js", search)):
        print(f"\n  --- {name} ---")
        for n, line in enumerate(src.splitlines(), 1):
            if needle.search(line):
                print(f"    {n:>4}: {line.strip()[:150]}")
    # The site's own ajax endpoints (public):
    endpoints = sorted(set(re.findall(r"ajax/[A-Za-z_]+\.php", desktop + search)))
    print("\n  ajax endpoints used by the site:", endpoints)


def step5_programs(byu: Byu, term: str) -> None:
    hr(f"STEP 5 — getCoursesForProgram.php (public) — program course lists")
    print("  NOTE: this lists a *program's* courses (degree requirements) for the")
    print("  term — the closest public proxy for 'envelope contents'. It is not a")
    print("  per-term class bundle and carries no envelope flag.")
    targets = {f"{d} {n}" for d, n in TARGET_COURSES}
    for pid, label in MARRIOTT_PROGRAMS.items():
        try:
            data = byu.post_json("ajax/getCoursesForProgram.php",
                                 {"programId": pid, "yearterm": term})
        except Exception as e:  # pragma: no cover
            print(f"\n  !! program {pid} ({label}) failed: {e}")
            continue
        if not isinstance(data, list):
            print(f"\n  program {pid} ({label}): unexpected response {type(data)}")
            continue
        print(f"\n  program {pid} — {label} ({len(data)} courses)")
        for c in data:
            code = f"{c.get('dept_name')} {c.get('catalog_number')}{c.get('catalog_suffix') or ''}".strip()
            mark = "  <== TARGET" if code in targets else ""
            print(f"      {code:<12} {c.get('title')}{mark}")


# ── Step 6: pattern analysis ─────────────────────────────────────────────────
def _hhmm_to_minutes(hhmm: Any) -> Optional[int]:
    if hhmm is None:
        return None
    digits = "".join(ch for ch in str(hhmm) if ch.isdigit()).zfill(4)
    if len(digits) != 4:
        return None
    hour, minute = int(digits[:2]), int(digits[2:])
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _blocks(section: dict) -> list[tuple[str, int, int]]:
    """(days, start_min, end_min) per meeting block; skips TBA/online."""
    out = []
    for b in section.get("times") or []:
        days = day_str(b)
        start = _hhmm_to_minutes(b.get("begin_time"))
        end = _hhmm_to_minutes(b.get("end_time"))
        if start is None or end is None or days == "-":
            continue
        out.append((days, start, end))
    return out


def _fmt(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


CORE_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("STRAT junior core (Fall candidates)", [("STRAT", "401"), ("STRAT", "402")]),
    ("STRAT winter core", [("STRAT", "411"), ("STRAT", "412"), ("STRAT", "432")]),
    ("IS Core", [("IS", "401"), ("IS", "402"), ("IS", "403"), ("IS", "404")]),
]


def step6_patterns(raw: dict[tuple[str, str], dict]) -> None:
    hr("STEP 6 — pattern analysis (all timing conclusions are PATTERN — guess)")
    present = set(raw.keys())

    # A) same section-number across the group
    for label, courses in CORE_GROUPS:
        courses = [c for c in courses if c in present]
        if len(courses) < 2:
            continue
        print(f"\n  ### {label}")
        print("  (A) do sections sharing a section-number line up?")
        # index sections by number per course
        by_num: dict[str, dict[tuple[str, str], list[tuple[str, int, int]]]] = {}
        for tag in courses:
            for s in raw[tag].get("sections") or []:
                num = s.get("section_number")
                by_num.setdefault(num, {})[tag] = _blocks(s)
        for num in sorted(by_num):
            entries = by_num[num]
            if len(entries) < 2:
                continue
            print(f"    section_number {num}:")
            for tag in sorted(entries):
                blocks = entries[tag]
                desc = "; ".join(f"{d} {_fmt(a)}-{_fmt(b)}" for d, a, b in blocks) or "no time"
                print(f"      {tag[0]} {tag[1]}: {desc}")
            # compare each pair
            tags = sorted(entries)
            for i in range(len(tags)):
                for j in range(i + 1, len(tags)):
                    ta, tb = tags[i], tags[j]
                    for (da, sa, ea) in entries[ta]:
                        for (db, sb, eb) in entries[tb]:
                            same_days = set(da) == set(db)
                            if sa == sb and ea == eb:
                                kind = "SAME slot" + ("" if same_days else
                                                      " (complementary days)")
                                print(f"        -> {ta} & {tb}: {kind} "
                                      f"{da}/{db} {_fmt(sa)}-{_fmt(ea)}")
                            elif same_days and (0 <= sb - ea <= 20 or 0 <= sa - eb <= 20):
                                print(f"        -> {ta} & {tb}: BACK-TO-BACK on "
                                      f"{da} ({_fmt(sa)}-{_fmt(ea)} / "
                                      f"{_fmt(sb)}-{_fmt(eb)})")

        # B) cluster sections by exact clock slot (start,end), regardless of
        #    weekday — a coordinated envelope packs a cohort into one slot.
        print("  (B) sections clustered by clock slot (start-end):")
        slots: dict[tuple[int, int], list[str]] = {}
        for tag in courses:
            for s in raw[tag].get("sections") or []:
                for (days, start, end) in _blocks(s):
                    slots.setdefault((start, end), []).append(
                        f"{tag[0]} {tag[1]}.{s.get('section_number')}[{days}]"
                    )
        for (start, end) in sorted(slots):
            members = slots[(start, end)]
            if len(members) > 1:
                print(f"    {_fmt(start)}-{_fmt(end)}: " + ", ".join(sorted(members)))
        # C) seat alignment
        print("  (C) seats (available/class_size) per section:")
        for tag in sorted(courses):
            for s in raw[tag].get("sections") or []:
                av = s.get("availability") or {}
                print(f"      {tag[0]} {tag[1]}.{s.get('section_number')}: "
                      f"{av.get('seats_available')}/{av.get('class_size')}")


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    default_term = getattr(settings, "current_term", None) or "20265"
    parser.add_argument("--term", default=default_term,
                        help=f"BYU term code (default: active term {default_term})")
    parser.add_argument("--no-site", action="store_true",
                        help="skip fetching/scanning js/desktop.js + js/search.js")
    parser.add_argument("--no-programs", action="store_true",
                        help="skip getCoursesForProgram.php")
    parser.add_argument("--out", default=None,
                        help="also write the raw section JSON dump to this path")
    parser.add_argument("--delay", type=float, default=0.4,
                        help="seconds between requests (politeness)")
    args = parser.parse_args()

    print("READ-ONLY envelope discovery")
    print(f"  base          : {BASE}")
    print(f"  term          : {args.term}")
    print(f"  target courses: {', '.join(f'{d} {n}' for d, n in TARGET_COURSES)}")

    byu = Byu(delay=args.delay)
    try:
        hr(f"STEP 0 — getYeartermData.php (term {args.term})")
        yt = byu.post_json("ajax/getYeartermData.php", {"yearterm": args.term})
        print("  top-level keys:", list(yt.keys()))
        print("  section_type_list:", json.dumps(yt.get("section_type_list")))
        print("  department_list size:", len(yt.get("department_list") or {}))
        print("  department_map size:", len(yt.get("department_map") or {}))
        # Marriott school program tree
        browse = yt.get("browse_data") or {}
        for school, sv in browse.items():
            if "marriott" not in (school or "").lower():
                continue
            print(f"\n  browse_data school: {school}")
            for unit in sv.get("units", []):
                if unit.get("name") not in {
                    "Accountancy", "Information Systems", "Management",
                    "Marketing and Global Supply Chain",
                }:
                    continue
                print(f"    unit {unit.get('name')} — subjects "
                      f"{[s['dept_name'] for s in unit.get('subjects', [])]}")
                for p in unit.get("programs", []):
                    print(f"      program_id={p['program_id']:<7} "
                          f"{p['type']:<9} {p['long_desc']}")

        resolved = step1_resolve_targets(byu, args.term)
        raw = step2_dump_sections(byu, args.term, resolved)
        step2b_union_of_fields(raw)
        step3_keyword_scan(raw)
        if not args.no_site:
            step4_site_inspection(byu)
        if not args.no_programs:
            step5_programs(byu, args.term)
        step6_patterns(raw)

        if args.out:
            serializable = {f"{d} {n}": v for (d, n), v in raw.items()}
            with open(args.out, "w") as fh:
                json.dump(serializable, fh, indent=2)
            print(f"\n  raw dump written to {args.out}")

        hr("DONE")
        print("  Reminder: nothing above is written to any database. This script")
        print("  only reads public GET/POST endpoints and static JS.")
    finally:
        byu.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Registration Cart "envelope" support.

BYU's schedule data carries no envelope flag, and bundles cannot be derived by
rules (STRAT, SFL, Learning Communities and MBA bundles share sections or do
not line up by section number). The source of truth is the *text* of the
Registration Cart's "Choose an envelope" screens, pasted once per term into
``data/envelopes/raw/<term>.txt``.

This module:

  1. ``parse_cart_text`` — turn that pasted text into envelope/bundle/section
     records (pure, deterministic, no I/O beyond reading the string).
  2. ``load_envelopes`` — read the committed ``data/envelopes/<term>/*.json``
     files and load them into the DB idempotently, resolving each bundle
     section to a ``sections.id``.
  3. ``verify_envelopes`` — compare every bundle section against the scraped
     schedule (meetings, room, instructor), stamp each bundle ``ok`` /
     mismatch / missing, and return a report.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from . import database
from .matching import normalize_course_code, normalize_name

logger = logging.getLogger("freeroomfinder.envelopes")

ENVELOPES_DIR = Path(__file__).resolve().parents[1] / "data" / "envelopes"

# ── Line-level regexes ───────────────────────────────────────────────────────

SECTION_RE = re.compile(r"^Section\s+(\d+)$")
ENV_RE = re.compile(r"^Env\s+(\d+)$")
SELECT_RE = re.compile(r"^Select$")
SEATS_RE = re.compile(r"^(\d+)\s*/\s*(\d+)\s+seats?\s+left$")
CREDITS_RE = re.compile(r"^(\d+(?:\.\d+)?)\s+CREDITS?$")
TERM_RE = re.compile(r"^TERM\s+([12])$")
BLENDED_RE = re.compile(r"^Blended$")
NOTE_RE = re.compile(r"^Note$")

_TIME_TOKEN_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?([ap])$", re.I)
_TIME_RANGE_RE = re.compile(
    r"^(\d{1,2}(?::\d{2})?[ap])\s*[-–—]\s*(\d{1,2}(?::\d{2})?[ap])$", re.I
)

_ACRONYMS = {
    "sfl", "mba", "exdm", "hrm", "is", "acc", "math", "engl", "univ",
    "wrtg", "phys", "rel", "strat", "msb", "macc",
}


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class EnvelopeSection:
    course_code: str
    section_number: str
    instructor_snapshot: Optional[str]
    mode: Optional[str]            # "Blended" or None
    credits: Optional[float]
    part_of_term: Optional[str]    # "1" / "2" or None
    note_flag: bool
    meetings: list[dict] = field(default_factory=list)  # {day, start_min, end_min, room}


@dataclass
class EnvelopeBundle:
    env_id: Optional[str]
    capacity_snapshot: Optional[int]
    seats_snapshot: Optional[int]
    sections: list[EnvelopeSection] = field(default_factory=list)


@dataclass
class Envelope:
    title: str
    display_title: str
    slug: str
    term: str
    captured_at: str
    bundles: list[EnvelopeBundle] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ── Title / slug helpers ─────────────────────────────────────────────────────

def slugify(title: str) -> str:
    """Lowercase, non-alphanumerics to hyphens. Stable across terms."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "envelope"


def display_title(raw: str) -> str:
    """Clean a pasted title into a human display title.

    Titles are kept as-given in ``title``; this produces a nicer label. It
    title-cases words, uppercases known dept acronyms, expands ``jr``/``yr``,
    and fixes the two misspellings present in the sample paste.
    """
    s = raw.strip()
    override = {
        "accountin jr core": "Accounting Junior Core",
        "2nd yr macc pro": "2nd Year MAcc Program",
    }
    key = s.lower()
    if key in override:
        return override[key]

    out: list[str] = []
    for w in re.split(r"\s+", s):
        lw = w.lower()
        if lw == "jr":
            out.append("Junior")
        elif lw == "yr":
            out.append("Year")
        elif lw == "pro":
            out.append("Program")
        elif lw in _ACRONYMS:
            out.append(w.upper())
        else:
            out.append(w.capitalize())
    return " ".join(out) or s


# ── Pure parsing helpers ─────────────────────────────────────────────────────

def parse_days(line: str) -> Optional[list[str]]:
    """'MWF' -> ['Monday','Wednesday','Friday']; 'TTh' -> ['Tuesday','Thursday'].

    ``T`` = Tuesday, ``Th`` = Thursday. Returns None for non-day lines.
    """
    s = line.strip()
    days: list[str] = []
    i = 0
    while i < len(s):
        if s.startswith("Th", i):
            days.append("Thursday")
            i += 2
        elif s[i] == "M":
            days.append("Monday")
            i += 1
        elif s[i] == "T":
            days.append("Tuesday")
            i += 1
        elif s[i] == "W":
            days.append("Wednesday")
            i += 1
        elif s[i] == "F":
            days.append("Friday")
            i += 1
        else:
            return None
    return days or None


def parse_time_token(tok: str) -> Optional[int]:
    """'12p' -> 720, '12a' -> 0, '7:45a' -> 465, '4p' -> 960."""
    m = _TIME_TOKEN_RE.match(tok)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ap = m.group(3).lower()
    if ap == "a":
        if hour == 12:
            hour = 0
    else:
        if hour != 12:
            hour += 12
    return hour * 60 + minute


def parse_time_range(line: str) -> Optional[tuple[int, int]]:
    """'9:30a - 10:45a' -> (570, 645); '12:15p - 4p' -> (735, 960)."""
    m = _TIME_RANGE_RE.match(line.strip())
    if not m:
        return None
    a = parse_time_token(m.group(1))
    b = parse_time_token(m.group(2))
    if a is None or b is None or b <= a:
        return None
    return (a, b)


def _clean_lines(text: str) -> list[str]:
    """Drop blank lines and the cart's decorative ``... icon`` lines."""
    out: list[str] = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if "icon" in s.lower():
            continue
        out.append(s)
    return out


def _is_boundary(s: str) -> bool:
    return bool(
        SECTION_RE.match(s) or ENV_RE.match(s) or SELECT_RE.match(s) or NOTE_RE.match(s)
    )


# ── Main parser ──────────────────────────────────────────────────────────────

def parse_cart_text(text: str, term: str, captured_at: str) -> list[Envelope]:
    """Parse pasted Registration Cart text into a list of Envelopes.

    Anchors on ``Section NNN`` lines. A section record reads, in order:
    instructor, course code, ``Section NNN``, optional ``Blended``,
    ``A/B seats left``, ``N CREDIT(S)``, optional ``TERM 1|2``, one or more
    meeting triples (days / time range / room), optional ``Note``. A bundle is
    consecutive sections ended by ``Env NNNNN`` then ``Select``.
    """
    lines = _clean_lines(text)
    n = len(lines)
    anchors = [i for i, ln in enumerate(lines) if SECTION_RE.match(ln)]

    envelopes: list[Envelope] = []
    cur_env: Optional[Envelope] = None
    cur_bundle: Optional[EnvelopeBundle] = None
    prev_end = -1  # index of last consumed token (drives title detection)

    for si in anchors:
        if si < 2:
            continue
        instructor = lines[si - 2]
        course = lines[si - 1]

        # A title is the unconsumed token immediately before the instructor.
        if si >= 3 and si - 3 > prev_end:
            title = lines[si - 3]
            cur_env = Envelope(
                title=title,
                display_title=display_title(title),
                slug=slugify(title),
                term=term,
                captured_at=captured_at,
            )
            cur_bundle = None
            envelopes.append(cur_env)
        if cur_env is None:
            continue

        j = si + 1

        mode: Optional[str] = None
        if j < n and BLENDED_RE.match(lines[j]):
            mode = "Blended"
            j += 1

        cap: Optional[int] = None
        seats: Optional[int] = None
        if j < n:
            m = SEATS_RE.match(lines[j])
            if m:
                seats = int(m.group(1))
                cap = int(m.group(2))
                j += 1

        credits: Optional[float] = None
        if j < n:
            m = CREDITS_RE.match(lines[j])
            if m:
                credits = float(m.group(1))
                j += 1

        part: Optional[str] = None
        if j < n:
            m = TERM_RE.match(lines[j])
            if m:
                part = m.group(1)
                j += 1

        meetings: list[dict] = []
        while j < n:
            days = parse_days(lines[j])
            if days is None:
                break
            if j + 1 >= n:
                break
            tr = parse_time_range(lines[j + 1])
            if tr is None:
                break
            if j + 2 >= n or _is_boundary(lines[j + 2]):
                break
            room = lines[j + 2]
            start, end = tr
            for d in days:
                meetings.append({
                    "day": d, "start_min": start, "end_min": end, "room": room,
                })
            j += 3

        note = False
        if j < n and NOTE_RE.match(lines[j]):
            note = True
            j += 1

        section = EnvelopeSection(
            course_code=normalize_course_code(course),
            section_number=SECTION_RE.match(lines[si]).group(1),
            instructor_snapshot=instructor,
            mode=mode,
            credits=credits,
            part_of_term=part,
            note_flag=note,
            meetings=meetings,
        )

        if cur_bundle is None:
            cur_bundle = EnvelopeBundle(
                env_id=None, capacity_snapshot=None, seats_snapshot=None
            )
            cur_env.bundles.append(cur_bundle)
        cur_bundle.sections.append(section)
        if cur_bundle.capacity_snapshot is None and cap is not None:
            cur_bundle.capacity_snapshot = cap
            cur_bundle.seats_snapshot = seats

        prev_end = j - 1

        # Bundle terminator: "Env NNNNN" then "Select".
        if j < n and ENV_RE.match(lines[j]):
            cur_bundle.env_id = ENV_RE.match(lines[j]).group(1)
            if j + 1 < n and SELECT_RE.match(lines[j + 1]):
                prev_end = j + 1
            else:
                prev_end = j
            cur_bundle = None
        elif j < n and SELECT_RE.match(lines[j]):
            # Select without an Env line: keep the bundle, env_id stays null.
            cur_env.warnings.append(
                f"bundle {len(cur_env.bundles)} has no Env number "
                f"(no 'Env NNNNN' line before 'Select')"
            )
            prev_end = j
            cur_bundle = None

    # Any trailing bundle that never saw an Env line.
    for env in envelopes:
        for idx, b in enumerate(env.bundles, 1):
            if b.env_id is None and (
                not any(w.startswith(f"bundle {idx} ") for w in env.warnings)
            ):
                env.warnings.append(
                    f"bundle {idx} has no Env number (no 'Env NNNNN' line)"
                )

    return envelopes


# ── JSON serialization ───────────────────────────────────────────────────────

def envelope_to_dict(env: Envelope) -> dict:
    return {
        "title": env.title,
        "display_title": env.display_title,
        "slug": env.slug,
        "term": env.term,
        "captured_at": env.captured_at,
        "bundles": [
            {
                "env_id": b.env_id,
                "capacity_snapshot": b.capacity_snapshot,
                "seats_snapshot": b.seats_snapshot,
                "sections": [
                    {
                        "course_code": s.course_code,
                        "section_number": s.section_number,
                        "instructor_snapshot": s.instructor_snapshot,
                        "mode": s.mode,
                        "credits": s.credits,
                        "part_of_term": s.part_of_term,
                        "note_flag": s.note_flag,
                        "meetings": s.meetings,
                    }
                    for s in b.sections
                ],
            }
            for b in env.bundles
        ],
    }


def envelope_from_dict(data: dict) -> Envelope:
    env = Envelope(
        title=data.get("title", ""),
        display_title=data.get("display_title", data.get("title", "")),
        slug=data.get("slug", slugify(data.get("title", ""))),
        term=data.get("term", ""),
        captured_at=data.get("captured_at", ""),
    )
    for b in data.get("bundles", []):
        bundle = EnvelopeBundle(
            env_id=b.get("env_id"),
            capacity_snapshot=b.get("capacity_snapshot"),
            seats_snapshot=b.get("seats_snapshot"),
        )
        for s in b.get("sections", []):
            bundle.sections.append(EnvelopeSection(
                course_code=s.get("course_code", ""),
                section_number=s.get("section_number", ""),
                instructor_snapshot=s.get("instructor_snapshot"),
                mode=s.get("mode"),
                credits=s.get("credits"),
                part_of_term=s.get("part_of_term"),
                note_flag=bool(s.get("note_flag")),
                meetings=s.get("meetings", []),
            ))
        env.bundles.append(bundle)
    return env


# ── Cart-text serialization (for merging raw text on partial saves) ──────────

_DAY_TO_CODE = {
    "Monday": "M", "Tuesday": "T", "Wednesday": "W", "Thursday": "Th",
    "Friday": "F", "Saturday": "S", "Sunday": "Su",
}


def _format_time(minutes: int) -> str:
    """720 -> '12p', 570 -> '9:30a', 465 -> '7:45a'."""
    h = minutes // 60
    m = minutes % 60
    ap = "a" if h < 12 else "p"
    if h == 0:
        h = 12
    elif h > 12:
        h -= 12
    return f"{h}:{m:02d}{ap}" if m else f"{h}{ap}"


def _format_days(days: list[str]) -> str:
    return "".join(_DAY_TO_CODE.get(d, "") for d in days)


def envelope_to_text(env: Envelope) -> str:
    """Serialize one envelope back into Registration Cart text.

    Produces text that ``parse_cart_text`` can re-parse identically, so the
    admin "replace only these programs" flow can splice a program into an
    existing pasted blob without re-pasting every program.
    """
    lines: list[str] = [env.title]
    for b in env.bundles:
        for s in b.sections:
            lines.append(s.instructor_snapshot or "TBD")
            lines.append(s.course_code)
            lines.append(f"Section {s.section_number}")
            if s.mode:
                lines.append(s.mode)
            if b.seats_snapshot is not None and b.capacity_snapshot is not None:
                lines.append(f"{b.seats_snapshot} / {b.capacity_snapshot} seats left")
            if s.credits is not None:
                lines.append(f"{s.credits:g} CREDITS")
            if s.part_of_term:
                lines.append(f"TERM {s.part_of_term}")
            for m in s.meetings:
                lines.append(_format_days([m["day"]]))
                lines.append(f"{_format_time(int(m['start_min']))} - {_format_time(int(m['end_min']))}")
                lines.append(m.get("room") or "TBA")
            if s.note_flag:
                lines.append("Note")
        lines.append(f"Env {b.env_id}" if b.env_id else "Select")
        lines.append("Select")
    return "\n".join(lines)


def envelopes_to_text(envs: list[Envelope]) -> str:
    """Join several envelopes into one cart text blob (blank line between)."""
    return "\n\n".join(envelope_to_text(e) for e in envs)


# ── File discovery ───────────────────────────────────────────────────────────

def raw_file_for_term(term: str) -> Path:
    return ENVELOPES_DIR / "raw" / f"{term}.txt"


def json_dir_for_term(term: str) -> Path:
    return ENVELOPES_DIR / str(term)


def discover_envelope_files(term: str) -> list[Path]:
    d = json_dir_for_term(term)
    if not d.is_dir():
        return []
    return sorted(d.glob("*.json"))


# ── Loading into the DB ──────────────────────────────────────────────────────

async def load_envelopes(term: Optional[str] = None) -> list[Envelope]:
    """Load committed JSON files for a term into the DB, idempotently.

    Returns the parsed envelopes (snapshot) in discovery order.
    """
    if term is None:
        term = await database.get_latest_sections_term() or _default_term()

    loaded: list[Envelope] = []
    for path in discover_envelope_files(term):
        data = json.loads(path.read_text(encoding="utf-8"))
        env = envelope_from_dict(data)
        env.term = term

        bundles: list[dict] = []
        for b in env.bundles:
            sections: list[dict] = []
            for s in b.sections:
                section_id = await database.resolve_section_id(
                    term, s.course_code, s.section_number
                )
                sections.append({
                    "section_id": section_id,
                    "course_code": s.course_code,
                    "section_number": s.section_number,
                    "instructor_snapshot": s.instructor_snapshot,
                    "mode": s.mode,
                    "credits": s.credits,
                    "part_of_term": s.part_of_term,
                    "note_flag": s.note_flag,
                })
            bundles.append({
                "env_id": b.env_id,
                "capacity_snapshot": b.capacity_snapshot,
                "seats_snapshot": b.seats_snapshot,
                "sections": sections,
            })

        await database.replace_envelope(
            term, env.slug, env.title, env.display_title,
            env.captured_at, bundles,
        )
        loaded.append(env)
    return loaded


def _default_term() -> str:
    from .config import settings
    return settings.current_term


async def seed_envelopes_from_json() -> list[str]:
    """Seed the DB from committed ``data/envelopes/<term>/*.json`` at startup.

    Only terms with no envelopes already in the DB are seeded, so admin-saved
    data is never overwritten. Returns the list of terms that were seeded.
    """
    seeded: list[str] = []
    if not ENVELOPES_DIR.is_dir():
        return seeded
    for d in sorted(ENVELOPES_DIR.iterdir()):
        if not d.is_dir() or not d.name.isdigit():
            continue
        term = d.name
        if not discover_envelope_files(term):
            continue
        if await database.has_envelopes_for_term(term):
            continue
        await load_envelopes(term)
        seeded.append(term)
    return seeded


# ── Verification ─────────────────────────────────────────────────────────────

@dataclass
class SectionVerify:
    course_code: str
    section_number: str
    status: str = "ok"   # "ok" | "mismatch" | "missing"
    differences: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class BundleVerify:
    bundle_index: int
    env_id: Optional[str]
    status: str = "ok"   # "ok" | "mismatch" | "missing"
    sections: list[SectionVerify] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class EnvelopeVerify:
    slug: str
    display_title: str
    status: str = "ok"   # "ok" | "mismatch" | "missing"
    bundles: list[BundleVerify] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class VerifyReport:
    term: str
    envelopes: list[EnvelopeVerify] = field(default_factory=list)
    ok_bundles: int = 0
    mismatch_bundles: int = 0
    missing_bundles: int = 0
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_problems(self) -> bool:
        return bool(self.problems)


def _norm_room(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).upper()


def _name_parts(name: str) -> tuple[Optional[str], Optional[str]]:
    tokens = normalize_name(name).split()
    if not tokens:
        return None, None
    if len(tokens) == 1:
        return None, tokens[0]
    return tokens[0], tokens[-1]


def instructors_match(snapshot: str | None, actual: str | None) -> bool:
    """Compare a snapshot instructor to the scraped name.

    ``TBD`` / missing snapshot means "no snapshot to compare" -> always a match.
    Otherwise compare last name and first initial (case-insensitive).
    """
    if not snapshot or snapshot.strip().lower() == "tbd":
        return True
    if not actual:
        return False
    s_first, s_last = _name_parts(snapshot)
    a_first, a_last = _name_parts(actual)
    if not s_last or not a_last:
        return s_last == a_last
    if s_last != a_last:
        return False
    if s_first and a_first:
        return s_first[0] == a_first[0]
    return True


def _meeting_key(m: dict) -> tuple:
    return (
        (m.get("day") or "").strip().title(),
        int(m.get("start_min") or 0),
        int(m.get("end_min") or 0),
        _norm_room(m.get("room") or ""),
    )


def _meeting_key_db(m: dict) -> tuple:
    building = (m.get("building") or "").strip()
    room = (m.get("room") or "").strip()
    full = f"{building} {room}".strip()
    return (
        (m.get("day") or "").strip().title(),
        int(m.get("start_min") or 0),
        int(m.get("end_min") or 0),
        _norm_room(full),
    )


async def verify_envelopes(term: Optional[str] = None) -> VerifyReport:
    """Compare every bundle section against the scraped schedule.

    Reads the snapshot (JSON files) and the DB (resolved sections + meetings +
    instructor), classifies each section/bundle, stamps ``ok`` on bundles, and
    returns a report. Callers should treat a non-empty ``problems`` list as a
    hard failure.
    """
    if term is None:
        term = await database.get_latest_sections_term() or _default_term()

    report = VerifyReport(term=term)

    # Snapshot from JSON.
    snapshot_by_slug: dict[str, Envelope] = {}
    for path in discover_envelope_files(term):
        data = json.loads(path.read_text(encoding="utf-8"))
        env = envelope_from_dict(data)
        env.term = term
        snapshot_by_slug[env.slug] = env

    # Resolved bundles from the DB.
    db_envelopes = await database.get_envelopes(term)

    # Preload actual section details for all resolved section ids.
    all_section_ids: list[int] = []
    for env in db_envelopes:
        for b in env["bundles"]:
            for s in b["sections"]:
                if s.get("section_id") is not None:
                    all_section_ids.append(s["section_id"])
    details = await database.get_sections_details(all_section_ids)

    for env in db_envelopes:
        slug = env["slug"]
        snap = snapshot_by_slug.get(slug)
        env_verify = EnvelopeVerify(
            slug=slug, display_title=env["display_title"]
        )
        report.envelopes.append(env_verify)

        for b in env["bundles"]:
            bundle_index = b["bundle_index"]
            snap_bundle = None
            if snap is not None and bundle_index < len(snap.bundles):
                snap_bundle = snap.bundles[bundle_index]

            bundle_verify = BundleVerify(
                bundle_index=bundle_index, env_id=b["env_id"]
            )
            env_verify.bundles.append(bundle_verify)

            for s in b["sections"]:
                course_code = s["course_code"]
                section_number = s["section_number"]
                sv = SectionVerify(
                    course_code=course_code, section_number=section_number,
                    status="ok",
                )
                bundle_verify.sections.append(sv)

                # The snapshot (JSON) holds the meetings + instructor snapshot;
                # the DB row only records the resolved section_id + metadata.
                snap_section = None
                if snap_bundle is not None:
                    for ss in snap_bundle.sections:
                        if (ss.course_code == course_code
                                and ss.section_number == section_number):
                            snap_section = ss
                            break

                section_id = s.get("section_id")
                if section_id is None:
                    sv.status = "missing"
                    report.problems.append(
                        f"{env['display_title']} bundle {bundle_index + 1}: "
                        f"{course_code} {section_number} is not in the DB"
                    )
                    continue

                actual = details.get(section_id)
                if actual is None:
                    sv.status = "missing"
                    report.problems.append(
                        f"{env['display_title']} bundle {bundle_index + 1}: "
                        f"{course_code} {section_number} section {section_id} "
                        f"not found in DB"
                    )
                    continue

                # Meetings: snapshot set must equal DB set.
                snap_meetings = {
                    _meeting_key(m) for m in (snap_section.meetings if snap_section else [])
                }
                db_meetings = {_meeting_key_db(m) for m in actual["meetings"]}
                if snap_meetings != db_meetings:
                    sv.status = "mismatch"
                    missing = sorted(
                        f"{m[0]} {m[1]}-{m[2]} {m[3]}" for m in (snap_meetings - db_meetings)
                    )
                    extra = sorted(
                        f"{m[0]} {m[1]}-{m[2]} {m[3]}" for m in (db_meetings - snap_meetings)
                    )
                    diff = []
                    if missing:
                        diff.append(f"meetings not in DB: {', '.join(missing)}")
                    if extra:
                        diff.append(f"extra meetings in DB: {', '.join(extra)}")
                    sv.differences.extend(diff)

                # Instructor (last name + first initial). An instructor-name
                # change is a SOFT warning: students always see the DB's current
                # name, so a changed name never withholds a bundle by itself.
                instructor_snapshot = (
                    snap_section.instructor_snapshot if snap_section
                    else s.get("instructor_snapshot")
                )
                if not instructors_match(instructor_snapshot, actual["instructor_name"]):
                    sv.warnings.append(
                        f"instructor changed: {instructor_snapshot!r} -> "
                        f"{actual['instructor_name']!r}"
                    )

                if sv.status != "ok":
                    report.problems.append(
                        f"{env['display_title']} bundle {bundle_index + 1}: "
                        f"{course_code} {section_number}: " + "; ".join(sv.differences)
                    )

                for w in sv.warnings:
                    line = (
                        f"{env['display_title']} bundle {bundle_index + 1}: "
                        f"{course_code} {section_number}: {w}"
                    )
                    report.warnings.append(line)
                    env_verify.warnings.append(line)

            # Bundle status: missing if any section missing, else mismatch if
            # any section mismatches, else ok.
            bundle_verify.warnings = [
                w for sv in bundle_verify.sections for w in sv.warnings
            ]
            statuses = [sv.status for sv in bundle_verify.sections]
            if "missing" in statuses:
                bundle_verify.status = "missing"
                report.missing_bundles += 1
                ok = 0
            elif "mismatch" in statuses:
                bundle_verify.status = "mismatch"
                report.mismatch_bundles += 1
                ok = 0
            else:
                bundle_verify.status = "ok"
                report.ok_bundles += 1
                ok = 1

            await database.update_bundle_ok(b["id"], ok)

            if bundle_verify.status == "ok":
                env_verify.status = env_verify.status or "ok"
            elif bundle_verify.status == "mismatch" and env_verify.status != "missing":
                env_verify.status = "mismatch"
            elif bundle_verify.status == "missing":
                env_verify.status = "missing"

    return report


def _compare_section(
    snap_section: EnvelopeSection | None,
    instructor_snapshot: str | None,
    actual: dict | None,
) -> tuple[str, list[str], list[str]]:
    """(status, differences, warnings) for one resolved bundle section.

    ``actual`` is the ``database.get_sections_details`` entry for the resolved
    section, or None when the section is not in the DB. Returns "ok" |
    "mismatch" | "missing". Meeting drift (day/time/room) is a hard mismatch;
    an instructor-name change alone is a soft warning.
    """
    if actual is None:
        return "missing", [], []

    differences: list[str] = []
    warnings: list[str] = []

    snap_meetings = {
        _meeting_key(m) for m in (snap_section.meetings if snap_section else [])
    }
    db_meetings = {_meeting_key_db(m) for m in actual["meetings"]}
    if snap_meetings != db_meetings:
        status = "mismatch"
        missing = sorted(
            f"{m[0]} {m[1]}-{m[2]} {m[3]}" for m in (snap_meetings - db_meetings)
        )
        extra = sorted(
            f"{m[0]} {m[1]}-{m[2]} {m[3]}" for m in (db_meetings - snap_meetings)
        )
        if missing:
            differences.append(f"meetings not in DB: {', '.join(missing)}")
        if extra:
            differences.append(f"extra meetings in DB: {', '.join(extra)}")
    else:
        status = "ok"

    snap_instructor = (
        snap_section.instructor_snapshot if snap_section else instructor_snapshot
    )
    if not instructors_match(snap_instructor, actual["instructor_name"]):
        warnings.append(
            f"instructor changed: {snap_instructor!r} -> {actual['instructor_name']!r}"
        )

    return status, differences, warnings


async def verify_parsed_envelopes(term: str, parsed: list[Envelope]) -> VerifyReport:
    """Verify parsed (not-yet-saved) envelopes against the scraped schedule.

    Used by the admin "Verify against schedule" step so a paste can be checked
    before saving. Never touches the DB's envelope tables.
    """
    report = VerifyReport(term=term)

    section_ids: list[int] = []
    for env in parsed:
        for b in env.bundles:
            for s in b.sections:
                sid = await database.resolve_section_id(
                    term, s.course_code, s.section_number
                )
                if sid is not None:
                    section_ids.append(sid)
    details = await database.get_sections_details(section_ids)

    for env in parsed:
        env_verify = EnvelopeVerify(slug=env.slug, display_title=env.display_title)
        report.envelopes.append(env_verify)

        for idx, bundle in enumerate(env.bundles):
            bundle_verify = BundleVerify(bundle_index=idx, env_id=bundle.env_id)
            env_verify.bundles.append(bundle_verify)

            for sec in bundle.sections:
                sv = SectionVerify(
                    course_code=sec.course_code, section_number=sec.section_number,
                    status="ok",
                )
                bundle_verify.sections.append(sv)

                section_id = await database.resolve_section_id(
                    term, sec.course_code, sec.section_number
                )
                if section_id is None:
                    sv.status = "missing"
                    report.problems.append(
                        f"{env.display_title} bundle {idx + 1}: "
                        f"{sec.course_code} {sec.section_number} is not in the DB"
                    )
                    continue

                actual = details.get(section_id)
                status, differences, warnings = _compare_section(
                    sec, sec.instructor_snapshot, actual
                )
                sv.status = status
                sv.differences.extend(differences)
                sv.warnings.extend(warnings)

                if sv.status == "missing":
                    report.problems.append(
                        f"{env.display_title} bundle {idx + 1}: "
                        f"{sec.course_code} {sec.section_number} section "
                        f"{section_id} not found in DB"
                    )
                elif sv.status != "ok":
                    report.problems.append(
                        f"{env.display_title} bundle {idx + 1}: "
                        f"{sec.course_code} {sec.section_number}: "
                        + "; ".join(sv.differences)
                    )
                for w in sv.warnings:
                    line = (
                        f"{env.display_title} bundle {idx + 1}: "
                        f"{sec.course_code} {sec.section_number}: {w}"
                    )
                    report.warnings.append(line)
                    env_verify.warnings.append(line)

            bundle_verify.warnings = [
                w for sv in bundle_verify.sections for w in sv.warnings
            ]
            statuses = [sv.status for sv in bundle_verify.sections]
            if "missing" in statuses:
                bundle_verify.status = "missing"
                report.missing_bundles += 1
            elif "mismatch" in statuses:
                bundle_verify.status = "mismatch"
                report.mismatch_bundles += 1
            else:
                bundle_verify.status = "ok"
                report.ok_bundles += 1

            if bundle_verify.status == "ok":
                env_verify.status = env_verify.status or "ok"
            elif bundle_verify.status == "mismatch" and env_verify.status != "missing":
                env_verify.status = "mismatch"
            elif bundle_verify.status == "missing":
                env_verify.status = "missing"

    return report


async def save_parsed_envelopes(term: str, envs: list[Envelope]) -> None:
    """Persist parsed envelopes into the DB (resolve section ids + upsert)."""
    for env in envs:
        bundles: list[dict] = []
        for b in env.bundles:
            sections: list[dict] = []
            for s in b.sections:
                section_id = await database.resolve_section_id(
                    term, s.course_code, s.section_number
                )
                sections.append({
                    "section_id": section_id,
                    "course_code": s.course_code,
                    "section_number": s.section_number,
                    "instructor_snapshot": s.instructor_snapshot,
                    "mode": s.mode,
                    "credits": s.credits,
                    "part_of_term": s.part_of_term,
                    "note_flag": s.note_flag,
                })
            bundles.append({
                "env_id": b.env_id,
                "capacity_snapshot": b.capacity_snapshot,
                "seats_snapshot": b.seats_snapshot,
                "sections": sections,
            })
        await database.replace_envelope(
            term, env.slug, env.title, env.display_title, env.captured_at, bundles
        )

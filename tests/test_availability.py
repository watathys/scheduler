"""Tests for the shared availability core. No network; uses temp SQLite DB."""

import asyncio
import os
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

# Isolate DB before importing backend modules
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DB_PATH"] = _tmp.name

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import availability  # noqa: E402
from backend import database  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.scraper import TimeSlot  # noqa: E402


@pytest.fixture()
def db():
    async def _seed():
        await database.init_db()
        now = datetime.now().isoformat()
        # One building, two rooms; one booked 15:00-16:00 on Mondays
        await database.upsert_room("HBLL", "2214", None, None, now)
        await database.upsert_room("HBLL", "2215", None, None, now)
        await database.bulk_upsert_schedules("HBLL", "2214", settings.current_term, [
            TimeSlot(course="C 101", section="001", sec_type="LEC", days_raw="M",
                     start_time="15:00", end_time="16:00",
                     begin_date="", end_date="", instructor=""),
        ])
    asyncio.run(_seed())
    return _tmp.name


def test_bad_params_raise_400_style_errors(db):
    with pytest.raises(availability.AvailabilityError):
        asyncio.run(availability.find_open_rooms(date="09-22", start_time="15:00"))
    with pytest.raises(availability.AvailabilityError):
        asyncio.run(availability.find_open_rooms(date="2026-09-22", start_time="3pm"))
    with pytest.raises(availability.AvailabilityError):
        asyncio.run(availability.find_open_rooms(date="2026-09-22", start_time="15:00",
                                                 duration_minutes=5))
    with pytest.raises(availability.AvailabilityError):
        asyncio.run(availability.find_open_rooms(date="2026-09-22", start_time="",
                                                 duration_minutes=60))
    with pytest.raises(availability.AvailabilityError):
        asyncio.run(availability.find_open_rooms(date="2026-09-22", start_time="15:00",
                                                 building="NOPE"))


def test_weekday_mapping_and_open_windows(db):
    # 2026-09-22 is a Tuesday -> booked Monday slot does NOT apply; both rooms free
    res = asyncio.run(availability.find_open_rooms(date="2026-09-22", start_time="15:00",
                                                   duration_minutes=60, building="HBLL"))
    assert res["timezone"] == "America/Denver"
    assert res["weekday"] == "Tuesday"
    assert len(res["rooms"]) == 2
    assert res["rooms"][0]["open_windows"][0]["start"] == "2026-09-22T15:00:00-06:00"
    assert "as_of" in res


def test_partially_booked_monday(db):
    # Monday: 2214 booked 15:00-16:00, so 15:00/60 -> only 2215 fully free
    res = asyncio.run(availability.find_open_rooms(date="2026-09-21", start_time="15:00",
                                                   duration_minutes=60, building="HBLL"))
    assert res["weekday"] == "Monday"
    ids = [r["id"] for r in res["rooms"]]
    assert "HBLL:2215" in ids
    assert "HBLL:2214" not in ids
    # Shorter window inside free time finds both
    res2 = asyncio.run(availability.find_open_rooms(date="2026-09-21", start_time="16:00",
                                                    duration_minutes=60, building="HBLL"))
    assert len(res2["rooms"]) == 2


def test_amenities_filter_matches_nothing(db):
    res = asyncio.run(availability.find_open_rooms(date="2026-09-22", start_time="15:00",
                                                   amenities="whiteboard", building="HBLL"))
    assert res["rooms"] == []
    assert "note" in res


def test_group_size_keeps_unknown_capacity(db):
    res = asyncio.run(availability.find_open_rooms(date="2026-09-22", start_time="15:00",
                                                   group_size=4, building="HBLL"))
    assert len(res["rooms"]) == 2


def test_empty_results_when_nothing_cached():
    res = asyncio.run(availability.find_open_rooms(date="2026-09-22", start_time="15:00",
                                                   building="JKB"))
    assert res["rooms"] == []


def test_room_details_and_booking_link(db):
    d = asyncio.run(availability.get_room_details("HBLL:2214", date="2026-09-22"))
    assert d["id"] == "HBLL:2214"
    assert d["booking_url"].startswith("https://")
    assert d["requires_login"] is False
    with pytest.raises(availability.AvailabilityError):
        asyncio.run(availability.get_room_details("bogus"))

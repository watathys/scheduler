from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional

VALID_DAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"}


class BuildingInfo(BaseModel):
    code: str
    display_name: str
    rooms_cached: int
    last_crawled: Optional[str] = None


class RoomInfo(BaseModel):
    building: str
    room_number: str
    description: Optional[str] = None
    capacity: Optional[int] = None
    discovered_at: str


class SearchRequest(BaseModel):
    buildings: list[str] = Field(..., min_length=1)
    day: str
    start_time: str  # "HH:MM" 24-hr
    end_time: str    # "HH:MM" 24-hr


class FreeWindow(BaseModel):
    start_time: str       # "HH:MM" 24-hr
    end_time: str         # "HH:MM" 24-hr
    duration_minutes: int


class FreeRoom(BaseModel):
    building: str
    room_number: str
    description: Optional[str] = None
    capacity: Optional[int] = None
    is_fully_free: bool
    free_windows: list[FreeWindow]
    longest_free_minutes: int


class SearchResponse(BaseModel):
    rooms: list[FreeRoom]
    searched_buildings: list[str]
    day: str
    start_time: str
    end_time: str
    total_rooms_checked: int


class DiscoveryStatus(BaseModel):
    building: str
    status: str  # 'running' | 'done' | 'error' | 'not_started'
    rooms_found: int
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class CacheStats(BaseModel):
    total_buildings_crawled: int
    total_rooms_cached: int
    buildings: list[DiscoveryStatus]


# ── Schedule builder: solver request/response models ─────────────────────────

VALID_PREFERENCES = {"easy", "days_off", "no_gaps"}


class BlockedTime(BaseModel):
    day: str
    start_min: int
    end_min: int


class SolverRequest(BaseModel):
    term: str
    courses: list[str] = Field(..., min_length=1)
    preferences: list[str]
    days_off: list[str] = Field(default_factory=lambda: ["Friday"])
    earliest_start_min: int = 0
    latest_end_min: int = 1440
    allow_online: bool = False
    blocked_times: list[BlockedTime] = Field(default_factory=list)
    skip_full_sections: bool = True

    @field_validator("preferences")
    @classmethod
    def _validate_preferences(cls, v: list[str]) -> list[str]:
        if len(v) != 3:
            raise ValueError("preferences must contain exactly 3 values (ranked 1..3)")
        if len(set(v)) != 3:
            raise ValueError("preferences must be 3 distinct values")
        bad = [p for p in v if p not in VALID_PREFERENCES]
        if bad:
            raise ValueError(f"unknown preference(s): {bad} (allowed: {sorted(VALID_PREFERENCES)})")
        return v

    @model_validator(mode="after")
    def _validate_window(self):
        if self.earliest_start_min > self.latest_end_min:
            raise ValueError("earliest_start_min must be <= latest_end_min")
        return self


class MeetingOut(BaseModel):
    day: str
    start_min: int
    end_min: int
    building: Optional[str] = None
    room: Optional[str] = None


class SectionOut(BaseModel):
    id: int
    course_code: str
    section_number: str
    title: Optional[str] = None
    is_online: bool
    seats_open: Optional[int] = None
    instructor_name: Optional[str] = None
    ease_score: float
    ease_confidence: float
    meetings: list[MeetingOut] = Field(default_factory=list)


class ScheduleMetrics(BaseModel):
    days_on_campus: int
    total_gap_minutes: int
    average_ease: float
    earliest_start: Optional[int] = None
    latest_end: Optional[int] = None
    professors_without_ratings: int


class ScheduleOut(BaseModel):
    sections: list[SectionOut]
    async_online: list[SectionOut]
    total_score: float
    metrics: ScheduleMetrics


class CourseFilterReport(BaseModel):
    course: str
    removed_by: dict[str, int]


class BottleneckDiagnosis(BaseModel):
    no_sections: list[CourseFilterReport] = Field(default_factory=list)
    feasible_relaxations: list[str] = Field(default_factory=list)
    always_conflicting_pairs: list[tuple[str, str]] = Field(default_factory=list)
    message: Optional[str] = None


class SolverResponse(BaseModel):
    schedules: list[ScheduleOut]
    cap_hit: bool = False
    bottleneck: Optional[BottleneckDiagnosis] = None


class ParsedConstraints(BaseModel):
    """Result of turning free-text constraints into structured blocked times."""
    blocked_times: list[BlockedTime] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    warning: Optional[str] = None


# ── Scheduler API (Step 5) request/response models ───────────────────────────

_VALID_OFF_DAYS = {
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
}


def parse_hhmm_to_minutes(value: str) -> int:
    """``"08:00"`` / ``"8:00"`` / ``"0800"`` -> minutes since midnight.

    Raises ``ValueError`` on malformed input so Pydantic can map it to a 422.
    """
    digits = "".join(ch for ch in value if ch.isdigit())
    if not digits or len(digits) > 4:
        raise ValueError(f"invalid time {value!r} (expected HH:MM)")
    digits = digits.zfill(4)
    hour, minute = int(digits[:2]), int(digits[2:])
    if hour > 23 or minute > 59:
        raise ValueError(f"invalid time {value!r} (expected HH:MM)")
    return hour * 60 + minute


class GenerateRequest(BaseModel):
    courses: list[str] = Field(..., min_length=1, max_length=8)
    preferences: list[str]
    days_off: list[str] = Field(default_factory=lambda: ["Friday"])
    earliest_start: str = "08:00"
    latest_end: str = "22:00"
    allow_online: bool = False
    extra_constraints_text: Optional[str] = Field(default=None, max_length=500)

    @field_validator("preferences")
    @classmethod
    def _validate_preferences(cls, v: list[str]) -> list[str]:
        if len(v) != 3:
            raise ValueError("preferences must contain exactly 3 values (ranked 1..3)")
        if len(set(v)) != 3:
            raise ValueError("preferences must be 3 distinct values")
        bad = [p for p in v if p not in VALID_PREFERENCES]
        if bad:
            raise ValueError(f"unknown preference(s): {bad} (allowed: {sorted(VALID_PREFERENCES)})")
        return v

    @field_validator("days_off")
    @classmethod
    def _validate_days_off(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in v:
            day = raw.strip().title()
            if day not in _VALID_OFF_DAYS:
                raise ValueError(f"unknown day {raw!r} (use a full weekday name)")
            if day not in seen:
                seen.add(day)
                out.append(day)
        return out

    @field_validator("earliest_start", "latest_end")
    @classmethod
    def _validate_time(cls, v: str) -> str:
        parse_hhmm_to_minutes(v)
        return v

    @model_validator(mode="after")
    def _validate_window(self):
        if parse_hhmm_to_minutes(self.earliest_start) > parse_hhmm_to_minutes(self.latest_end):
            raise ValueError("earliest_start must be <= latest_end")
        return self

    @property
    def earliest_start_min(self) -> int:
        return parse_hhmm_to_minutes(self.earliest_start)

    @property
    def latest_end_min(self) -> int:
        return parse_hhmm_to_minutes(self.latest_end)


class CourseSearchResult(BaseModel):
    code: str
    title: Optional[str] = None
    credits: Optional[float] = None
    section_count: int


class GeneratedSchedule(ScheduleOut):
    explanation: str = ""


class GenerateResponse(BaseModel):
    status: str  # "ok" | "no_schedule"
    term: str
    schedules: list[GeneratedSchedule] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    cap_hit: bool = False
    bottleneck: Optional[BottleneckDiagnosis] = None


class SchedulerStatusOut(BaseModel):
    status: str  # "ready" | "loading"
    term: str
    section_count: int
    meeting_count: int
    professor_count: int
    professors_with_ratings: int
    last_scrape_time: Optional[str] = None
    last_rmp_refresh_time: Optional[str] = None
    llm_configured: bool

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Literal, Optional


# ── Schedule builder: solver request/response models ─────────────────────────

VALID_PREFERENCES = {"easy", "no_gaps", "fewer_days"}
PREFERENCE_ORDER = ["easy", "no_gaps", "fewer_days"]


def normalize_preferences(v: list[str]) -> list[str]:
    """Validate a ranked preference list; any omitted preference is appended
    at the lowest ranks so older 2-item clients keep working."""
    if len(set(v)) != len(v):
        raise ValueError("preferences must be distinct values")
    bad = [p for p in v if p not in VALID_PREFERENCES]
    if bad:
        raise ValueError(f"unknown preference(s): {bad} (allowed: {sorted(VALID_PREFERENCES)})")
    if len(v) < 2:
        raise ValueError("preferences must contain 2 or 3 ranked values")
    return list(v) + [p for p in PREFERENCE_ORDER if p not in v]


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
        return normalize_preferences(v)

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


class ProfessorBreakdown(BaseModel):
    """Per-instructor breakdown for the "why ease is N" panel.

    ``data_status`` gates what numbers we are willing to show a student:

    * ``"rated"``   — a confident RMP match with >= 3 ratings; show all numbers.
    * ``"limited"`` — a confident match but fewer than 3 ratings; numbers are
      real but thin.
    * ``"none"``    — no confident RMP match; every numeric field is ``null``.
    """
    display_name: str
    courses: list[str] = Field(default_factory=list)
    avg_rating: Optional[float] = None
    avg_difficulty: Optional[float] = None
    would_take_again_percent: Optional[float] = None
    num_ratings: Optional[int] = None
    rmp_url: Optional[str] = None
    ease_score: Optional[float] = None
    ease_confidence: Optional[float] = None
    data_status: Literal["rated", "limited", "none"]


class ScheduleMetrics(BaseModel):
    days_on_campus: int
    total_gap_minutes: int
    # None when no instructor has RateMyProfessors data, so ease cannot be
    # calculated for this schedule.
    average_ease: Optional[float] = None
    earliest_start: Optional[int] = None
    latest_end: Optional[int] = None
    professors_without_ratings: int
    ease_breakdown: list[ProfessorBreakdown] = Field(default_factory=list)


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
        return normalize_preferences(v)

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

"""Pydantic request/response schemas for the Mini App API (SPEC §20)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    conint,
    field_validator,
    model_validator,
)

from assistant.i18n import is_supported
from assistant.models.calendar_items import ItemKind, ItemPriority
from assistant.services.reminders import (
    MAX_OFFSET_MINUTES,
    MAX_REMINDERS_PER_ITEM,
    MIN_OFFSET_MINUTES,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserOut(ORMModel):
    id: int
    first_name: str
    last_name: str | None
    username: str | None


class SettingsOut(ORMModel):
    timezone: str
    digest_time: time
    motivation_enabled: bool
    language: str


class SettingsUpdate(BaseModel):
    timezone: str | None = None
    digest_time: time | None = None
    motivation_enabled: bool | None = None
    language: str | None = None

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except (ValueError, KeyError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @field_validator("language")
    @classmethod
    def _valid_language(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not is_supported(value):
            raise ValueError(f"unsupported language {value!r}")
        return value


class MeOut(BaseModel):
    user: UserOut
    settings: SettingsOut


class ItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    kind: ItemKind = ItemKind.task
    description: str | None = Field(default=None, max_length=4000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    due_at: datetime | None = None
    priority: ItemPriority = ItemPriority.normal
    # SPEC §14.2: the pydantic constraint is the API's first gate; the
    # shared service validation re-checks the same bounds for every path.
    remind_offsets_minutes: list[
        conint(ge=MIN_OFFSET_MINUTES, le=MAX_OFFSET_MINUTES)
    ] = Field(default_factory=list, max_length=MAX_REMINDERS_PER_ITEM)


class ItemUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=4000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    due_at: datetime | None = None
    priority: ItemPriority | None = None


class ItemOut(ORMModel):
    id: int
    kind: str
    title: str
    description: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    due_at: datetime | None
    status: str
    priority: str
    source: str
    extra: dict
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class WorkoutCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    started_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, gt=0)
    notes: str | None = Field(default=None, max_length=4000)
    perceived_effort: int | None = Field(default=None, ge=1, le=10)


class WorkoutOut(ORMModel):
    id: int
    name: str
    started_at: datetime
    duration_minutes: int | None
    notes: str | None
    perceived_effort: int | None
    status: str
    created_at: datetime


class WorkoutStatsOut(BaseModel):
    total: int
    total_minutes: int
    this_week: int
    current_streak: int
    longest_streak: int
    last_date: date | None


class FileOut(ORMModel):
    id: int
    original_filename: str
    mime_type: str
    size_bytes: int | None
    state: str
    error: str | None
    created_at: datetime
    indexed_at: datetime | None


class SearchResultOut(ORMModel):
    file_id: int
    file_name: str
    position: int
    text: str
    score: float


class ReminderCreate(BaseModel):
    fire_at: datetime
    message: str = Field(min_length=1, max_length=1000)


class ReminderOut(ORMModel):
    id: int
    fire_at: datetime
    message: str
    status: str
    calendar_item_id: int | None = None
    offset_minutes: int | None = None
    created_at: datetime


class FactCreate(BaseModel):
    value: str = Field(min_length=1, max_length=2000)
    category: str | None = Field(default=None, max_length=64)
    confidence: float | None = Field(default=None, ge=0, le=1)


class FactSupersede(BaseModel):
    value: str = Field(min_length=1, max_length=2000)
    category: str | None = Field(default=None, max_length=64)


class ActionOut(ORMModel):
    id: int
    kind: str
    summary: str
    status: str
    payload: dict
    last_result: dict | None
    last_error: str | None
    created_at: datetime
    expires_at: datetime | None
    confirmed_at: datetime | None
    rejected_at: datetime | None
    executed_at: datetime | None
    expired_at: datetime | None

    @model_validator(mode="after")
    def _report_effective_status(self) -> ActionOut:
        # Read paths do not persist expiry (SPEC §3): report an overdue
        # proposed/confirmed action as ``expired`` at the API boundary so the
        # UI reflects reality without the read writing to the DB.
        if (
            self.status in ("proposed", "confirmed")
            and self.expires_at is not None
            and self.expires_at <= datetime.now(UTC)
        ):
            self.status = "expired"
        return self


class ProactiveSettingsOut(ORMModel):
    enabled: bool
    weekly_review_enabled: bool
    workout_nudge_enabled: bool
    overdue_nudge_enabled: bool
    quiet_hours_start: time
    quiet_hours_end: time
    max_nudges_per_day: int
    min_interval_minutes: int


class ProactiveSettingsUpdate(BaseModel):
    enabled: bool | None = None
    weekly_review_enabled: bool | None = None
    workout_nudge_enabled: bool | None = None
    overdue_nudge_enabled: bool | None = None
    quiet_hours_start: time | None = None
    quiet_hours_end: time | None = None
    max_nudges_per_day: int | None = Field(default=None, ge=1, le=20)
    min_interval_minutes: int | None = Field(default=None, ge=0, le=1440)


class WorkoutSchedule(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    starts_at: datetime
    duration_minutes: int | None = Field(default=None, gt=0)


class FactOut(ORMModel):
    id: int
    category: str
    value: str
    provenance: str | None
    confidence: float | None
    status: str
    replaces_fact_id: int | None
    superseded_by: int | None
    created_at: datetime



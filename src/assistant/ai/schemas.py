"""Pydantic schemas for model-produced structured output (SPEC §6, §15)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AITaskDraft(BaseModel):
    """A task/event draft produced by the model from natural language.

    This is *not* written to the database directly: it is rendered as a
    human-readable preview and only persisted after explicit user
    confirmation (SPEC §6).
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    kind: Literal["task", "event"] = "task"
    priority: Literal["low", "normal", "high"] = "normal"
    # Naive local time in the user's timezone; resolved to UTC at the call site.
    start: datetime | None = None
    duration_minutes: int | None = Field(default=None, ge=1, le=48 * 60)
    notes: str | None = Field(default=None, max_length=4000)
    # Minutes relative to the start (0 = at start, negative = after).
    reminder_offsets: list[int] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    # Human-readable notes about anything the model had to guess.
    ambiguities: list[str] = Field(default_factory=list)

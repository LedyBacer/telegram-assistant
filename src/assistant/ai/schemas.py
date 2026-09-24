"""Pydantic schemas for model-produced structured output (SPEC §3, §6, §15)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, conint, model_validator

from assistant.services.reminders import (
    MAX_OFFSET_MINUTES,
    MAX_REMINDERS_PER_ITEM,
    MIN_OFFSET_MINUTES,
)


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
    # Minutes relative to the start (0 = at start, negative = after),
    # within the shared SPEC §14.2 bounds.
    reminder_offsets: list[conint(ge=MIN_OFFSET_MINUTES, le=MAX_OFFSET_MINUTES)] = Field(
        default_factory=list, max_length=MAX_REMINDERS_PER_ITEM
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    # Human-readable notes about anything the model had to guess.
    ambiguities: list[str] = Field(default_factory=list)


class ReadToolRequest(BaseModel):
    """A request for the model to fetch app data through a bounded read tool.

    Read tools are deterministic lookups over existing services (calendar,
    reminders, workouts, files, facts). The engine enforces the bound and
    never loops (SPEC §2, §3).
    """

    model_config = ConfigDict(extra="forbid")

    tool: Literal[
        "calendar", "reminders", "workouts", "files", "documents", "facts"
    ]
    query: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=5, ge=1, le=20)


class ActionProposal(BaseModel):
    """A proposed mutation the user must confirm before it is executed.

    The engine re-validates ``payload`` against the registered kind's schema
    and only then stores it as a durable ``PendingAction`` (SPEC §3). The
    model never applies a mutation directly.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    # Short human-readable description shown to the user for confirmation.
    summary: str = Field(min_length=1, max_length=200)


class FactProposal(BaseModel):
    """A candidate long-term memory fact the user must confirm (SPEC §14).

    Never stored as trusted context until confirmed; the engine dedupes
    against the user's existing facts (including rejected ones) before
    persisting a ``proposed`` row.
    """

    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=2000)
    category: str = Field(default="general", max_length=64)
    # Optional reference to an existing confirmed-fact id from memory context
    # (SPEC §16): the model marks this fact as an update/replacement. The
    # engine revalidates ownership and state before linking it.
    replaces_fact_id: int | None = None


class AssistantTurn(BaseModel):
    """The typed assistant-turn protocol (SPEC §3, §11).

    A single model request returns one of these shapes (fields combined as
    needed): a direct ``reply``, ``data_requests`` to pull app data,
    ``actions`` to propose mutations, or a ``clarification`` question when
    the request is ambiguous. At most one extra model call is made (to fold
    tool results into a final reply), so the turn is bounded and never runs
    an unbounded ReAct loop (SPEC §2).

    Mutually exclusive modes are enforced in Python, not left to the prompt
    (V3 §8): a turn that asks for clarification must not also propose
    mutations — such contradictory output fails validation, giving the
    provider's repair loop one chance to self-correct.
    """

    model_config = ConfigDict(extra="forbid")

    reply: str | None = Field(default=None, max_length=4000)
    data_requests: list[ReadToolRequest] = Field(default_factory=list, max_length=4)
    actions: list[ActionProposal] = Field(default_factory=list, max_length=3)
    facts: list[FactProposal] = Field(default_factory=list, max_length=3)
    clarification: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _modes_are_consistent(self) -> AssistantTurn:
        if (
            self.clarification
            and self.clarification.strip()
            and self.actions
        ):
            raise ValueError(
                "a clarification must not propose actions simultaneously"
            )
        return self

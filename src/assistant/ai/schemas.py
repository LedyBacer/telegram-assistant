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
    # Calendar-only typed parameters (V4 §24). Strongly typed (not free text)
    # so a 9B model can answer date/attribute questions — "what events do I
    # have this month?", "show my high-priority tasks" — without arbitrary SQL
    # and without an unbounded tool loop. Ignored by every other tool.
    range_start: str | None = Field(default=None, max_length=10)  # YYYY-MM-DD
    range_end: str | None = Field(default=None, max_length=10)  # YYYY-MM-DD
    priority: Literal["low", "normal", "high"] | None = None
    kind: Literal["task", "event", "workout"] | None = None

    @model_validator(mode="after")
    def _calendar_params_are_calendar_only(self) -> ReadToolRequest:
        if self.tool != "calendar" and any(
            v is not None
            for v in (self.range_start, self.range_end, self.priority, self.kind)
        ):
            raise ValueError(
                "range_start, range_end, priority, and kind are only valid "
                "for the calendar tool"
            )
        return self


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

    A single structured model request returns exactly ONE mode (tagged
    union), selected by the required ``mode`` field:

    - ``answer``: a non-blank ``reply`` (plus optional ``facts``);
    - ``need_data``: one or more ``data_requests`` (no ``actions``, no
      ``reply``, no ``facts``);
    - ``proposal``: one or more ``actions`` (plus optional ``reply`` and
      ``facts``);
    - ``clarification``: a non-blank ``clarification`` question and nothing
      else.

    ``mode`` is the discriminator and every cross-mode combination is
    rejected in Python, not left to the prompt (V4 §12-13): a turn can never
    both answer and ask for data, ask for data and propose mutations, or
    clarify and propose mutations. A contradictory combination fails
    validation, giving the provider's repair loop one chance to
    self-correct. At most one extra model call is made (to fold tool results
    into a final reply via :class:`AssistantFold`), so the turn is bounded
    and never runs an unbounded ReAct loop (SPEC §2).
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["answer", "need_data", "proposal", "clarification"]
    reply: str | None = Field(default=None, max_length=4000)
    data_requests: list[ReadToolRequest] = Field(default_factory=list, max_length=4)
    actions: list[ActionProposal] = Field(default_factory=list, max_length=3)
    facts: list[FactProposal] = Field(default_factory=list, max_length=3)
    clarification: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _mode_is_consistent(self) -> AssistantTurn:
        reply = bool(self.reply and self.reply.strip())
        clar = bool(self.clarification and self.clarification.strip())
        if self.mode == "answer":
            if not reply:
                raise ValueError("answer mode requires a non-blank reply")
            if self.data_requests or self.actions or clar:
                raise ValueError(
                    "answer mode must not carry data_requests, actions, "
                    "or clarification"
                )
        elif self.mode == "need_data":
            if not self.data_requests:
                raise ValueError(
                    "need_data mode requires at least one data_request"
                )
            if self.actions or reply or self.facts:
                raise ValueError(
                    "need_data mode must not carry actions, reply, or facts"
                )
        elif self.mode == "proposal":
            if not self.actions:
                raise ValueError("proposal mode requires at least one action")
            if self.data_requests or clar:
                raise ValueError(
                    "proposal mode must not carry data_requests "
                    "or clarification"
                )
        else:  # clarification
            if not clar:
                raise ValueError(
                    "clarification mode requires a non-blank question"
                )
            if reply or self.actions or self.data_requests or self.facts:
                raise ValueError("clarification mode must not carry other fields")
        return self


class AssistantFold(BaseModel):
    """The typed fold call: the final word after bounded read tools ran
    (SPEC §2, §3, §11; V4 §14-15).

    Same shape as :class:`AssistantTurn` minus the ``need_data`` mode. The
    fold is the LAST model call of the turn, so its schema structurally
    cannot request more data — a fold can never start a third call (no ReAct
    loop). Exactly one of ``answer`` / ``proposal`` / ``clarification`` is
    active, with the same mode-disjoint guarantees.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["answer", "proposal", "clarification"]
    reply: str | None = Field(default=None, max_length=4000)
    actions: list[ActionProposal] = Field(default_factory=list, max_length=3)
    facts: list[FactProposal] = Field(default_factory=list, max_length=3)
    clarification: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _mode_is_consistent(self) -> AssistantFold:
        reply = bool(self.reply and self.reply.strip())
        clar = bool(self.clarification and self.clarification.strip())
        if self.mode == "answer":
            if not reply:
                raise ValueError("answer mode requires a non-blank reply")
            if self.actions or clar:
                raise ValueError(
                    "answer mode must not carry actions or clarification"
                )
        elif self.mode == "proposal":
            if not self.actions:
                raise ValueError("proposal mode requires at least one action")
            if clar:
                raise ValueError(
                    "proposal mode must not carry a clarification"
                )
        else:  # clarification
            if not clar:
                raise ValueError(
                    "clarification mode requires a non-blank question"
                )
            if reply or self.actions or self.facts:
                raise ValueError("clarification mode must not carry other fields")
        return self

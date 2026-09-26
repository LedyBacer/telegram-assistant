"""Pydantic schemas for model-produced structured output (SPEC §3, §6, §15)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    conint,
    model_validator,
)

from assistant.ai.structured_events import count_structured_event
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


def _validate_action_payloads(actions: list[ActionProposal]) -> None:
    """Validate action payloads at schema time, not at Phase-C persist time.

    The engine re-validates each payload against the registered kind's
    schema when storing a ``PendingAction`` and SILENTLY SKIPS a mismatch —
    leaving a turn that proposes in its reply while nothing is storable.
    Failing here instead feeds the provider's repair loop one corrective
    retry with the exact validation error, and rejects invented action
    kinds (e.g. "update_fact") the same way. The engine keeps its own
    re-validation as the authority; this makes first-attempt payloads
    measurably better (V5.3 live-LLM eval).
    """
    # Lazy import: assistant.actions self-registers the built-in kinds and
    # importing it at module load would cycle through assistant.services.
    from assistant.actions import get_action_kind, registered_kinds  # noqa: PLC0415

    for action in actions:
        spec = get_action_kind(action.kind)
        if spec is None:
            raise ValueError(
                f"unknown action kind {action.kind!r}; "
                f"allowed kinds: {registered_kinds()}"
            )
        try:
            spec.payload_schema.model_validate(action.payload)
        except ValidationError as exc:
            raise ValueError(
                f"payload for action kind {action.kind!r} is invalid: "
                f"{exc.errors()[:3]}"
            ) from exc


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


def _infer_missing_mode(data: Any, *, schema: str = "AssistantTurn") -> Any:
    """Fill the ``mode`` discriminator from the single populated field.

    The live model frequently omits the redundant ``mode`` key when the
    populated field already determines the mode — e.g. it emits
    ``{"clarification": "..."}`` for a clarifying question. Requiring the
    discriminator in that case rejects a perfectly valid turn, so infer it
    when (and only when) exactly one mode's field is populated and no
    explicit ``mode`` was given. A genuinely empty payload still fails the
    required-field check downstream.

    Also normalizes four shapes the 9B model emits repeatedly (V5.3
    live-LLM eval), each of which previously died after both repair
    attempts:

    * a stray TOP-LEVEL ``replaces_fact_id`` (it belongs inside the fact
      entry) is moved into the first fact entry;
    * a ``"proposal"`` carrying facts but no actions is re-labelled
      ``"answer"`` (a zero-action proposal is a facts answer; any
      lifted-out top-level ``"summary"`` is dropped with it);
    * a BARE read-tool request (``data_requests[0]`` flattened to the top
      level, no envelope) is wrapped into ``need_data`` — a top-level
      ``"tool"`` can never be a valid turn field, so the wrap is
      unambiguous;
    * an echoed ``"id"`` INSIDE a fact entry (the old fact's id) is mapped
      onto that entry's ``"replaces_fact_id"`` — a new fact has no id of
      its own, so an id there can only refer to the fact being replaced.

    Every normalization increments a safe process-wide counter and logs a
    fixed-name event (V5.4 §22); only the schema class name is logged,
    never the payload content.
    """
    if not isinstance(data, dict):
        return data
    # The model sometimes returns a BARE read-tool request
    # ({"tool": ..., "query": ..., "limit": ...}) — data_requests[0]
    # flattened to the top level, no envelope. A top-level "tool" can never
    # be a valid turn field, so wrap it unambiguously (V5.3 live-LLM eval).
    if "tool" in data and not data.get("mode"):
        data = {"mode": "need_data", "data_requests": [data]}
        count_structured_event("structured_bare_tool_wrapped", schema=schema)
    if not data.get("mode"):
        mode = next(
            (
                inferred
                for field, inferred in (
                    ("clarification", "clarification"),
                    ("actions", "proposal"),
                    ("data_requests", "need_data"),
                    ("reply", "answer"),
                )
                if data.get(field)
            ),
            None,
        )
        if mode is not None:
            data["mode"] = mode
            count_structured_event(
                "structured_missing_mode_inferred", schema=schema
            )
    # The model sometimes echoes the old fact's "id" INSIDE the new fact
    # entry (V5.3 live-LLM eval): a new fact has no id, so it can only be
    # the existing fact this one replaces — map it onto
    # "replaces_fact_id" (or drop it when that is already present).
    if isinstance(data, dict) and data.get("facts"):
        for fact in data["facts"]:
            if isinstance(fact, dict) and "id" in fact:
                if fact.get("replaces_fact_id") is None:
                    fact["replaces_fact_id"] = fact.pop("id")
                else:
                    fact.pop("id")
                count_structured_event("structured_fact_id_normalized", schema=schema)
    # The 9B model sometimes emits "replaces_fact_id" as a TOP-LEVEL key next
    # to "facts" instead of inside a fact entry (V5.3 live-LLM eval), which
    # fails extra="forbid" and is repeated on the repair attempt, killing the
    # turn. Normalize: move it into the first fact entry that lacks one.
    if (
        isinstance(data, dict)
        and data.get("replaces_fact_id") is not None
        and data.get("facts")
    ):
        for fact in data["facts"]:
            if isinstance(fact, dict) and fact.get("replaces_fact_id") is None:
                fact["replaces_fact_id"] = data["replaces_fact_id"]
                break
        data.pop("replaces_fact_id")
        count_structured_event(
            "structured_top_level_replaces_fact_id_normalized", schema=schema
        )
    # The model sometimes labels a FACT proposal as "proposal" with no
    # actions (and occasionally a lifted-out top-level "summary"): a
    # proposal with zero actions is an answer carrying facts, and facts in
    # answer mode are valid — re-label it (V5.3 live-LLM eval).
    if (
        isinstance(data, dict)
        and data.get("mode") == "proposal"
        and not data.get("actions")
        and data.get("facts")
    ):
        data["mode"] = "answer"
        data.pop("summary", None)
        count_structured_event(
            "structured_facts_only_proposal_normalized", schema=schema
        )
    return data


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

    @model_validator(mode="before")
    @classmethod
    def _fill_mode(cls, data: Any) -> Any:
        return _infer_missing_mode(data, schema=cls.__name__)

    @model_validator(mode="after")
    def _mode_is_consistent(self) -> AssistantTurn:
        _validate_action_payloads(self.actions)
        reply = bool(self.reply and self.reply.strip())
        clar = bool(self.clarification and self.clarification.strip())
        if self.mode == "answer":
            # A facts-only answer (no prose) is valid: the bot renders each
            # proposed fact with its confirm keyboard even when the reply
            # text is empty (V5.3 live-LLM eval — the 9B model emits exactly
            # this shape for "remember that ..." turns, and the engine +
            # bot handle the blank reply).
            if not (reply or self.facts):
                raise ValueError(
                    "answer mode requires a non-blank reply "
                    "or at least one fact"
                )
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

    @model_validator(mode="before")
    @classmethod
    def _fill_mode(cls, data: Any) -> Any:
        return _infer_missing_mode(data, schema=cls.__name__)

    @model_validator(mode="after")
    def _mode_is_consistent(self) -> AssistantFold:
        _validate_action_payloads(self.actions)
        reply = bool(self.reply and self.reply.strip())
        clar = bool(self.clarification and self.clarification.strip())
        if self.mode == "answer":
            # Facts-only fold answers are valid (the 9B model emits exactly
            # this shape for fact-replacement turns; the engine persists no
            # assistant message and the bot renders the fact-confirm card —
            # V5.3 live-LLM eval).
            if not (reply or self.facts):
                raise ValueError(
                    "answer mode requires a non-blank reply "
                    "or at least one fact"
                )
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

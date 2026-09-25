"""Typed pending-action registry (SPEC §3).

An action *kind* pairs a Pydantic payload schema with an async executor.
The payload schema is used to validate the payload at proposal time and
again at execution time, so a stored row can never be executed with a
malformed payload. The executor is the only code path that mutates state
for a kind, and it must re-validate ownership and entity state itself
(entities may have changed between proposal and execution).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.models.users import User

# Executor: receives the user and the *parsed* payload model; returns a
# JSON-serializable result (dict, list, scalar, or None) that is stored on
# the action row for idempotent re-execution.
Executor = Callable[
    [AsyncSession, User, BaseModel], Awaitable[dict | list | str | int | float | None]
]

# Baseline: called at PROPOSAL time with the parsed payload; returns extra
# JSON-serializable payload fields capturing the target entity's state at that
# moment (e.g. its updated_at), so the executor can detect drift (SPEC §3).
Baseline = Callable[[AsyncSession, User, BaseModel], Awaitable[dict[str, object]]]

# Preview: called at PROPOSAL time with the parsed payload; returns a concise,
# deterministic, human-readable description of the mutation derived from the
# typed data (datetimes shown in the user's timezone). It is stored as the
# action's summary so the confirm/done text is exact and consistent (SPEC §3).
Preview = Callable[[AsyncSession, User, BaseModel], Awaitable[str]]


@dataclass(frozen=True)
class ActionKind:
    kind: str
    payload_schema: type[BaseModel]
    executor: Executor
    # Optional proposal-time baseline capture (optimistic staleness guard).
    baseline: Baseline | None = field(default=None)
    # Optional typed-data preview builder (deterministic user-facing summary).
    preview: Preview | None = field(default=None)


_REGISTRY: dict[str, ActionKind] = {}


def register_action_kind(
    kind: str,
    *,
    payload_schema: type[BaseModel],
    executor: Executor,
    baseline: Baseline | None = None,
    preview: Preview | None = None,
) -> ActionKind:
    """Register (or replace) an action kind. Idempotent for the same spec."""
    spec = ActionKind(
        kind=kind,
        payload_schema=payload_schema,
        executor=executor,
        baseline=baseline,
        preview=preview,
    )
    _REGISTRY[kind] = spec
    return spec


def get_action_kind(kind: str) -> ActionKind | None:
    return _REGISTRY.get(kind)


def registered_kinds() -> list[str]:
    return sorted(_REGISTRY)


# Built-in kinds (calendar, workout and data mutations) register themselves on
# import.
import assistant.actions.calendar  # noqa: E402,F401
import assistant.actions.data  # noqa: E402,F401
import assistant.actions.workouts  # noqa: E402,F401

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
from dataclasses import dataclass

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.models.users import User

# Executor: receives the user and the *parsed* payload model; returns a
# JSON-serializable result (dict, list, scalar, or None) that is stored on
# the action row for idempotent re-execution.
Executor = Callable[
    [AsyncSession, User, BaseModel], Awaitable[dict | list | str | int | float | None]
]


@dataclass(frozen=True)
class ActionKind:
    kind: str
    payload_schema: type[BaseModel]
    executor: Executor


_REGISTRY: dict[str, ActionKind] = {}


def register_action_kind(
    kind: str,
    *,
    payload_schema: type[BaseModel],
    executor: Executor,
) -> ActionKind:
    """Register (or replace) an action kind. Idempotent for the same spec."""
    spec = ActionKind(kind=kind, payload_schema=payload_schema, executor=executor)
    _REGISTRY[kind] = spec
    return spec


def get_action_kind(kind: str) -> ActionKind | None:
    return _REGISTRY.get(kind)


def registered_kinds() -> list[str]:
    return sorted(_REGISTRY)


# Built-in kinds (calendar mutations) register themselves on import.
import assistant.actions.calendar  # noqa: E402,F401

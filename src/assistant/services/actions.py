"""Durable pending-action lifecycle (SPEC §3).

Mutations are never applied directly: an action is **proposed** (typed,
validated payload), the user **confirms** it, and only then is it
**executed**. Every transition is persisted on the row, so the state
survives restarts. Execution is idempotent: an already-executed action
returns its stored result and is not re-applied.

At execution time the stored payload is re-validated against the kind's
Pydantic schema and the executor re-checks ownership and entity state;
a stale action (entity gone or changed) is transitioned to ``expired``
rather than executed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.actions import get_action_kind
from assistant.actions.calendar import ActionStaleError
from assistant.models.pending_actions import ActionStatus, PendingAction
from assistant.models.users import User

#: A proposed action the user never answers is no longer actionable after
#: this long (checked lazily on every access, and in bulk via
#: :func:`expire_actions` from the worker).
DEFAULT_ACTION_TTL = timedelta(hours=1)

_TERMINAL = (
    ActionStatus.rejected.value,
    ActionStatus.executed.value,
    ActionStatus.expired.value,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _is_expired_now(action: PendingAction) -> bool:
    """Pure check: a proposed/confirmed action is overdue once its TTL passes.
    Reads use this to *report* an effective status without writing."""
    return (
        action.status in (ActionStatus.proposed.value, ActionStatus.confirmed.value)
        and action.expires_at is not None
        and action.expires_at <= _now()
    )


def effective_status(action: PendingAction) -> str:
    """The status a reader should see. An overdue proposed/confirmed action is
    reported as ``expired`` WITHOUT persisting the transition — read paths must
    not mutate; the durable ``expired`` write happens only in the write paths
    (confirm/reject/execute) and the worker's bulk :func:`expire_actions`."""
    if _is_expired_now(action):
        return ActionStatus.expired.value
    return action.status


def _lazy_expire(action: PendingAction) -> bool:
    """Write-path only: transition an overdue action to ``expired``. Returns
    True if the action just expired. Callers in a write transaction commit it."""
    if _is_expired_now(action):
        action.status = ActionStatus.expired.value
        action.expired_at = _now()
        return True
    return False


async def _load_write(session: AsyncSession, user: User, action_id: int) -> PendingAction | None:
    """Load for a WRITE path, lazily expiring (mutates + flushes) an overdue
    action so the transition is committed with the caller's transaction."""
    action = await session.get(PendingAction, action_id)
    if action is None or action.user_id != user.id:
        return None
    if _lazy_expire(action):
        await session.flush()
    return action


async def propose_action(
    session: AsyncSession,
    user: User,
    *,
    kind: str,
    payload: BaseModel | dict,
    summary: str,
    expires_in: timedelta | None = None,
) -> PendingAction:
    """Create a ``proposed`` action. The payload is validated against the
    registered kind's schema (flush only; the caller commits)."""
    spec = get_action_kind(kind)
    if spec is None:
        raise ValueError(f"Unknown action kind: {kind!r}")
    if not summary.strip():
        raise ValueError("summary is required.")
    parsed = (
        spec.payload_schema.model_validate(payload)
        if isinstance(payload, dict)
        else payload
    )
    # Only explicitly set fields are stored, so the tri-state semantics
    # (omitted vs. explicit null, SPEC §4.3) survive the round trip.
    stored = parsed.model_dump(mode="json", exclude_unset=True)
    # Capture the target entity's state at proposal time (optimistic
    # staleness guard, SPEC §3) so the executor can detect drift.
    if spec.baseline is not None:
        stored.update(await spec.baseline(session, user, parsed))
    # The user-facing preview is derived from the typed payload when the kind
    # provides a preview builder, so confirm/done text is exact and consistent
    # (SPEC §3); otherwise fall back to the model-supplied summary.
    if spec.preview is not None:
        summary = (await spec.preview(session, user, parsed)).strip() or summary
    action = PendingAction(
        user_id=user.id,
        kind=kind,
        payload=stored,
        summary=summary.strip()[:2000],
        status=ActionStatus.proposed.value,
        expires_at=_now() + (expires_in if expires_in is not None else DEFAULT_ACTION_TTL),
    )
    session.add(action)
    await session.flush()
    return action


async def get_action(
    session: AsyncSession, user: User, action_id: int
) -> PendingAction | None:
    """Return the user's action. Pure read: it never mutates; use
    :func:`effective_status` to report an overdue action as ``expired``."""
    action = await session.get(PendingAction, action_id)
    if action is None or action.user_id != user.id:
        return None
    return action


async def list_actions(
    session: AsyncSession,
    user: User,
    *,
    status: ActionStatus | None = None,
    limit: int = 50,
) -> list[PendingAction]:
    """List the user's actions, newest first, optionally filtered by
    *effective* status. Pure read: it never mutates.

    The filter is TTL-aware (V4 §29): a ``proposed``/``confirmed`` row whose
    ``expires_at`` has passed is treated as ``expired`` in the query itself,
    so ``?status=proposed`` excludes overdue rows and ``?status=expired``
    includes them — without requiring the worker to have flushed the durable
    ``expired`` transition first."""
    stmt = (
        select(PendingAction)
        .where(PendingAction.user_id == user.id)
        .order_by(PendingAction.created_at.desc(), PendingAction.id.desc())
        .limit(limit)
    )
    if status is None:
        return list((await session.scalars(stmt)).all())

    now = _now()
    live = (PendingAction.expires_at.is_(None)) | (PendingAction.expires_at > now)
    overdue = and_(
        PendingAction.status.in_(
            (ActionStatus.proposed.value, ActionStatus.confirmed.value)
        ),
        PendingAction.expires_at.is_not(None),
        PendingAction.expires_at <= now,
    )
    if status == ActionStatus.expired:
        stmt = stmt.where(or_(PendingAction.status == status.value, overdue))
    elif status in (ActionStatus.proposed, ActionStatus.confirmed):
        stmt = stmt.where(PendingAction.status == status.value, live)
    else:
        stmt = stmt.where(PendingAction.status == status.value)
    return list((await session.scalars(stmt)).all())


async def confirm_action(
    session: AsyncSession, user: User, action_id: int
) -> PendingAction:
    """Mark a proposed action ``confirmed`` (idempotent: confirming an
    already-confirmed action returns it unchanged). Raises ValueError when
    the action does not exist or was already rejected/expired."""
    action = await _load_write(session, user, action_id)
    if action is None:
        raise ValueError("Action not found.")
    if action.status == ActionStatus.proposed.value:
        action.status = ActionStatus.confirmed.value
        action.confirmed_at = _now()
        await session.flush()
    elif action.status in _TERMINAL:
        raise ValueError(f"Action is {action.status}.")
    return action


async def reject_action(
    session: AsyncSession, user: User, action_id: int
) -> PendingAction:
    """Mark a proposed/confirmed action ``rejected`` (idempotent on a
    rejected action)."""
    action = await _load_write(session, user, action_id)
    if action is None:
        raise ValueError("Action not found.")
    if action.status in (ActionStatus.proposed.value, ActionStatus.confirmed.value):
        action.status = ActionStatus.rejected.value
        action.rejected_at = _now()
        await session.flush()
    elif action.status != ActionStatus.rejected.value:
        raise ValueError(f"Action is {action.status}.")
    return action


async def execute_action(
    session: AsyncSession, user: User, action_id: int
) -> tuple[PendingAction, dict | list | str | int | float | None]:
    """Execute a confirmed action (SPEC §3).

    * The stored payload is re-parsed through the kind's schema; a
      malformed payload expires the action instead of running.
    * The executor re-validates ownership and entity state; a stale
      target expires the action and re-raises :class:`ActionStaleError`.
    * Execution is idempotent: an ``executed`` action returns its stored
      result without re-applying anything.

    Flushes only; the caller commits (the executor's mutations land in the
    same transaction, so a commit failure rolls the action state back too).
    """
    action = await _load_write(session, user, action_id)
    if action is None:
        raise ValueError("Action not found.")
    if action.status == ActionStatus.executed.value:
        return action, action.last_result
    if action.status != ActionStatus.confirmed.value:
        raise ValueError(f"Action is {action.status}, not executable.")

    spec = get_action_kind(action.kind)
    if spec is None:
        raise ValueError(f"No executor registered for kind: {action.kind!r}")
    try:
        payload = spec.payload_schema.model_validate(action.payload)
    except ValidationError as exc:
        action.status = ActionStatus.expired.value
        action.expired_at = _now()
        action.last_error = "payload no longer valid"[:1000]
        await session.flush()
        raise ValueError("Action payload is no longer valid.") from exc

    try:
        result = await spec.executor(session, user, payload)
    except ActionStaleError as exc:
        action.status = ActionStatus.expired.value
        action.expired_at = _now()
        action.last_error = str(exc)[:1000]
        await session.flush()
        raise
    except ValueError as exc:
        # Expected domain validation failure from the executor (interval
        # inversion, reminder cap, entity state, ...): the action can no
        # longer run, so make it terminal instead of leaving it stuck
        # ``confirmed`` (V5 §5.2). The error re-raises so the caller reports
        # the failure; infra exceptions (DB, network) are NOT caught here and
        # propagate with the action state untouched.
        action.status = ActionStatus.expired.value
        action.expired_at = _now()
        action.last_error = str(exc)[:1000]
        await session.flush()
        raise

    if result is None:
        result = {}
    action.status = ActionStatus.executed.value
    action.executed_at = _now()
    action.last_result = result if isinstance(result, (dict, list)) else {"value": result}
    await session.flush()
    return action, result


async def confirm_and_execute_action(
    session: AsyncSession, user: User, action_id: int
) -> tuple[PendingAction, dict | list | str | int | float | None]:
    """Confirm (when proposed) and execute an action atomically under a row lock.

    The action row is locked with ``SELECT ... FOR UPDATE`` for the whole
    transaction, so two concurrent confirmations of the same action serialize:
    the first confirms + executes + commits; the second blocks on the row lock,
    then re-reads the now-terminal row and returns the stored result WITHOUT
    re-applying the mutation. This makes confirm+execute concurrency-safe
    (SPEC §3) — a double-click cannot create a duplicate mutation.

    Raises ValueError when the action is missing, was rejected, expired, or is
    no longer valid/stale (see :func:`execute_action`). Flushes only; the
    caller commits.
    """
    # Lock the row for the duration of this transaction.
    action = await session.get(PendingAction, action_id, with_for_update=True)
    if action is None or action.user_id != user.id:
        raise ValueError("Action not found.")
    if _lazy_expire(action):
        await session.flush()
        raise ValueError(f"Action is {action.status}.")

    # Idempotent: already executed -> return the stored result, do not re-apply.
    if action.status == ActionStatus.executed.value:
        return action, action.last_result
    if action.status in (
        ActionStatus.rejected.value,
        ActionStatus.expired.value,
    ):
        raise ValueError(f"Action is {action.status}.")

    if action.status == ActionStatus.proposed.value:
        action.status = ActionStatus.confirmed.value
        action.confirmed_at = _now()
        await session.flush()

    return await execute_action(session, user, action_id)


async def expire_actions(session: AsyncSession) -> int:
    """Bulk-expire overdue proposed/confirmed actions (worker pass)."""
    now = _now()
    stmt = select(PendingAction).where(
        PendingAction.status.in_(
            [ActionStatus.proposed.value, ActionStatus.confirmed.value]
        ),
        PendingAction.expires_at <= now,
    )
    actions = list((await session.scalars(stmt)).all())
    for action in actions:
        action.status = ActionStatus.expired.value
        action.expired_at = now
    if actions:
        await session.flush()
    return len(actions)

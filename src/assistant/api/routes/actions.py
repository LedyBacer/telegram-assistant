"""Assistant Inbox — pending AI mutation proposal endpoints (SPEC §3)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.actions.calendar import ActionStaleError
from assistant.api.auth import get_current_user
from assistant.api.routes.common import _bad_request, _not_found
from assistant.api.schemas import ActionOut
from assistant.db import get_session
from assistant.models.pending_actions import ActionStatus
from assistant.models.users import User
from assistant.services import actions as actions_service

router = APIRouter(tags=["miniapp"])


@router.get("/actions", response_model=list[ActionOut])
async def list_actions(
    status: ActionStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ActionOut]:
    actions = await actions_service.list_actions(
        session, user, status=status, limit=limit
    )
    return [ActionOut.model_validate(a) for a in actions]


@router.post("/actions/{action_id}/confirm", response_model=ActionOut)
async def confirm_action(
    action_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ActionOut:
    """Confirm and execute a proposed action in one transaction — the same
    flow the Telegram bot uses, so confirming here makes the action
    terminal for the bot and vice versa.

    Replaying confirmation on an already-executed action returns the stored
    state (execution itself is idempotent, SPEC §3). The confirm+execute runs
    under a row lock (:func:`confirm_and_execute_action`), so concurrent
    double-clicks cannot double-apply the mutation."""
    pre = await actions_service.get_action(session, user, action_id)
    if pre is None:
        raise _not_found()
    try:
        action, _result = await actions_service.confirm_and_execute_action(
            session, user, action_id
        )
    except ActionStaleError as exc:
        # The service marked the action ``expired`` in this transaction;
        # persist it before raising, because the session dependency rolls
        # back uncommitted work when the handler exits with an exception
        # (the action would otherwise stay proposed forever).
        await session.commit()
        raise HTTPException(
            status_code=409, detail="proposal no longer applies to current data"
        ) from exc
    except ValueError as exc:
        # Rejected/expired/no-longer-valid: persist any in-transaction expiry
        # the service recorded, then 400. (Not-found is handled above via the
        # pre-check; the service re-check only re-raises it in a delete race,
        # which we treat as "unavailable".)
        await session.commit()
        raise _bad_request(exc) from exc
    await session.commit()
    return ActionOut.model_validate(action)


@router.post("/actions/{action_id}/reject", response_model=ActionOut)
async def reject_action(
    action_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ActionOut:
    action = await actions_service.get_action(session, user, action_id)
    if action is None:
        raise _not_found()
    # Use effective status: an overdue proposed/confirmed action is rejected
    # (not executed/transitioned) and reported as expired without a write.
    if actions_service.effective_status(action) not in (
        ActionStatus.proposed.value,
        ActionStatus.confirmed.value,
    ):
        raise HTTPException(
            status_code=400, detail=f"action is {actions_service.effective_status(action)}"
        )
    action = await actions_service.reject_action(session, user, action_id)
    await session.commit()
    return ActionOut.model_validate(action)

"""Durable pending-action lifecycle tests (SPEC §3) — real PostgreSQL.

Covers the propose -> confirm -> execute flow, idempotent re-confirm and
re-execute, typed payload re-validation at execution, ownership scoping,
lazy and bulk expiry, and stale-entity handling (executor re-validates
entity state; a stale target expires the action).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.actions.calendar import ActionStaleError, UpdateItemPayload
from assistant.models.calendar_items import CalendarItem
from assistant.models.pending_actions import ActionStatus
from assistant.models.users import User
from assistant.services import actions as act
from assistant.services import calendar as cal
from assistant.services.users import upsert_user

START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
MOVED = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)


async def _user(session: AsyncSession, user_id: int = 21) -> User:
    user, _ = await upsert_user(session, user_id=user_id, first_name="T")
    await session.flush()
    return user


async def _item(session: AsyncSession, user: User, title: str = "Move me") -> None:
    await cal.create_item(session, user, title=title, starts_at=START)
    await session.flush()


# ---------------------------------------------------------------------------
# Proposal
# ---------------------------------------------------------------------------


async def test_propose_validates_payload(session: AsyncSession) -> None:
    user = await _user(session)
    with pytest.raises(ValueError, match="Unknown action kind"):
        await act.propose_action(
            session, user, kind="nope", payload={}, summary="x"
        )
    with pytest.raises(ValueError):
        await act.propose_action(
            session,
            user,
            kind="update_item",
            payload={"item_id": "not-an-int"},
            summary="x",
        )
    with pytest.raises(ValueError, match="summary"):
        await act.propose_action(
            session,
            user,
            kind="update_item",
            payload={"item_id": 1},
            summary="   ",
        )


async def test_propose_stores_typed_payload(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    item = await cal.create_item(session, user, title="P", starts_at=START)
    action = await act.propose_action(
        session,
        user,
        kind="update_item",
        payload=UpdateItemPayload(item_id=item.id, starts_at=MOVED.isoformat()),
        summary="Move to 14:00",
    )
    await session.commit()
    assert action.status == ActionStatus.proposed.value
    assert action.expires_at is not None
    assert action.payload["starts_at"] == MOVED.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Confirm / reject
# ---------------------------------------------------------------------------


async def test_confirm_is_idempotent(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(session, user, title="P")
    action = await act.propose_action(
        session, user, kind="update_item", payload={"item_id": item.id}, summary="s"
    )
    confirmed = await act.confirm_action(session, user, action.id)
    await session.commit()
    assert confirmed.status == ActionStatus.confirmed.value
    assert confirmed.confirmed_at is not None
    # Second confirm: no state change, no error.
    again = await act.confirm_action(session, user, action.id)
    assert again.status == ActionStatus.confirmed.value
    assert again.confirmed_at == confirmed.confirmed_at


async def test_reject_then_confirm_raises(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(session, user, title="P")
    action = await act.propose_action(
        session, user, kind="update_item", payload={"item_id": item.id}, summary="s"
    )
    rejected = await act.reject_action(session, user, action.id)
    await session.commit()
    assert rejected.status == ActionStatus.rejected.value
    assert rejected.rejected_at is not None
    # Reject is idempotent.
    assert (await act.reject_action(session, user, action.id)).status == (
        ActionStatus.rejected.value
    )
    with pytest.raises(ValueError, match="rejected"):
        await act.confirm_action(session, user, action.id)
    with pytest.raises(ValueError, match="rejected"):
        await act.execute_action(session, user, action.id)


async def test_execute_requires_confirmation(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(session, user, title="P", starts_at=START)
    action = await act.propose_action(
        session,
        user,
        kind="update_item",
        payload={"item_id": item.id, "starts_at": MOVED.isoformat()},
        summary="s",
    )
    with pytest.raises(ValueError, match="not executable"):
        await act.execute_action(session, user, action.id)
    # The item must be untouched.
    assert (await cal.get_item(session, user, item.id)).starts_at == START


# ---------------------------------------------------------------------------
# Execution: real mutation, idempotent re-execution
# ---------------------------------------------------------------------------


async def test_execute_update_item_moves_item(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(session, user, title="P", starts_at=START)
    action = await act.propose_action(
        session,
        user,
        kind="update_item",
        payload={"item_id": item.id, "starts_at": MOVED.isoformat()},
        summary="Move to 14:00",
    )
    await act.confirm_action(session, user, action.id)
    done, result = await act.execute_action(session, user, action.id)
    await session.commit()
    assert done.status == ActionStatus.executed.value
    assert done.executed_at is not None
    assert result["item_id"] == item.id
    assert (await cal.get_item(session, user, item.id)).starts_at == MOVED


async def test_re_execute_is_idempotent(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(session, user, title="P", starts_at=START)
    action = await act.propose_action(
        session,
        user,
        kind="update_item",
        payload={"item_id": item.id, "starts_at": MOVED.isoformat()},
        summary="s",
    )
    await act.confirm_action(session, user, action.id)
    _, first = await act.execute_action(session, user, action.id)
    await session.commit()

    # Simulate later drift: the item is moved away again.
    item.starts_at = START
    await session.commit()

    # Re-execution returns the stored result and does NOT re-apply.
    done, second = await act.execute_action(session, user, action.id)
    assert done.status == ActionStatus.executed.value
    assert second == first
    assert (await cal.get_item(session, user, item.id)).starts_at == START


async def test_execute_create_item_with_reminders(session: AsyncSession) -> None:
    user = await _user(session)
    action = await act.propose_action(
        session,
        user,
        kind="create_item",
        payload={
            "title": "Call",
            "starts_at": START.isoformat(),
            "remind_offsets_minutes": [30],
        },
        summary="Create 'Call'",
    )
    await act.confirm_action(session, user, action.id)
    done, result = await act.execute_action(session, user, action.id)
    await session.commit()
    assert done.status == ActionStatus.executed.value
    item = await cal.get_item(session, user, result["item_id"])
    assert item is not None and item.title == "Call"
    from sqlalchemy import select

    from assistant.models.reminders import Reminder

    rows = list(
        (
            await session.execute(
                select(Reminder).where(Reminder.calendar_item_id == item.id)
            )
        )
        .scalars()
    )
    assert len(rows) == 1


async def test_execute_delete_item(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(session, user, title="D", starts_at=START)
    action = await act.propose_action(
        session,
        user,
        kind="delete_item",
        payload={"item_id": item.id},
        summary="Delete 'D'",
    )
    await act.confirm_action(session, user, action.id)
    done, _ = await act.execute_action(session, user, action.id)
    await session.commit()
    assert done.status == ActionStatus.executed.value
    assert await cal.get_item(session, user, item.id) is None


# ---------------------------------------------------------------------------
# Stale entities and payload re-validation at execution
# ---------------------------------------------------------------------------


async def test_stale_entity_expires_action(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(session, user, title="Gone", starts_at=START)
    action = await act.propose_action(
        session,
        user,
        kind="update_item",
        payload={"item_id": item.id, "starts_at": MOVED.isoformat()},
        summary="s",
    )
    await act.confirm_action(session, user, action.id)
    await cal.delete_item(session, user, item.id)
    await session.commit()

    with pytest.raises(ActionStaleError):
        await act.execute_action(session, user, action.id)
    await session.commit()
    fresh = await act.get_action(session, user, action.id)
    assert fresh is not None
    assert fresh.status == ActionStatus.expired.value
    assert fresh.last_error is not None


async def test_stale_state_expires_action(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(session, user, title="Done", starts_at=START)
    await cal.complete_item(session, user, item.id)
    await session.commit()

    action = await act.propose_action(
        session,
        user,
        kind="cancel_item",
        payload={"item_id": item.id},
        summary="s",
    )
    await act.confirm_action(session, user, action.id)
    with pytest.raises(ActionStaleError):
        await act.execute_action(session, user, action.id)
    fresh = await act.get_action(session, user, action.id)
    assert fresh is not None and fresh.status == ActionStatus.expired.value


async def test_drifted_entity_expires_action(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(session, user, title="Stable", starts_at=START)
    action = await act.propose_action(
        session,
        user,
        kind="update_item",
        payload={"item_id": item.id, "starts_at": MOVED.isoformat()},
        summary="s",
    )
    await act.confirm_action(session, user, action.id)
    await session.commit()

    # The target drifts AFTER the proposal (the baseline captured the pre-drift
    # updated_at). The executor must treat it as stale, not clobber the change.
    await cal.update_item(session, user, item.id, title="Changed")
    await session.commit()

    with pytest.raises(ActionStaleError):
        await act.execute_action(session, user, action.id)
    await session.commit()
    fresh = await act.get_action(session, user, action.id)
    assert fresh is not None
    assert fresh.status == ActionStatus.expired.value
    assert fresh.last_error is not None
    # The proposed mutation was NOT applied on top of the newer state.
    assert (await cal.get_item(session, user, item.id)).starts_at == START


async def test_corrupted_payload_expires_action(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(session, user, title="P")
    action = await act.propose_action(
        session, user, kind="update_item", payload={"item_id": item.id}, summary="s"
    )
    await act.confirm_action(session, user, action.id)
    # Simulate payload drift/corruption in storage.
    action.payload = {"item_id": "not-an-int"}
    await session.flush()

    with pytest.raises(ValueError, match="no longer valid"):
        await act.execute_action(session, user, action.id)
    fresh = await act.get_action(session, user, action.id)
    assert fresh is not None
    assert fresh.status == ActionStatus.expired.value
    assert fresh.last_error == "payload no longer valid"


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


async def test_actions_are_user_scoped(session: AsyncSession) -> None:
    owner = await _user(session, user_id=21)
    other = await _user(session, user_id=22)
    item = await cal.create_item(session, owner, title="Private", starts_at=START)
    action = await act.propose_action(
        session,
        owner,
        kind="update_item",
        payload={"item_id": item.id, "starts_at": MOVED.isoformat()},
        summary="s",
    )
    await session.commit()

    assert await act.get_action(session, other, action.id) is None
    with pytest.raises(ValueError, match="not found"):
        await act.confirm_action(session, other, action.id)
    with pytest.raises(ValueError, match="not found"):
        await act.execute_action(session, other, action.id)
    # The other user's listing is empty too.
    assert await act.list_actions(session, other) == []


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


async def test_expired_action_cannot_be_confirmed_or_executed(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    item = await cal.create_item(session, user, title="P")
    action = await act.propose_action(
        session,
        user,
        kind="update_item",
        payload={"item_id": item.id},
        summary="s",
        expires_in=timedelta(seconds=-1),
    )
    with pytest.raises(ValueError, match="expired"):
        await act.confirm_action(session, user, action.id)
    with pytest.raises(ValueError, match="expired"):
        await act.execute_action(session, user, action.id)
    fresh = await act.get_action(session, user, action.id)
    assert fresh is not None
    assert fresh.status == ActionStatus.expired.value
    assert fresh.expired_at is not None


async def test_bulk_expire(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(session, user, title="P")
    overdue = await act.propose_action(
        session,
        user,
        kind="update_item",
        payload={"item_id": item.id},
        summary="old",
        expires_in=timedelta(seconds=-1),
    )
    fresh = await act.propose_action(
        session,
        user,
        kind="update_item",
        payload={"item_id": item.id},
        summary="new",
        expires_in=timedelta(hours=2),
    )
    await session.commit()

    expired_count = await act.expire_actions(session)
    await session.commit()
    assert expired_count == 1
    assert (await act.get_action(session, user, overdue.id)).status == (
        ActionStatus.expired.value
    )
    assert (await act.get_action(session, user, fresh.id)).status == (
        ActionStatus.proposed.value
    )


async def test_read_does_not_mutate_overdue_action(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(session, user, title="P")
    action = await act.propose_action(
        session,
        user,
        kind="update_item",
        payload={"item_id": item.id},
        summary="s",
        expires_in=timedelta(seconds=-1),
    )
    await session.commit()

    # A read reports the effective (expired) status...
    fresh = await act.get_action(session, user, action.id)
    assert fresh is not None
    assert act.effective_status(fresh) == ActionStatus.expired.value
    # ...without persisting the transition (stored status stays proposed).
    assert fresh.status == ActionStatus.proposed.value

    # list_actions is equally non-mutating.
    listed = await act.list_actions(session, user)
    assert [act.effective_status(a) for a in listed] == [ActionStatus.expired.value]
    assert [a.status for a in listed] == [ActionStatus.proposed.value]

    # The worker's bulk pass is what durably flips the row.
    await act.expire_actions(session)
    await session.commit()
    assert (await act.get_action(session, user, action.id)).status == (
        ActionStatus.expired.value
    )


# ---------------------------------------------------------------------------
# Atomic confirm + execute (row-locked, double-click safe)
# ---------------------------------------------------------------------------


async def test_confirm_and_execute_is_idempotent(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(session, user, title="P", starts_at=START)
    action = await act.propose_action(
        session,
        user,
        kind="update_item",
        payload={"item_id": item.id, "starts_at": MOVED.isoformat()},
        summary="s",
    )
    done, first = await act.confirm_and_execute_action(session, user, action.id)
    await session.commit()
    assert done.status == ActionStatus.executed.value
    assert (await cal.get_item(session, user, item.id)).starts_at == MOVED

    # A second confirm+execute returns the stored result, does NOT re-apply.
    again, second = await act.confirm_and_execute_action(session, user, action.id)
    assert again.status == ActionStatus.executed.value
    assert second == first


async def test_concurrent_confirm_executes_once(
    session: AsyncSession, engine
) -> None:
    user = await _user(session)
    action = await act.propose_action(
        session,
        user,
        kind="create_item",
        payload={"title": "Race", "starts_at": START.isoformat()},
        summary="Create 'Race'",
    )
    await session.commit()
    action_id = action.id

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _confirm() -> ActionStatus:
        async with factory() as s:
            done, _result = await act.confirm_and_execute_action(s, user, action_id)
            await s.commit()
            return done.status

    # Both confirmations race on the same row; the FOR UPDATE lock serializes
    # them so the (non-idempotent) create_item mutation is applied exactly once.
    results = await asyncio.gather(_confirm(), _confirm())
    assert all(r == ActionStatus.executed.value for r in results)

    # Verify in a fresh session: the action is executed and the mutation
    # (create_item) landed exactly once.
    async with factory() as s:
        fresh = await act.get_action(s, user, action_id)
        assert fresh is not None
        assert fresh.status == ActionStatus.executed.value
        count = await s.scalar(
            select(func.count())
            .select_from(CalendarItem)
            .where(CalendarItem.user_id == user.id)
        )
        assert count == 1

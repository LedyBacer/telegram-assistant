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
        session,
        user,
        kind="update_item",
        payload={"item_id": item.id, "title": "P"},
        summary="s",
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
        session,
        user,
        kind="update_item",
        payload={"item_id": item.id, "title": "P"},
        summary="s",
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
        session,
        user,
        kind="update_item",
        payload={"item_id": item.id, "title": "P"},
        summary="s",
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
        payload={"item_id": item.id, "title": "P"},
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
        payload={"item_id": item.id, "title": "P"},
        summary="old",
        expires_in=timedelta(seconds=-1),
    )
    fresh = await act.propose_action(
        session,
        user,
        kind="update_item",
        payload={"item_id": item.id, "title": "P"},
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
        payload={"item_id": item.id, "title": "P"},
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


async def test_list_actions_status_filter_is_ttl_aware(session: AsyncSession) -> None:
    """?status= must reflect effective status: an overdue proposed row is
    'expired' in the SQL filter even before the worker flushes the durable
    transition (V4 §29)."""
    user = await _user(session)
    item = await cal.create_item(session, user, title="P")
    fresh = await act.propose_action(
        session, user, kind="update_item",
        payload={"item_id": item.id, "title": "P"}, summary="fresh",
        expires_in=timedelta(hours=2),
    )
    overdue = await act.propose_action(
        session, user, kind="update_item",
        payload={"item_id": item.id, "title": "P"}, summary="old",
        expires_in=timedelta(seconds=-1),
    )
    await session.commit()

    by_status = await act.list_actions(session, user, status=ActionStatus.proposed)
    assert [a.id for a in by_status] == [fresh.id]
    # Both rows are still stored as proposed (no durable transition yet),
    # but the expired filter surfaces the overdue one.
    expired = await act.list_actions(session, user, status=ActionStatus.expired)
    assert [a.id for a in expired] == [overdue.id]
    all_rows = await act.list_actions(session, user)
    assert {a.id for a in all_rows} == {fresh.id, overdue.id}


async def test_list_actions_actionable_and_history_filters(session: AsyncSession) -> None:
    """V5.1: the aggregate filters ``actionable`` (live proposed/confirmed,
    TTL-aware) and ``history`` (terminal, plus overdue proposed/confirmed
    reported as expired) partition the inbox server-side, so the Mini App
    never filters client-side."""
    user = await _user(session)
    item = await cal.create_item(session, user, title="P")
    payload = {"item_id": item.id, "title": "P"}
    proposed = await act.propose_action(
        session, user, kind="update_item", payload=payload,
        summary="proposed", expires_in=timedelta(hours=2),
    )
    confirmed = await act.propose_action(
        session, user, kind="update_item", payload=payload,
        summary="confirmed", expires_in=timedelta(hours=2),
    )
    await act.confirm_action(session, user, confirmed.id)
    rejected = await act.propose_action(
        session, user, kind="update_item", payload=payload,
        summary="rejected", expires_in=timedelta(hours=2),
    )
    await act.reject_action(session, user, rejected.id)
    # One overdue row (no durable transition yet) and one durably expired.
    overdue = await act.propose_action(
        session, user, kind="update_item", payload=payload,
        summary="overdue", expires_in=timedelta(seconds=-1),
    )
    durable_expired = await act.propose_action(
        session, user, kind="update_item", payload=payload,
        summary="expired", expires_in=timedelta(seconds=-1),
    )
    await act.expire_actions(session)
    await session.commit()

    actionable = await act.list_actions(session, user, status="actionable")
    assert {a.id for a in actionable} == {proposed.id, confirmed.id}

    history = await act.list_actions(session, user, status="history")
    assert {a.id for a in history} == {
        rejected.id, overdue.id, durable_expired.id,
    }

    # The two views are disjoint and exhaustive over the user's actions.
    assert not ({a.id for a in actionable} & {a.id for a in history})
    all_rows = await act.list_actions(session, user)
    assert {a.id for a in all_rows} == (
        {a.id for a in actionable} | {a.id for a in history}
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


# ---------------------------------------------------------------------------
# Workout action kinds (P12)
# ---------------------------------------------------------------------------


async def test_execute_log_workout(session: AsyncSession) -> None:
    from assistant.services import workouts as wo

    user = await _user(session)
    action = await act.propose_action(
        session,
        user,
        kind="log_workout",
        payload={
            "name": "Push",
            "duration_minutes": 40,
            "perceived_effort": 7,
            "notes": "new PR",
        },
        summary="Log push workout",
    )
    await act.confirm_action(session, user, action.id)
    done, result = await act.execute_action(session, user, action.id)
    await session.commit()
    assert done.status == ActionStatus.executed.value
    assert result["name"] == "Push"
    log = await wo.get_workout(session, user, result["workout_id"])
    assert log is not None
    assert log.duration_minutes == 40
    assert log.perceived_effort == 7
    assert log.notes == "new PR"
    assert log.status == "completed"


async def test_log_workout_payload_validation(session: AsyncSession) -> None:
    user = await _user(session)
    with pytest.raises(ValueError):
        await act.propose_action(
            session,
            user,
            kind="log_workout",
            payload={"name": "Push", "duration_minutes": 0},
            summary="s",
        )
    with pytest.raises(ValueError):
        await act.propose_action(
            session,
            user,
            kind="log_workout",
            payload={"name": "Push", "perceived_effort": 11},
            summary="s",
        )


async def test_execute_schedule_workout_creates_item_and_reminder(
    session: AsyncSession,
) -> None:
    from assistant.models.reminders import Reminder

    user = await _user(session)
    action = await act.propose_action(
        session,
        user,
        kind="schedule_workout",
        payload={
            "name": "Run",
            "starts_at": MOVED.isoformat(),
            "duration_minutes": 30,
        },
        summary="Schedule run",
    )
    await act.confirm_action(session, user, action.id)
    done, result = await act.execute_action(session, user, action.id)
    await session.commit()
    assert done.status == ActionStatus.executed.value
    item = await cal.get_item(session, user, result["item_id"])
    assert item is not None
    # V5 §6.1: raw workout name persisted (no "Workout: " prefix).
    assert item.title == "Run"
    assert item.starts_at == MOVED
    assert item.extra.get("duration_minutes") == 30
    rows = list(
        (
            await session.execute(
                select(Reminder).where(Reminder.calendar_item_id == item.id)
            )
        )
        .scalars()
    )
    assert len(rows) == 1
    assert rows[0].status == "pending"


# ---------------------------------------------------------------------------
# Mutation previews derived from typed data (P13)
# ---------------------------------------------------------------------------


async def test_preview_create_item_from_typed_data(session: AsyncSession) -> None:
    user = await _user(session)
    user.settings.language = "en"
    await session.flush()
    action = await act.propose_action(
        session,
        user,
        kind="create_item",
        payload={
            "title": "Call Alex",
            "starts_at": MOVED.isoformat(),
            "remind_offsets_minutes": [15],
        },
        summary="model phrasing should be ignored",
    )
    assert action.summary == (
        "Create task 'Call Alex' at 2026-10-01 14:00 reminder(s) at 15 min"
    )


async def test_preview_create_item_respects_user_timezone(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    user.settings.timezone = "Europe/Berlin"
    user.settings.language = "en"
    await session.flush()
    # 12:00 UTC == 14:00 Europe/Berlin (CEST, UTC+2 in October).
    action = await act.propose_action(
        session,
        user,
        kind="create_item",
        payload={"title": "Standup", "starts_at": START.isoformat()},
        summary="s",
    )
    assert action.summary == "Create task 'Standup' at 2026-10-01 14:00"


async def test_preview_update_item_lists_changed_fields(session: AsyncSession) -> None:
    user = await _user(session)
    user.settings.language = "en"
    await session.flush()
    item = await cal.create_item(session, user, title="Move me", starts_at=START)
    action = await act.propose_action(
        session,
        user,
        kind="update_item",
        payload={"item_id": item.id, "starts_at": MOVED.isoformat()},
        summary="s",
    )
    # V5 §9: the changed field is rendered as its localized label, never the
    # raw identifier ``starts_at``.
    assert action.summary == "Update item 'Move me': start"


async def test_preview_ru_has_no_raw_identifiers(session: AsyncSession) -> None:
    """V5 §9: RU/EN previews must not leak raw identifiers — the item kind and
    the changed field names (task/event, starts_at/ends_at/due_at/priority)
    must all be rendered as localized labels."""
    user = await _user(session)
    # Russian is the default language, so no override is needed.
    assert user.settings.language == "ru"
    item = await cal.create_item(
        session, user, title="Move me", starts_at=START
    )
    action = await act.propose_action(
        session,
        user,
        kind="update_item",
        payload={
            "item_id": item.id,
            "starts_at": MOVED.isoformat(),
            "ends_at": MOVED.isoformat(),
            "due_at": MOVED.isoformat(),
            "priority": "high",
            "title": "New title",
        },
        summary="s",
    )
    raw = ["starts_at", "ends_at", "due_at", "priority", "title", "description"]
    for ident in raw:
        assert ident not in action.summary, (ident, action.summary)
    # Localized RU labels are present instead.
    for label in ("название", "начало", "окончание", "срок", "приоритет"):
        assert label in action.summary, (label, action.summary)

    # The item kind is localized too (no raw ``event`` enum value).
    create = await act.propose_action(
        session,
        user,
        kind="create_item",
        payload={"title": "Планировка", "kind": "event"},
        summary="s",
    )
    assert "событие" in create.summary
    assert "event" not in create.summary


async def test_preview_complete_item_uses_current_title(session: AsyncSession) -> None:
    user = await _user(session)
    user.settings.language = "en"
    await session.flush()
    item = await cal.create_item(session, user, title="Standup", starts_at=START)
    action = await act.propose_action(
        session, user, kind="complete_item", payload={"item_id": item.id}, summary="s"
    )
    assert action.summary == "Complete item 'Standup'"


async def test_preview_create_reminder(session: AsyncSession) -> None:
    user = await _user(session)
    user.settings.language = "en"
    await session.flush()
    action = await act.propose_action(
        session,
        user,
        kind="create_reminder",
        payload={"message": "Buy milk", "fire_at": MOVED.isoformat()},
        summary="s",
    )
    assert action.summary == "Remind me 'Buy milk' at 2026-10-01 14:00"


async def test_preview_workouts(session: AsyncSession) -> None:
    user = await _user(session)
    user.settings.language = "en"
    await session.flush()
    log_action = await act.propose_action(
        session,
        user,
        kind="log_workout",
        payload={"name": "Push", "duration_minutes": 40, "perceived_effort": 7},
        summary="s",
    )
    assert log_action.summary == "Log workout 'Push' 40 min effort 7"
    sched_action = await act.propose_action(
        session,
        user,
        kind="schedule_workout",
        payload={
            "name": "Run",
            "starts_at": MOVED.isoformat(),
            "duration_minutes": 30,
        },
        summary="s",
    )
    assert sched_action.summary == "Schedule workout 'Run' at 2026-10-01 14:00 (30 min)"


async def test_preview_localizes_to_russian_by_default(session: AsyncSession) -> None:
    user = await _user(session)  # default language is Russian
    user.settings.timezone = "Europe/Berlin"
    await session.flush()
    action = await act.propose_action(
        session,
        user,
        kind="create_item",
        payload={"title": "Standup", "starts_at": START.isoformat()},
        summary="s",
    )
    # V5 §9: the kind is localized ("задачу"), not the raw enum value "task".
    assert action.summary == "Создать задачу «Standup» в 2026-10-01 14:00"


# ---------------------------------------------------------------------------
# Strict payload schemas (V4 §17-18)
# ---------------------------------------------------------------------------


def test_update_item_rejects_no_op_payload() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="at least one field"):
        UpdateItemPayload(item_id=1)
    # A real field is accepted.
    assert UpdateItemPayload(item_id=1, title="x").title == "x"


def test_payload_schemas_forbid_extra_fields() -> None:
    from pydantic import ValidationError

    from assistant.actions.workouts import LogWorkoutPayload, ScheduleWorkoutPayload

    with pytest.raises(ValidationError):
        UpdateItemPayload(item_id=1, title="x", bogus_field="nope")
    with pytest.raises(ValidationError):
        LogWorkoutPayload(name="x", bogus_field="nope")
    with pytest.raises(ValidationError):
        ScheduleWorkoutPayload(name="x", starts_at=START, bogus_field="nope")

"""Limited proactivity tests (SPEC §11, V3 P38-P40) — real PostgreSQL, fake sender.

Covers the deterministic triggers (weekly review summary, workout staleness
with calendar context, overdue items), the per-user anti-spam gates (enabled,
quiet hours, max/day, min interval), NudgeDelivery dedupe, user scoping,
pending-action expiry, per-user failure isolation, and cross-session nudge
dedupe under concurrent worker passes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from assistant.models.calendar_items import CalendarItem
from assistant.models.pending_actions import ActionStatus, PendingAction
from assistant.models.proactivity import NudgeDelivery, NudgeKind, ProactiveSettings
from assistant.models.users import User
from assistant.services import proactivity as pro
from assistant.services import workouts as workouts_service
from assistant.services.users import upsert_user

# 2026-09-21 is a Monday (local == UTC for the test users).
MON = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
TUE = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
WED = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
NEXT_MON = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


async def _user(session: AsyncSession, user_id: int = 91) -> User:
    user, _ = await upsert_user(session, user_id=user_id, first_name="P")
    user.settings.timezone = "UTC"
    user.settings.language = "en"
    await session.commit()
    return user


def _fake_send(fail_for: set[int] | None = None):
    calls: list[tuple[int, str]] = []

    async def send(chat_id: int, text: str) -> None:
        if fail_for and chat_id in fail_for:
            raise RuntimeError("bot API down")
        calls.append((chat_id, text))

    return send, calls


async def _settings(session: AsyncSession, user: User) -> ProactiveSettings:
    settings = await pro.get_proactive_settings(session, user.id)
    await session.commit()
    return settings


async def _item(session: AsyncSession, user: User, **kw) -> CalendarItem:
    item = CalendarItem(user_id=user.id, title=kw.pop("title", "Item"), **kw)
    session.add(item)
    await session.flush()
    return item


async def _kinds(session: AsyncSession, user_id: int) -> list[str]:
    rows = (
        await session.scalars(
            select(NudgeDelivery).where(NudgeDelivery.user_id == user_id)
        )
    ).all()
    return sorted(r.kind for r in rows)


# ---------------------------------------------------------------------------
# Weekly review (deterministic summary, V3 P38)
# ---------------------------------------------------------------------------


async def test_weekly_review_fires_once_per_iso_week(session: AsyncSession) -> None:
    user = await _user(session)
    # A completed item this week gives the summary something to report, and
    # the fresh workout suppresses the workout nudge, isolating the review.
    await _item(
        session,
        user,
        title="Done thing",
        status="completed",
        completed_at=MON - timedelta(hours=2),
    )
    await workouts_service.log_workout(session, user, name="Fresh", started_at=MON - timedelta(hours=1))
    await session.commit()

    send, calls = _fake_send()
    assert await pro.evaluate_user(session, user.id, now=MON, send=send) == [
        NudgeKind.weekly_review
    ]
    await session.commit()
    assert calls and calls[0][0] == user.id

    # Same ISO week, a few hours later: deduped.
    send2, calls2 = _fake_send()
    assert (
        await pro.evaluate_user(session, user.id, now=MON + timedelta(hours=4), send=send2)
        == []
    )
    await session.commit()
    assert calls2 == []

    # Next ISO week: the fresh workout logged this week is itself content,
    # so the review fires again (and keeps the workout gate off).
    await workouts_service.log_workout(session, user, name="Fresh2", started_at=NEXT_MON - timedelta(hours=1))
    await session.commit()
    send3, calls3 = _fake_send()
    assert await pro.evaluate_user(session, user.id, now=NEXT_MON, send=send3) == [
        NudgeKind.weekly_review
    ]
    await session.commit()
    assert len(calls3) == 1


async def test_weekly_review_only_on_monday(session: AsyncSession) -> None:
    user = await _user(session)
    await workouts_service.log_workout(session, user, name="Fresh", started_at=TUE - timedelta(hours=1))
    await session.commit()

    send, _ = _fake_send()
    assert NudgeKind.weekly_review not in await pro.evaluate_user(
        session, user.id, now=TUE, send=send
    )
    await session.commit()


async def test_weekly_review_skipped_when_nothing_to_report(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    # No workouts and no calendar state: an empty week has nothing to report,
    # so the Monday review must stay silent (only the workout nudge fires).
    send, _ = _fake_send()
    sent = await pro.evaluate_user(session, user.id, now=MON, send=send)
    await session.commit()
    assert sent == [NudgeKind.workout]
    assert NudgeKind.weekly_review not in sent


async def test_weekly_review_summary_lists_state(session: AsyncSession) -> None:
    user = await _user(session)
    # 1 overdue, 2 completed this week, 1 upcoming high-priority, 2 workouts.
    await _item(session, user, title="Late one", status="scheduled", due_at=MON - timedelta(days=1))
    await _item(session, user, title="Done 1", status="completed", completed_at=MON - timedelta(hours=2))
    await _item(session, user, title="Done 2", status="completed", completed_at=MON - timedelta(hours=1))
    await _item(
        session,
        user,
        title="Big fish",
        status="scheduled",
        priority="high",
        starts_at=MON + timedelta(days=2),
    )
    await workouts_service.log_workout(
        session, user, name="A", started_at=MON - timedelta(hours=2), duration_minutes=30
    )
    await workouts_service.log_workout(
        session, user, name="B", started_at=MON - timedelta(hours=1), duration_minutes=45
    )
    await session.commit()

    send, calls = _fake_send()
    # V4 §9-10: the weekly review has priority, and the per-reservation gate
    # re-check suppresses the (also-eligible) overdue nudge in the same pass
    # via min_interval — only one nudge is sent per pass.
    assert await pro.evaluate_user(session, user.id, now=MON, send=send) == [
        NudgeKind.weekly_review
    ]
    await session.commit()
    text = calls[0][1]
    assert text.startswith("Weekly review:")
    assert "overdue: 1" in text
    assert "completed this week: 2" in text
    assert "upcoming (important): Big fish" in text
    assert "workouts: 2 (75 min)" in text
    # Deterministic ordering: the summary lines follow the fixed template.
    assert text.index("overdue:") < text.index("completed this week")
    assert text.index("completed this week") < text.index("upcoming (important)")
    assert text.index("upcoming (important)") < text.index("workouts:")


# ---------------------------------------------------------------------------
# Workout nudge (with calendar context, V3 P39)
# ---------------------------------------------------------------------------


async def test_workout_nudge_when_never_or_stale(
    session: AsyncSession,
) -> None:
    user = await _user(session)  # no workouts at all

    send, calls = _fake_send()
    sent = await pro.evaluate_user(session, user.id, now=TUE, send=send)
    await session.commit()
    assert NudgeKind.workout in sent
    assert len(calls) >= 1

    # Logged once today: deduped for the rest of the local day.
    send2, calls2 = _fake_send()
    sent2 = await pro.evaluate_user(session, user.id, now=TUE, send=send2)
    await session.commit()
    assert sent2 == [] and calls2 == []


async def test_workout_nudge_not_when_recent(session: AsyncSession) -> None:
    user = await _user(session)
    await workouts_service.log_workout(
        session, user, name="Fresh", started_at=TUE - timedelta(hours=1)
    )
    await session.commit()

    send, _ = _fake_send()
    sent = await pro.evaluate_user(session, user.id, now=TUE, send=send)
    await session.commit()
    assert NudgeKind.workout not in sent


async def test_workout_nudge_suppressed_when_scheduled_today(
    session: AsyncSession,
) -> None:
    user = await _user(session)  # no logged workouts at all
    await _item(
        session,
        user,
        title="Evening session",
        status="scheduled",
        source="workout",
        starts_at=TUE + timedelta(hours=8),
    )
    await session.commit()

    send, calls = _fake_send()
    assert await pro.evaluate_user(session, user.id, now=TUE, send=send) == []
    await session.commit()
    assert calls == []


async def test_workout_nudge_suppressed_when_scheduled_upcoming(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    await _item(
        session,
        user,
        title="Friday session",
        status="scheduled",
        source="workout",
        starts_at=TUE + timedelta(days=3),
    )
    await session.commit()

    send, calls = _fake_send()
    assert await pro.evaluate_user(session, user.id, now=TUE, send=send) == []
    await session.commit()
    assert calls == []


async def test_workout_nudge_fires_when_scheduled_workout_already_past(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    # A workout planned yesterday that never happened: the plan slipped, so
    # the nudge is still warranted.
    await _item(
        session,
        user,
        title="Missed session",
        status="scheduled",
        source="workout",
        starts_at=TUE - timedelta(days=1),
    )
    await session.commit()

    send, calls = _fake_send()
    # The slipped workout item is past due, so the overdue nudge fires — and
    # it has priority over the workout nudge (V4 §10). The per-reservation
    # gate re-check then suppresses the workout nudge in the same pass.
    sent = await pro.evaluate_user(session, user.id, now=TUE, send=send)
    await session.commit()
    assert sent == [NudgeKind.overdue]
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Overdue nudge (V3 P38)
# ---------------------------------------------------------------------------


async def test_overdue_nudge_fires_and_dedupes_daily(session: AsyncSession) -> None:
    user = await _user(session)
    await _item(
        session, user, title="Late one", status="scheduled", due_at=MON
    )
    await session.commit()

    # Tuesday: the overdue nudge fires with priority over the (also-stale)
    # workout nudge, which the per-reservation gate then suppresses (V4 §10).
    send, calls = _fake_send()
    sent = await pro.evaluate_user(session, user.id, now=TUE, send=send)
    await session.commit()
    assert sent == [NudgeKind.overdue]
    assert "Overdue: 1." in calls[0][1]

    # Two hours later (past the 120-min default interval) the deferred
    # workout nudge is finally allowed to fire for the day.
    send2, calls2 = _fake_send()
    assert (
        await pro.evaluate_user(session, user.id, now=TUE + timedelta(hours=2), send=send2)
        == [NudgeKind.workout]
    )
    await session.commit()

    # A further pass the same day: both nudges are now deduped.
    send3, calls3 = _fake_send()
    assert (
        await pro.evaluate_user(session, user.id, now=TUE + timedelta(hours=4), send=send3)
        == []
    )
    await session.commit()
    assert calls3 == []

    # Next day: the overdue item is still late, so the overdue nudge returns
    # (again taking priority over the workout nudge).
    send4, _ = _fake_send()
    assert (
        await pro.evaluate_user(session, user.id, now=WED, send=send4)
        == [NudgeKind.overdue]
    )
    await session.commit()


async def test_overdue_nudge_toggle_off(session: AsyncSession) -> None:
    user = await _user(session)
    settings = await _settings(session, user)
    settings.overdue_nudge_enabled = False
    await session.commit()
    await _item(session, user, title="Late one", status="scheduled", due_at=MON)
    await session.commit()

    send, _ = _fake_send()
    sent = await pro.evaluate_user(session, user.id, now=TUE, send=send)
    await session.commit()
    assert sent == [NudgeKind.workout]


async def test_overdue_nudge_ignores_future_and_completed(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    # Future due date: not overdue. Completed past-due item: already done.
    await _item(
        session, user, title="Later", status="scheduled", due_at=TUE + timedelta(days=3)
    )
    await _item(
        session,
        user,
        title="Done late",
        status="completed",
        due_at=MON,
        completed_at=TUE - timedelta(hours=1),
    )
    await session.commit()

    send, _ = _fake_send()
    sent = await pro.evaluate_user(session, user.id, now=TUE, send=send)
    await session.commit()
    assert NudgeKind.overdue not in sent
    assert sent == [NudgeKind.workout]


# ---------------------------------------------------------------------------
# Anti-spam gates
# ---------------------------------------------------------------------------


async def test_quiet_hours_wrap_midnight(session: AsyncSession) -> None:
    user = await _user(session)  # defaults: quiet 22:00 -> 08:00

    # 23:00 local: suppressed.
    send, calls = _fake_send()
    assert (
        await pro.evaluate_user(
            session, user.id, now=MON.replace(hour=23, minute=30), send=send
        )
        == []
    )
    await session.commit()
    assert calls == []

    # 07:59 (wrap side): suppressed.
    send2, calls2 = _fake_send()
    assert (
        await pro.evaluate_user(
            session, user.id, now=MON.replace(hour=7, minute=59), send=send2
        )
        == []
    )
    await session.commit()
    assert calls2 == []

    # 08:00: outside quiet hours, fires.
    send3, _ = _fake_send()
    assert (
        NudgeKind.workout
        in await pro.evaluate_user(session, user.id, now=MON.replace(hour=8, minute=0), send=send3)
    )
    await session.commit()


async def test_max_nudges_per_day_cap(session: AsyncSession) -> None:
    user = await _user(session)
    settings = await _settings(session, user)
    settings.max_nudges_per_day = 1
    # Weekly-review content so Monday has two eligible triggers.
    await _item(
        session,
        user,
        title="Done thing",
        status="completed",
        completed_at=MON - timedelta(hours=5),
    )
    await session.commit()

    # Monday: two triggers are eligible, but max_nudges_per_day=1 caps the
    # pass at a single nudge (the highest-priority one, the weekly review).
    send, _ = _fake_send()
    sent = await pro.evaluate_user(session, user.id, now=MON, send=send)
    assert sent == [NudgeKind.weekly_review]
    await session.commit()

    # Drop only the workout dedupe row so the workout trigger is eligible
    # again; with the daily count already at the cap, nothing may send.
    await session.execute(
        delete(NudgeDelivery).where(
            NudgeDelivery.user_id == user.id,
            NudgeDelivery.kind == NudgeKind.workout,
        )
    )
    await session.commit()
    send2, calls2 = _fake_send()
    assert (
        await pro.evaluate_user(session, user.id, now=MON + timedelta(hours=2), send=send2)
        == []
    )
    await session.commit()
    assert calls2 == []


async def test_min_interval_suppresses_recent_nudges(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    settings = await _settings(session, user)
    settings.min_interval_minutes = 120
    # Weekly-review content so Monday has two eligible triggers.
    await _item(
        session,
        user,
        title="Done thing",
        status="completed",
        completed_at=MON - timedelta(hours=5),
    )
    await session.commit()

    # Monday: only the highest-priority trigger (weekly review) sends; the
    # min-interval gate re-check suppresses the (also-eligible) workout nudge
    # in the same pass. The weekly row is the min-interval record below.
    send, _ = _fake_send()
    sent = await pro.evaluate_user(session, user.id, now=MON, send=send)
    await session.commit()
    assert sent == [NudgeKind.weekly_review]

    # Drop only the workout dedupe row so that trigger is eligible again;
    # the weekly delivery row keeps supplying the min-interval gate.
    await session.execute(
        delete(NudgeDelivery).where(
            NudgeDelivery.user_id == user.id,
            NudgeDelivery.kind == NudgeKind.workout,
        )
    )
    await session.commit()

    send2, calls2 = _fake_send()
    assert (
        await pro.evaluate_user(
            session, user.id, now=MON + timedelta(minutes=30), send=send2
        )
        == []
    )
    await session.commit()
    assert calls2 == []

    # After the interval elapses the nudge is allowed again.
    send3, _ = _fake_send()
    assert (
        await pro.evaluate_user(
            session, user.id, now=MON + timedelta(hours=3), send=send3
        )
        == [NudgeKind.workout]
    )
    await session.commit()


async def test_disabled_settings_send_nothing(session: AsyncSession) -> None:
    user = await _user(session)
    settings = await _settings(session, user)
    settings.enabled = False

    send, calls = _fake_send()
    assert await pro.evaluate_user(session, user.id, now=MON, send=send) == []
    await session.commit()
    assert calls == []
    assert await _kinds(session, user.id) == []


async def test_evaluation_is_user_scoped(session: AsyncSession) -> None:
    a = await _user(session, user_id=91)
    b = await _user(session, user_id=92)
    # A has a fresh workout; B has none.
    await workouts_service.log_workout(session, a, name="Fresh", started_at=TUE - timedelta(hours=1))
    await session.commit()

    send, calls = _fake_send()
    sent_a = await pro.evaluate_user(session, a.id, now=TUE, send=send)
    sent_b = await pro.evaluate_user(session, b.id, now=TUE, send=send)
    await session.commit()

    assert sent_a == []  # A's fresh workout suppresses the only trigger
    assert NudgeKind.workout in sent_b  # B is unaffected by A's workout
    assert {c[0] for c in calls} == {b.id}
    # A's dedupe rows do not shadow B's.
    b_rows = (
        await session.scalars(
            select(NudgeDelivery).where(NudgeDelivery.user_id == b.id)
        )
    ).all()
    assert {r.kind for r in b_rows} == {NudgeKind.workout}


# ---------------------------------------------------------------------------
# Pending action expiry
# ---------------------------------------------------------------------------


async def _action(
    session: AsyncSession,
    user: User,
    *,
    status: str,
    expires_at: datetime | None,
) -> PendingAction:
    action = PendingAction(
        user_id=user.id,
        kind="test",
        payload={},
        summary="x",
        status=status,
        expires_at=expires_at,
    )
    session.add(action)
    await session.flush()
    return action


async def test_expire_stale_actions_marks_only_expired(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    now = TUE
    past = now - timedelta(hours=1)
    future = now + timedelta(hours=1)
    proposed_past = await _action(
        session, user, status=ActionStatus.proposed.value, expires_at=past
    )
    confirmed_past = await _action(
        session, user, status=ActionStatus.confirmed.value, expires_at=past
    )
    proposed_future = await _action(
        session, user, status=ActionStatus.proposed.value, expires_at=future
    )
    rejected_past = await _action(
        session, user, status=ActionStatus.rejected.value, expires_at=past
    )
    no_expiry = await _action(
        session, user, status=ActionStatus.proposed.value, expires_at=None
    )
    await session.commit()

    assert await pro.expire_stale_actions(session, now=now) == 2
    # The bulk UPDATE leaves the ORM instances stale: refetch by id.
    rows = {
        r.id: r
        for r in (
            await session.scalars(
                select(PendingAction)
                .where(
                    PendingAction.id.in_(
                        [
                            proposed_past.id,
                            confirmed_past.id,
                            proposed_future.id,
                            rejected_past.id,
                            no_expiry.id,
                        ]
                    )
                )
            )
        ).all()
    }
    assert rows[proposed_past.id].status == ActionStatus.expired.value
    assert rows[confirmed_past.id].status == ActionStatus.expired.value
    assert rows[proposed_past.id].expired_at == now
    assert rows[proposed_future.id].status == ActionStatus.proposed.value
    assert rows[rejected_past.id].status == ActionStatus.rejected.value
    assert rows[no_expiry.id].status == ActionStatus.proposed.value

    # Idempotent: nothing left to expire.
    assert await pro.expire_stale_actions(session, now=now) == 0
    await session.commit()


# ---------------------------------------------------------------------------
# Worker pass
# ---------------------------------------------------------------------------


async def test_run_proactive_pass_isolates_user_failure(
    session: AsyncSession,
) -> None:
    a = await _user(session, user_id=91)
    b = await _user(session, user_id=92)
    await session.commit()
    # Capture PKs: the pass's internal rollback expires ORM state.
    a_id, b_id = a.id, b.id

    send, calls = _fake_send(fail_for={a_id})
    result = await pro.run_proactive_pass(session, now=TUE, send=send)
    await session.commit()

    # B's nudges landed; A's send failed.
    assert result["nudges_sent"] == 1
    assert result["actions_expired"] == 0
    assert {c[0] for c in calls} == {b_id}
    assert NudgeKind.workout in await _kinds(session, b_id)

    # At-most-once: A's dedupe row was committed BEFORE the (failed) send,
    # so it is lost — the next pass must NOT re-send it (a nudge is a
    # convenience, not a commitment like a reminder or digest).
    assert NudgeKind.workout in await _kinds(session, a_id)
    send2, calls2 = _fake_send()
    result2 = await pro.run_proactive_pass(session, now=TUE, send=send2)
    await session.commit()
    assert result2["nudges_sent"] == 0
    assert calls2 == []


# ---------------------------------------------------------------------------
# Concurrency (V3 P40)
# ---------------------------------------------------------------------------


async def test_concurrent_sessions_nudge_exactly_once(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    user = await _user(session)
    # Pre-create the settings row so both sessions take the FOR UPDATE path
    # on an existing row (a first-run create race is a pass-level retry).
    await _settings(session, user)
    user_id = user.id
    factory = async_sessionmaker(engine, expire_on_commit=False)
    send, calls = _fake_send()

    async def one() -> list[str]:
        async with factory() as other:
            sent = await pro.evaluate_user(other, user_id, now=TUE, send=send)
            await other.commit()
            return sent

    # Two worker sessions evaluate the same user at the same instant.
    results = await asyncio.gather(one(), one())
    total = [kind for kinds in results for kind in kinds]
    assert total.count(NudgeKind.workout) == 1
    assert len(calls) == 1
    # Exactly one durable delivery row for the period.
    rows = (
        await session.scalars(
            select(NudgeDelivery).where(NudgeDelivery.user_id == user_id)
        )
    ).all()
    assert [r.kind for r in rows] == [NudgeKind.workout]
    assert rows[0].period_key == TUE.date().isoformat()


async def _run_concurrent(
    engine: AsyncEngine,
    user_id: int,
    now: datetime,
    send,
    sessions: int = 2,
    *,
    return_exceptions: bool = False,
):
    """Run ``sessions`` independent sessions evaluating the same user."""
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def one() -> list[str]:
        async with factory() as other:
            sent = await pro.evaluate_user(other, user_id, now=now, send=send)
            await other.commit()
            return sent

    return await asyncio.gather(
        *(one() for _ in range(sessions)), return_exceptions=return_exceptions
    )


async def _delivery_rows(session: AsyncSession, user_id: int) -> list[NudgeDelivery]:
    return (
        (
            await session.scalars(
                select(NudgeDelivery).where(NudgeDelivery.user_id == user_id)
            )
        )
        .all()
    )


# ---------------------------------------------------------------------------
# V4 §11: concurrency under real PostgreSQL
# ---------------------------------------------------------------------------


async def test_concurrent_passes_enforce_max_nudges_per_day(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """A daily cap of 1 means concurrent passes send exactly one nudge total."""
    user = await _user(session, user_id=201)
    settings = await _settings(session, user)
    settings.max_nudges_per_day = 1
    await session.commit()
    # Both an overdue item and a stale workout are eligible; the weekly
    # review (Monday) outranks both, and the cap must stop the rest.
    await _item(session, user, title="Late", status="scheduled", due_at=MON - timedelta(days=1))
    await session.commit()
    user_id = user.id

    send, calls = _fake_send()
    results = await _run_concurrent(engine, user_id, MON, send, sessions=2)
    total = [kind for kinds in results for kind in kinds]
    assert total == [NudgeKind.weekly_review]
    assert len(calls) == 1
    assert len(await _delivery_rows(session, user_id)) == 1


async def test_concurrent_passes_min_interval_suppresses_same_day(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """The first-sent nudge of a pass suppresses the rest via the interval gate.

    On a weekday with both an overdue item and a stale workout, the overdue
    nudge outranks the workout; a concurrent session must not slip a second
    nudge through inside the minimum interval.
    """
    user = await _user(session, user_id=202)
    await _settings(session, user)
    await _item(session, user, title="Late", status="scheduled", due_at=TUE - timedelta(days=1))
    await session.commit()
    user_id = user.id

    send, calls = _fake_send()
    results = await _run_concurrent(engine, user_id, TUE, send, sessions=2)
    total = [kind for kinds in results for kind in kinds]
    assert total == [NudgeKind.overdue]
    assert len(calls) == 1
    assert [r.kind for r in await _delivery_rows(session, user_id)] == [NudgeKind.overdue]


async def test_concurrent_weekly_review_uses_iso_week_key(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """Concurrent Monday passes dedupe on the ISO week, not a rolling window."""
    user = await _user(session, user_id=203)
    await _settings(session, user)
    await _item(
        session, user, title="Done", status="completed", completed_at=MON - timedelta(hours=2)
    )
    await session.commit()
    user_id = user.id
    expected_week = f"{MON.isocalendar().year}-W{MON.isocalendar().week:02d}"

    send, calls = _fake_send()
    results = await _run_concurrent(engine, user_id, MON, send, sessions=2)
    total = [kind for kinds in results for kind in kinds]
    assert total == [NudgeKind.weekly_review]
    assert len(calls) == 1
    rows = await _delivery_rows(session, user_id)
    assert len(rows) == 1
    assert rows[0].period_key == expected_week


async def test_concurrent_passes_per_user_isolation(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """Two users evaluated concurrently each get exactly their own nudge."""
    a = await _user(session, user_id=204)
    b = await _user(session, user_id=205)
    await _settings(session, a)
    await _settings(session, b)
    a_id, b_id = a.id, b.id

    # Interleave sessions for both users in one gather so the two users'
    # passes truly run at the same instant.
    send, calls = _fake_send()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def one(user_id: int) -> list[str]:
        async with factory() as other:
            sent = await pro.evaluate_user(other, user_id, now=TUE, send=send)
            await other.commit()
            return sent

    results = await asyncio.gather(
        one(a_id), one(b_id), one(a_id), one(b_id)
    )

    assert [k for ks in results for k in ks].count(NudgeKind.workout) == 2
    assert len(calls) == 2
    assert sorted(cid for cid, _ in calls) == sorted([a_id, b_id])
    assert [r.kind for r in await _delivery_rows(session, a_id)] == [NudgeKind.workout]
    assert [r.kind for r in await _delivery_rows(session, b_id)] == [NudgeKind.workout]


async def test_concurrent_passes_at_most_once_when_send_fails(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """A failed send after the reservation is NOT retried on the next pass."""
    user = await _user(session, user_id=206)
    await _settings(session, user)
    user_id = user.id

    # The send raises for this user; the reservation is committed first, so
    # the nudge is lost, not duplicated. gather returns the raised error for
    # the winning session and [] for the deduped one.
    send, _ = _fake_send(fail_for={user_id})
    await _run_concurrent(engine, user_id, TUE, send, sessions=2, return_exceptions=True)

    # The durable reservation exists (committed before the failed send).
    assert [r.kind for r in await _delivery_rows(session, user_id)] == [NudgeKind.workout]

    # A healthy next pass must NOT re-send: at-most-once.
    healthy, healthy_calls = _fake_send()
    assert await pro.evaluate_user(session, user_id, now=TUE, send=healthy) == []
    await session.commit()
    assert healthy_calls == []


async def test_many_concurrent_passes_single_kind_exactly_once(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """Four simultaneous sessions racing for one nudge send it exactly once."""
    user = await _user(session, user_id=207)
    await _settings(session, user)
    user_id = user.id

    send, calls = _fake_send()
    results = await _run_concurrent(engine, user_id, TUE, send, sessions=4)
    total = [kind for kinds in results for kind in kinds]
    assert total.count(NudgeKind.workout) == 1
    assert len(calls) == 1
    assert len(await _delivery_rows(session, user_id)) == 1

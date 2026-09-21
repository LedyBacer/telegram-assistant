"""User facts lifecycle tests (SPEC §14) — real PostgreSQL."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.bot.callbacks import FactCallback
from assistant.bot.handlers import cmd_facts, cmd_remember, on_fact
from assistant.models.facts import FactStatus, UserFact
from assistant.models.users import User
from assistant.services import facts
from assistant.services.users import upsert_user


async def _user(session: AsyncSession, user_id: int = 61) -> User:
    user, _ = await upsert_user(session, user_id=user_id, first_name="F")
    await session.commit()
    return user


# ---------------------------------------------------------------------------
# Lifecycle (service)
# ---------------------------------------------------------------------------


async def test_propose_fact_starts_proposed(session: AsyncSession) -> None:
    user = await _user(session)
    fact = await facts.propose_fact(
        session, user, value="I prefer morning workouts", provenance="telegram"
    )
    await session.commit()

    assert fact.status == FactStatus.proposed.value
    assert fact.value == "I prefer morning workouts"
    assert fact.key == "i prefer morning workouts"
    assert fact.provenance == "telegram"
    assert fact.category == "general"


async def test_propose_fact_rejects_blank_and_overlong(session: AsyncSession) -> None:
    user = await _user(session)
    with pytest.raises(ValueError, match="required"):
        await facts.propose_fact(session, user, value="   ")
    with pytest.raises(ValueError, match="too long"):
        await facts.propose_fact(session, user, value="x" * (facts.MAX_FACT_LENGTH + 1))
    assert (await session.scalars(select(UserFact))).all() == []


async def test_confirm_then_confirm_is_idempotent(session: AsyncSession) -> None:
    user = await _user(session)
    fact = await facts.propose_fact(session, user, value="Facts")
    await session.commit()

    confirmed = await facts.confirm_fact(session, user, fact.id)
    assert confirmed is not None
    assert confirmed.status == FactStatus.confirmed.value
    again = await facts.confirm_fact(session, user, fact.id)
    assert again is not None
    assert again.status == FactStatus.confirmed.value


async def test_reject_fact(session: AsyncSession) -> None:
    user = await _user(session)
    fact = await facts.propose_fact(session, user, value="Not true")
    await session.commit()

    rejected = await facts.reject_fact(session, user, fact.id)
    assert rejected is not None
    assert rejected.status == FactStatus.rejected.value


async def test_supersede_creates_proposed_and_marks_old(session: AsyncSession) -> None:
    user = await _user(session)
    fact = await facts.propose_fact(session, user, value="old value")
    await facts.confirm_fact(session, user, fact.id)
    await session.commit()

    new = await facts.supersede_fact(session, user, fact.id, value="new value")
    await session.commit()

    assert new is not None
    assert new.status == FactStatus.proposed.value
    assert new.value == "new value"
    old = await session.get(UserFact, fact.id)
    assert old.status == FactStatus.superseded.value
    assert old.superseded_by == new.id


async def test_supersede_rejects_blank_value(session: AsyncSession) -> None:
    user = await _user(session)
    fact = await facts.propose_fact(session, user, value="value")
    await session.commit()
    with pytest.raises(ValueError, match="required"):
        await facts.supersede_fact(session, user, fact.id, value="  ")
    await session.rollback()


async def test_facts_are_scoped_to_owner(session: AsyncSession) -> None:
    user = await _user(session, user_id=61)
    other = await _user(session, user_id=62)
    fact = await facts.propose_fact(session, user, value="mine")
    await session.commit()

    assert await facts.get_fact(session, other, fact.id) is None
    assert await facts.confirm_fact(session, other, fact.id) is None
    assert await facts.supersede_fact(session, other, fact.id, value="x") is None
    assert await facts.delete_fact(session, other, fact.id) is False
    assert await session.get(UserFact, fact.id) is not None


async def test_delete_fact_and_list_filters(session: AsyncSession) -> None:
    user = await _user(session)
    f1 = await facts.propose_fact(session, user, value="one")
    f2 = await facts.propose_fact(session, user, value="two")
    await facts.confirm_fact(session, user, f2.id)
    await session.commit()

    listed = await facts.list_facts(session, user)
    assert {f.id for f in listed} == {f1.id, f2.id}
    confirmed = await facts.list_facts(session, user, status=FactStatus.confirmed)
    assert [f.id for f in confirmed] == [f2.id]

    assert await facts.delete_fact(session, user, f1.id) is True
    assert await facts.delete_fact(session, user, f1.id) is False
    assert await session.get(UserFact, f1.id) is None


async def test_confirmed_lines_excludes_unconfirmed(session: AsyncSession) -> None:
    user = await _user(session)
    a = await facts.propose_fact(session, user, value="proposed only")
    b = await facts.propose_fact(
        session, user, value="likes tea", category="preferences"
    )
    c = await facts.propose_fact(session, user, value="old routine")
    await facts.confirm_fact(session, user, b.id)
    await facts.confirm_fact(session, user, c.id)
    await facts.supersede_fact(session, user, c.id, value="new routine")
    await session.commit()

    lines = await facts.confirmed_lines(session, user)
    # "proposed only" was never confirmed and "old routine" is superseded,
    # so only the confirmed "likes tea" fact remains.
    assert lines == ["[preferences] likes tea"]
    assert a.id is not None  # proposed fact must never leak into context


# ---------------------------------------------------------------------------
# Bot wiring
# ---------------------------------------------------------------------------


def _fake_message(text: str, user_id: int = 61) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(
            id=user_id, first_name="F", last_name=None, username=None, is_bot=False
        ),
        answer=AsyncMock(),
    )


def _fake_fact_callback(user_id: int = 61) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(
            id=user_id, first_name="F", last_name=None, username=None, is_bot=False
        ),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


async def test_cmd_remember_proposes_and_asks_confirmation(
    session: AsyncSession,
) -> None:
    message = _fake_message("/remember I prefer morning workouts")
    await cmd_remember(message, session)
    await session.commit()

    message.answer.assert_awaited_once()
    assert "Proposed fact" in message.answer.await_args.args[0]
    fact = (await session.scalars(select(UserFact))).one()
    assert fact.status == FactStatus.proposed.value


async def test_cmd_remember_without_text_shows_usage(session: AsyncSession) -> None:
    message = _fake_message("/remember")
    await cmd_remember(message, session)
    await session.commit()

    message.answer.assert_awaited_once()
    assert "Usage" in message.answer.await_args.args[0]
    assert (await session.scalars(select(UserFact))).all() == []


async def test_cmd_facts_lists_stored_facts(session: AsyncSession) -> None:
    user = await _user(session)
    await facts.propose_fact(session, user, value="listed fact")
    await session.commit()

    message = _fake_message("/facts")
    await cmd_facts(message, session)
    await session.commit()

    message.answer.assert_awaited_once()
    assert "[proposed]" in message.answer.await_args.args[0]
    assert "listed fact" in message.answer.await_args.args[0]


async def test_cmd_facts_empty_state(session: AsyncSession) -> None:
    message = _fake_message("/facts")
    await cmd_facts(message, session)
    await session.commit()

    message.answer.assert_awaited_once()
    assert "no stored facts" in message.answer.await_args.args[0]


async def test_on_fact_confirm_reject_delete(session: AsyncSession) -> None:
    user = await _user(session)
    f1 = await facts.propose_fact(session, user, value="to confirm")
    f2 = await facts.propose_fact(session, user, value="to reject")
    f3 = await facts.propose_fact(session, user, value="to delete")
    await session.commit()

    callback = _fake_fact_callback()
    data = FactCallback(action="confirm", fact_id=f1.id)
    await on_fact(callback, data, session)
    callback = _fake_fact_callback()
    await on_fact(callback, FactCallback(action="reject", fact_id=f2.id), session)
    callback = _fake_fact_callback()
    await on_fact(callback, FactCallback(action="delete", fact_id=f3.id), session)
    await session.commit()

    assert (await session.get(UserFact, f1.id)).status == FactStatus.confirmed.value
    assert (await session.get(UserFact, f2.id)).status == FactStatus.rejected.value
    assert await session.get(UserFact, f3.id) is None

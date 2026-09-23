"""User facts lifecycle tests (SPEC §14) — real PostgreSQL."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.bot.callbacks import FactCallback
from assistant.bot.handlers import cmd_facts, cmd_remember, on_fact
from assistant.i18n import LocalizableError
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
    with pytest.raises(LocalizableError) as exc:
        await facts.propose_fact(session, user, value="   ")
    assert exc.value.key == "facts.err_required"
    with pytest.raises(LocalizableError) as exc:
        await facts.propose_fact(session, user, value="x" * (facts.MAX_FACT_LENGTH + 1))
    assert exc.value.key == "facts.err_too_long"
    assert exc.value.params["max"] == facts.MAX_FACT_LENGTH
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


async def test_supersede_creates_proposed_and_keeps_old_confirmed(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    fact = await facts.propose_fact(session, user, value="old value")
    await facts.confirm_fact(session, user, fact.id)
    await session.commit()

    new = await facts.supersede_fact(session, user, fact.id, value="new value")
    await session.commit()

    assert new is not None
    assert new.status == FactStatus.proposed.value
    assert new.value == "new value"
    assert new.replaces_fact_id == fact.id
    old = await session.get(UserFact, fact.id)
    # The trusted fact stays confirmed while its replacement is pending.
    assert old.status == FactStatus.confirmed.value
    assert old.superseded_by is None


async def test_confirming_replacement_atomically_supersedes_old(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    old = await facts.propose_fact(
        session, user, value="prefer meetings after 11"
    )
    await facts.confirm_fact(session, user, old.id)
    new = await facts.supersede_fact(
        session, user, old.id, value="prefer meetings before 10"
    )
    await session.commit()

    confirmed = await facts.confirm_fact(session, user, new.id)
    await session.commit()

    assert confirmed is not None
    assert confirmed.status == FactStatus.confirmed.value
    reloaded_old = await session.get(UserFact, old.id)
    assert reloaded_old.status == FactStatus.superseded.value
    assert reloaded_old.superseded_by == new.id


async def test_rejecting_replacement_keeps_old_confirmed(session: AsyncSession) -> None:
    user = await _user(session)
    old = await facts.propose_fact(session, user, value="old preference")
    await facts.confirm_fact(session, user, old.id)
    new = await facts.supersede_fact(session, user, old.id, value="new preference")
    await session.commit()

    await facts.reject_fact(session, user, new.id)
    await session.commit()

    assert (await session.get(UserFact, new.id)).status == FactStatus.rejected.value
    assert (await session.get(UserFact, old.id)).status == FactStatus.confirmed.value


async def test_deleting_replacement_keeps_old_confirmed(session: AsyncSession) -> None:
    user = await _user(session)
    old = await facts.propose_fact(session, user, value="old preference")
    await facts.confirm_fact(session, user, old.id)
    new = await facts.supersede_fact(session, user, old.id, value="new preference")
    await session.commit()

    assert await facts.delete_fact(session, user, new.id) is True
    await session.commit()

    assert await session.get(UserFact, new.id) is None
    assert (await session.get(UserFact, old.id)).status == FactStatus.confirmed.value


async def test_supersede_for_other_users_fact_is_noop(session: AsyncSession) -> None:
    user = await _user(session, user_id=61)
    other = await _user(session, user_id=62)
    old = await facts.propose_fact(session, user, value="mine")
    await facts.confirm_fact(session, user, old.id)
    await session.commit()

    # A replacement proposal for another user's fact does not touch it.
    assert await facts.supersede_fact(session, other, old.id, value="theirs") is None
    assert (await session.get(UserFact, old.id)).status == FactStatus.confirmed.value


async def test_supersede_rejects_blank_value(session: AsyncSession) -> None:
    user = await _user(session)
    fact = await facts.propose_fact(session, user, value="value")
    await session.commit()
    with pytest.raises(LocalizableError) as exc:
        await facts.supersede_fact(session, user, fact.id, value="  ")
    assert exc.value.key == "facts.err_required"
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
    replacement = await facts.supersede_fact(session, user, c.id, value="new routine")
    # Until the replacement is confirmed, "old routine" stays confirmed.
    assert (await session.get(UserFact, c.id)).status == FactStatus.confirmed.value
    await facts.confirm_fact(session, user, replacement.id)
    await session.commit()

    lines = await facts.confirmed_lines(session, user)
    # "proposed only" was never confirmed and "old routine" is superseded by
    # the now-confirmed replacement, so "likes tea" and "new routine" remain.
    assert lines == ["[preferences] likes tea", "new routine"]
    assert a.id is not None  # proposed fact must never leak into context


# ---------------------------------------------------------------------------
# Dedupe identity + replacement revalidation (SPEC §16)
# ---------------------------------------------------------------------------


async def test_dedupe_uses_hash_not_truncated_prefix(session: AsyncSession) -> None:
    """Two values sharing a 255-char prefix are distinct facts under the
    hash identity (the old truncated `key` would have collided)."""
    user = await _user(session)
    prefix = "w" * 255
    a = await facts.propose_fact(session, user, value=prefix + "-alpha")
    b = await facts.propose_fact(session, user, value=prefix + "-beta")
    await session.commit()

    # The display keys are identical (both truncated at 255) ...
    assert a.key == b.key
    # ... but the dedupe hashes differ, so both facts were kept.
    assert a.key_hash != b.key_hash
    assert {f.id for f in await facts.list_facts(session, user)} == {a.id, b.id}


async def test_dedupe_collapses_equivalent_normalized_values(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    first = await facts.propose_if_absent(
        session, user, value="Likes  tea"
    )
    assert first is not None
    # Same normalized value (case + whitespace-insensitive) is suppressed.
    dup = await facts.propose_if_absent(session, user, value="likes  tea")
    assert dup is None
    assert (await session.scalars(select(UserFact))).all() == [first]


async def test_rejection_suppression_is_permanent(session: AsyncSession) -> None:
    """A rejected fact blocks re-proposal of the same value until deleted
    (SPEC §16: rejection suppression is permanent, not time-bounded)."""
    user = await _user(session)
    fact = await facts.propose_if_absent(session, user, value="disliked claim")
    assert fact is not None
    await facts.reject_fact(session, user, fact.id)
    await session.commit()

    assert await facts.propose_if_absent(session, user, value="disliked claim") is None
    # Deleting the rejected row clears the suppression.
    assert await facts.delete_fact(session, user, fact.id) is True
    await session.commit()
    repropose = await facts.propose_if_absent(session, user, value="disliked claim")
    assert repropose is not None
    assert repropose.status == FactStatus.proposed.value


async def test_propose_if_absent_links_valid_replaces_fact(session: AsyncSession) -> None:
    user = await _user(session)
    old = await facts.propose_fact(session, user, value="meetings after 11")
    await facts.confirm_fact(session, user, old.id)
    await session.commit()

    new = await facts.propose_if_absent(
        session,
        user,
        value="meetings before 10",
        replaces_fact_id=old.id,
    )
    assert new is not None
    assert new.replaces_fact_id == old.id
    assert new.status == FactStatus.proposed.value


async def test_propose_if_absent_drops_invalid_replaces_fact(
    session: AsyncSession,
) -> None:
    """A model-referenced replaces_fact_id that fails ownership/state
    revalidation is dropped: the proposal is stored as a plain new fact."""
    user = await _user(session, user_id=61)
    other = await _user(session, user_id=62)

    # Another user's fact is not a valid target.
    foreign = await facts.propose_fact(session, other, value="other users fact")
    await facts.confirm_fact(session, other, foreign.id)
    # A rejected fact is not a valid target.
    rejected = await facts.propose_fact(session, user, value="rejected fact")
    await facts.reject_fact(session, user, rejected.id)
    await session.commit()

    for bad_id in (foreign.id, rejected.id, 999999999):
        new = await facts.propose_if_absent(
            session,
            user,
            value=f"new distinct value {bad_id}",
            replaces_fact_id=bad_id,
        )
        assert new is not None
        assert new.replaces_fact_id is None
    await session.commit()


async def test_confirmed_facts_orders_oldest_first(session: AsyncSession) -> None:
    user = await _user(session)
    a = await facts.propose_fact(session, user, value="a")
    b = await facts.propose_fact(session, user, value="b")
    await facts.confirm_fact(session, user, a.id)
    await facts.confirm_fact(session, user, b.id)
    await session.commit()

    confirmed = await facts.confirmed_facts(session, user)
    assert [f.id for f in confirmed] == [a.id, b.id]


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
    user = await _user(session)
    user.settings.language = "en"
    await session.commit()

    message = _fake_message("/remember I prefer morning workouts")
    await cmd_remember(message, session)
    await session.commit()

    message.answer.assert_awaited_once()
    assert "Proposed fact" in message.answer.await_args.args[0]
    fact = (await session.scalars(select(UserFact))).one()
    assert fact.status == FactStatus.proposed.value


async def test_cmd_remember_without_text_shows_usage(session: AsyncSession) -> None:
    user = await _user(session)
    user.settings.language = "en"
    await session.commit()

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
    user = await _user(session)
    user.settings.language = "en"
    await session.commit()

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

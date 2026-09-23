"""User facts / long-term memory lifecycle (SPEC §14).

Facts are proposed, never silently promoted: a new fact starts in the
``proposed`` state and only becomes ``confirmed`` (trusted context for chat)
after the user explicitly confirms it. Users can inspect and delete their own
facts; all access is scoped to the requesting user.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.i18n import LocalizableError
from assistant.models.facts import FactStatus, UserFact
from assistant.models.users import User

MAX_FACT_LENGTH = 2000


def _normalize(value: str) -> str:
    return " ".join(value.split()).lower()


def _fact_key(value: str) -> str:
    """Human-readable, truncated identifier (display/debugging only)."""
    return _normalize(value)[:255]


def _fact_hash(value: str) -> str:
    """Collision-resistant dedupe identity: stable digest of the normalized
    full value (SPEC §16). Two distinct facts sharing a 255-char prefix can
    no longer collide the way the truncated ``key`` could."""
    return hashlib.sha256(_normalize(value).encode("utf-8")).hexdigest()


async def propose_fact(
    session: AsyncSession,
    user: User,
    *,
    value: str,
    category: str = "general",
    provenance: str | None = None,
    confidence: float | None = None,
    replaces_fact_id: int | None = None,
) -> UserFact:
    """Create a new fact in the ``proposed`` state (flush only).

    ``replaces_fact_id`` optionally links the fact as a replacement for an
    existing one (SPEC §15); callers that trust an unvalidated reference
    should pass it through ``propose_if_absent`` instead.
    """
    value = (value or "").strip()
    if not value:
        raise LocalizableError("facts.err_required")
    if len(value) > MAX_FACT_LENGTH:
        raise LocalizableError("facts.err_too_long", max=MAX_FACT_LENGTH)
    if confidence is not None and not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1.")
    fact = UserFact(
        user_id=user.id,
        category=(category or "general")[:64],
        key=_fact_key(value),
        key_hash=_fact_hash(value),
        value=value,
        provenance=(provenance or "user")[:255],
        confidence=confidence,
        status=FactStatus.proposed.value,
        replaces_fact_id=replaces_fact_id,
    )
    session.add(fact)
    await session.flush()
    return fact


async def _revalidated_replaces_id(
    session: AsyncSession,
    user: User,
    replaces_fact_id: int | None,
) -> int | None:
    """Revalidate a model-referenced replacement target (SPEC §16).

    The model may cite an existing confirmed-fact id that is explicitly
    present in its memory context, but ownership and state are re-checked
    here: the referenced fact must belong to the same user and be in a
    live state (``proposed`` or ``confirmed``). Anything else — a missing
    id, another user's fact, an already ``rejected``/``superseded`` fact,
    or a self-reference — is treated as absent, so the proposal is stored
    as a plain new fact rather than trusted blindly.
    """
    if replaces_fact_id is None:
        return None
    old = await session.get(UserFact, replaces_fact_id)
    if (
        old is not None
        and old.user_id == user.id
        and old.status
        in (FactStatus.proposed.value, FactStatus.confirmed.value)
    ):
        return old.id
    return None


async def propose_if_absent(
    session: AsyncSession,
    user: User,
    *,
    value: str,
    category: str = "general",
    provenance: str | None = None,
    replaces_fact_id: int | None = None,
) -> UserFact | None:
    """Propose a fact for automatic memory capture (SPEC §14).

    Returns a new ``proposed`` fact, or ``None`` when the user already has a
    fact with the same normalized key in a live state (``proposed``,
    ``confirmed`` or ``rejected``) — so the assistant never re-asks about a
    fact the user already settled. A ``superseded`` fact does not block a
    fresh proposal (it allows updating a previously replaced fact).

    Dedupe compares the collision-resistant ``key_hash`` (SPEC §16), not the
    truncated ``key``. ``replaces_fact_id`` (SPEC §15) is revalidated for
    ownership and state before being linked; an invalid reference is simply
    dropped so the proposal is stored as a plain new fact.
    """
    value = (value or "").strip()
    if not value or len(value) > MAX_FACT_LENGTH:
        return None
    key_hash = _fact_hash(value)
    existing = (
        await session.scalars(
            select(UserFact).where(
                UserFact.user_id == user.id, UserFact.key_hash == key_hash
            )
        )
    ).all()
    live_states = (
        FactStatus.proposed.value,
        FactStatus.confirmed.value,
        FactStatus.rejected.value,
    )
    for fact in existing:
        if fact.status in live_states:
            return None
    return await propose_fact(
        session,
        user,
        value=value,
        category=category,
        provenance=provenance,
        replaces_fact_id=await _revalidated_replaces_id(
            session, user, replaces_fact_id
        ),
    )


async def get_fact(
    session: AsyncSession, user: User, fact_id: int
) -> UserFact | None:
    """Return a fact belonging to the user, if any."""
    fact = await session.get(UserFact, fact_id)
    if fact is None or fact.user_id != user.id:
        return None
    return fact


async def list_facts(
    session: AsyncSession,
    user: User,
    *,
    status: FactStatus | None = None,
    limit: int = 50,
) -> list[UserFact]:
    """List the user's facts, newest first, optionally filtered by status."""
    stmt = (
        select(UserFact)
        .where(UserFact.user_id == user.id)
        .order_by(UserFact.created_at.desc(), UserFact.id.desc())
        .limit(limit)
    )
    if status is not None:
        stmt = stmt.where(UserFact.status == status.value)
    return list((await session.scalars(stmt)).all())


async def confirm_fact(
    session: AsyncSession, user: User, fact_id: int
) -> UserFact | None:
    """Mark a proposed fact confirmed (idempotent; flush only).

    When the confirmed fact is a replacement (``replaces_fact_id`` set), the
    referenced fact is atomically moved to ``superseded`` in the same flush,
    so a confirmed trusted fact only loses its status once its replacement is
    itself confirmed (SPEC §15).
    """
    fact = await get_fact(session, user, fact_id)
    if fact is None:
        return None
    if fact.status == FactStatus.proposed.value:
        fact.status = FactStatus.confirmed.value
        if fact.replaces_fact_id is not None and fact.replaces_fact_id != fact.id:
            old = await session.get(UserFact, fact.replaces_fact_id)
            if (
                old is not None
                and old.user_id == user.id
                and old.status
                in (FactStatus.proposed.value, FactStatus.confirmed.value)
            ):
                old.status = FactStatus.superseded.value
                old.superseded_by = fact.id
        await session.flush()
    return fact


async def reject_fact(
    session: AsyncSession, user: User, fact_id: int
) -> UserFact | None:
    """Mark a proposed fact rejected (idempotent; flush only)."""
    fact = await get_fact(session, user, fact_id)
    if fact is None:
        return None
    if fact.status == FactStatus.proposed.value:
        fact.status = FactStatus.rejected.value
        await session.flush()
    return fact


async def supersede_fact(
    session: AsyncSession,
    user: User,
    fact_id: int,
    *,
    value: str,
    category: str | None = None,
    provenance: str | None = None,
) -> UserFact | None:
    """Propose a replacement for a fact (SPEC §15).

    The referenced fact keeps its current state — a ``confirmed`` fact stays
    confirmed — while the new value is created as ``proposed`` and linked via
    ``replaces_fact_id``. The replacement only takes effect (atomically
    superseding the referenced fact) once the user confirms the new fact, so a
    trusted fact is never demoted while its replacement is pending.
    """
    old = await get_fact(session, user, fact_id)
    if old is None:
        return None
    if old.status not in (
        FactStatus.proposed.value,
        FactStatus.confirmed.value,
    ):
        return None
    value = value.strip()
    if not value:
        raise LocalizableError("facts.err_required")
    if len(value) > MAX_FACT_LENGTH:
        raise LocalizableError("facts.err_too_long", max=MAX_FACT_LENGTH)
    new = UserFact(
        user_id=user.id,
        category=(category or old.category)[:64],
        key=_fact_key(value),
        key_hash=_fact_hash(value),
        value=value,
        provenance=(provenance or "user")[:255],
        status=FactStatus.proposed.value,
        replaces_fact_id=old.id,
    )
    session.add(new)
    await session.flush()
    return new


async def delete_fact(session: AsyncSession, user: User, fact_id: int) -> bool:
    """Delete a fact belonging to the user. Returns True if deleted."""
    fact = await get_fact(session, user, fact_id)
    if fact is None:
        return False
    await session.delete(fact)
    await session.flush()
    return True


async def confirmed_facts(
    session: AsyncSession, user: User, *, limit: int = 20
) -> list[UserFact]:
    """The user's confirmed facts, oldest first (stable reading order)."""
    facts = await list_facts(session, user, status=FactStatus.confirmed, limit=limit)
    return list(reversed(facts))


async def confirmed_lines(
    session: AsyncSession, user: User, *, limit: int = 20
) -> list[str]:
    """Confirmed facts rendered as context lines for chat (SPEC §14-15)."""
    lines: list[str] = []
    for fact in await confirmed_facts(session, user, limit=limit):
        prefix = f"[{fact.category}] " if fact.category != "general" else ""
        lines.append(f"{prefix}{fact.value}")
    return lines

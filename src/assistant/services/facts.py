"""User facts / long-term memory lifecycle (SPEC §14).

Facts are proposed, never silently promoted: a new fact starts in the
``proposed`` state and only becomes ``confirmed`` (trusted context for chat)
after the user explicitly confirms it. Users can inspect and delete their own
facts; all access is scoped to the requesting user.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.models.facts import FactStatus, UserFact
from assistant.models.users import User

MAX_FACT_LENGTH = 2000


def _fact_key(value: str) -> str:
    """Normalized, collision-tolerant identity for a fact's content."""
    return " ".join(value.split()).lower()[:255]


async def propose_fact(
    session: AsyncSession,
    user: User,
    *,
    value: str,
    category: str = "general",
    provenance: str | None = None,
    confidence: float | None = None,
) -> UserFact:
    """Create a new fact in the ``proposed`` state (flush only)."""
    value = (value or "").strip()
    if not value:
        raise ValueError("Fact value is required.")
    if len(value) > MAX_FACT_LENGTH:
        raise ValueError(f"Fact value is too long (max {MAX_FACT_LENGTH}).")
    if confidence is not None and not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1.")
    fact = UserFact(
        user_id=user.id,
        category=(category or "general")[:64],
        key=_fact_key(value),
        value=value,
        provenance=(provenance or "user")[:255],
        confidence=confidence,
        status=FactStatus.proposed.value,
    )
    session.add(fact)
    await session.flush()
    return fact


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
    """Mark a proposed fact confirmed (idempotent; flush only)."""
    fact = await get_fact(session, user, fact_id)
    if fact is None:
        return None
    if fact.status == FactStatus.proposed.value:
        fact.status = FactStatus.confirmed.value
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
    """Replace a fact's value: the old one becomes ``superseded`` and a new
    ``proposed`` fact is created for the user to confirm (flush only)."""
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
        raise ValueError("Fact value is required.")
    if len(value) > MAX_FACT_LENGTH:
        raise ValueError(f"Fact value is too long (max {MAX_FACT_LENGTH}).")
    new = UserFact(
        user_id=user.id,
        category=(category or old.category)[:64],
        key=_fact_key(value),
        value=value,
        provenance=(provenance or "user")[:255],
        status=FactStatus.proposed.value,
    )
    session.add(new)
    await session.flush()
    old.status = FactStatus.superseded.value
    old.superseded_by = new.id
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


async def confirmed_lines(
    session: AsyncSession, user: User, *, limit: int = 20
) -> list[str]:
    """Confirmed facts rendered as context lines for chat (SPEC §14-15)."""
    facts = await list_facts(session, user, status=FactStatus.confirmed, limit=limit)
    lines: list[str] = []
    for fact in reversed(facts):  # oldest first: stable reading order
        prefix = f"[{fact.category}] " if fact.category != "general" else ""
        lines.append(f"{prefix}{fact.value}")
    return lines

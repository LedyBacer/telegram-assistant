"""Fact (memory) endpoints (SPEC §14)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.api.auth import get_current_user
from assistant.api.routes.common import _bad_request, _not_found
from assistant.api.schemas import FactCreate, FactOut, FactSupersede
from assistant.db import get_session
from assistant.models.facts import FactStatus
from assistant.models.users import User
from assistant.services import facts as facts_service

router = APIRouter(tags=["miniapp"])


@router.get("/facts", response_model=list[FactOut])
async def list_facts(
    status: FactStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[FactOut]:
    facts = await facts_service.list_facts(session, user, status=status, limit=limit)
    return [FactOut.model_validate(f) for f in facts]


@router.post("/facts", response_model=FactOut, status_code=201)
async def create_fact(
    body: FactCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FactOut:
    try:
        fact = await facts_service.propose_fact(
            session,
            user,
            value=body.value,
            category=body.category or "general",
            provenance="miniapp",
            confidence=body.confidence,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    await session.commit()
    await session.refresh(fact)
    return FactOut.model_validate(fact)


@router.post("/facts/{fact_id}/confirm", response_model=FactOut)
async def confirm_fact(
    fact_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FactOut:
    fact = await facts_service.confirm_fact(session, user, fact_id)
    if fact is None:
        raise _not_found()
    await session.commit()
    await session.refresh(fact)
    return FactOut.model_validate(fact)


@router.post("/facts/{fact_id}/reject", response_model=FactOut)
async def reject_fact(
    fact_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FactOut:
    fact = await facts_service.reject_fact(session, user, fact_id)
    if fact is None:
        raise _not_found()
    await session.commit()
    await session.refresh(fact)
    return FactOut.model_validate(fact)


@router.delete("/facts/{fact_id}", status_code=204)
async def delete_fact(
    fact_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    deleted = await facts_service.delete_fact(session, user, fact_id)
    if not deleted:
        raise _not_found()
    await session.commit()
    return Response(status_code=204)


@router.post("/facts/{fact_id}/supersede", response_model=FactOut, status_code=201)
async def supersede_fact(
    fact_id: int,
    body: FactSupersede,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FactOut:
    """Propose a replacement: the referenced fact keeps its state, and the new
    value is created as ``proposed`` (linked via ``replaces_fact_id``); it
    only supersedes the old fact once the user confirms the new one."""
    try:
        fact = await facts_service.supersede_fact(
            session,
            user,
            fact_id,
            value=body.value,
            category=body.category,
            provenance="miniapp",
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    if fact is None:
        raise _not_found()
    await session.commit()
    await session.refresh(fact)
    return FactOut.model_validate(fact)

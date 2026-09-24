"""Authenticated Mini App API (SPEC §20) — composed from focused domain routers.

The public ``router`` (mounted at ``/api/v1`` by :mod:`assistant.api.main`) is
the concatenation of the per-domain routers below, included in the same order
the endpoints were originally declared so routing precedence and the OpenAPI
layout are unchanged. Every route is user-scoped: identity comes from verified
Telegram initData (:mod:`assistant.api.auth`) and all service calls are
user-scoped, so one user can never read or modify another's data.
"""

from __future__ import annotations

from fastapi import APIRouter

from assistant.api.routes import (
    actions,
    calendar,
    facts,
    files,
    i18n,
    me,
    reminders,
    settings,
    workouts,
)

router = APIRouter(prefix="/api/v1", tags=["miniapp"])

router.include_router(me.router)
router.include_router(calendar.router)
router.include_router(workouts.router)
router.include_router(files.router)
router.include_router(reminders.router)
router.include_router(facts.router)
router.include_router(actions.router)
router.include_router(settings.router)
router.include_router(i18n.router)

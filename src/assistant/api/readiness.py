"""Readiness probe: Postgres is a hard dependency; AI providers are degraded.

``/healthz`` (liveness) is a plain "process is up" answer. ``/readyz``
(readiness) verifies the one hard dependency — a reachable PostgreSQL — and
reports each AI provider's availability as a component. An unconfigured AI
provider is **degraded**, never *unready*: a chat-only deployment (no
embedding provider) is still ready to serve, and a missing chat key is a
misconfiguration surfaced as degraded rather than a readiness failure, so an
operator can see it without taking the service out of rotation.

The probe performs **no inference**: it checks Postgres connectivity
(``SELECT 1``) and reads provider configuration from settings only. It never
calls a chat or embedding endpoint, so it is cheap and safe to run on a
schedule.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from assistant.config import get_settings
from assistant.db import get_engine

# Bound the Postgres probe so a wedged database cannot hang the probe.
DB_PROBE_TIMEOUT_SECONDS = 3.0

# A readiness probe is an async () -> None check; the default runs
# ``SELECT 1``. Injectable in tests to simulate a down database.
DbProbe = Callable[[], Awaitable[None]]


async def _probe_postgres() -> None:
    engine: AsyncEngine = get_engine()
    async with asyncio.timeout(DB_PROBE_TIMEOUT_SECONDS):
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))


async def check_readiness(db_probe: DbProbe | None = None) -> tuple[bool, dict]:
    """Return ``(ready, payload)`` for the ``/readyz`` response.

    ``ready`` is True only when PostgreSQL is reachable. AI providers are
    reported as ``ok``/``degraded`` components and do not affect ``ready``.
    """
    settings = get_settings()
    probe = db_probe if db_probe is not None else _probe_postgres

    ready = True
    try:
        await probe()
        postgres_status = "ok"
    except Exception:
        postgres_status = "error"
        ready = False

    components = {
        "postgres": {"status": postgres_status},
        "ai_chat": {"status": "ok" if bool(settings.chat_api_key) else "degraded"},
        "ai_embedding": {"status": "ok" if settings.embedding_configured else "degraded"},
    }
    return ready, {"status": "ready" if ready else "not_ready", "components": components}

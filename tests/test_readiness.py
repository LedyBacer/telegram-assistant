"""P45 health/readiness probes.

``/healthz`` is liveness (process up). ``/readyz`` is readiness: 200 only when
PostgreSQL is reachable; AI providers are reported as configured/unconfigured
components (config-only) and never gate readiness. The probe performs no
inference.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://assistant:assistant@localhost:5432/assistant",
    ),
)
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

from assistant.api.main import create_app  # noqa: E402
from assistant.api.readiness import check_readiness  # noqa: E402
from assistant.config import get_settings  # noqa: E402
from assistant.db import get_session  # noqa: E402


@pytest_asyncio.fixture()
async def client(session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        yield c
    app.dependency_overrides.clear()


async def test_healthz_is_liveness(client: httpx.AsyncClient) -> None:
    res = await client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


async def test_readyz_ready_when_postgres_reachable(client: httpx.AsyncClient) -> None:
    res = await client.get("/readyz")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ready"
    assert body["components"]["postgres"]["status"] == "ok"


async def test_readyz_not_ready_when_postgres_down() -> None:
    async def _down() -> None:
        raise RuntimeError("postgres down")

    ready, payload = await check_readiness(db_probe=_down)
    assert ready is False
    assert payload["status"] == "not_ready"
    assert payload["components"]["postgres"]["status"] == "error"


async def test_readyz_ai_unconfigured_is_not_unready(monkeypatch) -> None:
    """An unconfigured AI provider is reported as unconfigured but keeps the
    service ready (a chat-only deployment is fully usable)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "chat_api_key", None)
    monkeypatch.setattr(settings, "embedding_api_key", None)

    ready, payload = await check_readiness()  # real, reachable test Postgres
    assert ready is True
    assert payload["status"] == "ready"
    assert payload["components"]["ai_chat"]["status"] == "unconfigured"
    assert payload["components"]["ai_embedding"]["status"] == "unconfigured"


async def test_readyz_ai_configured_when_credentials_present() -> None:
    _, payload = await check_readiness()
    # The test env provides OPENAI_API_KEY, so both providers report configured.
    assert payload["components"]["ai_chat"]["status"] == "configured"
    assert payload["components"]["ai_embedding"]["status"] == "configured"

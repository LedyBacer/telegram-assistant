"""Mini App shell-level tests: root redirect, static entry, file upload, and the
auth boundary.

The production ``create_app()`` has **no** test-auth bypass: it authenticates
only with a signed Telegram ``initData``, and setting ``ASSISTANT_TEST_AUTH``
in the environment cannot enable one. The deterministic test user is installed
by the separate test-only entry point ``assistant.api.testing.create_test_app``
(used by Playwright), and a test here proves the production app rejects that
bypass while the test-only app applies it.

Environment is set before importing the app so ``get_settings()`` reads test
values; no real Telegram/OpenAI credentials are used.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://assistant:assistant@localhost:5432/assistant",
)
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TEST-TOKEN"
os.environ["OPENAI_API_KEY"] = "test-key"

from assistant.api.main import create_app  # noqa: E402
from assistant.config import get_settings  # noqa: E402
from assistant.db import get_session  # noqa: E402
from test_init_data import make_init_data  # noqa: E402

INIT_DATA = make_init_data(
    user={"id": 777, "first_name": "T", "last_name": "X", "username": "tx"}
)
HEADERS = {"X-Telegram-Init-Data": INIT_DATA}


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


# ---------------------------------------------------------------------------
# Root URL + static entry
# ---------------------------------------------------------------------------


async def test_root_redirects_to_miniapp(client: httpx.AsyncClient) -> None:
    res = await client.get("/")
    assert res.status_code == 307
    assert res.headers["location"] == "/miniapp"


async def test_miniapp_serves_index(client: httpx.AsyncClient) -> None:
    res = await client.get("/miniapp")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    body = res.text
    assert '<script' in body  # an HTML document, not an error page


async def test_miniapp_assets_reachable(client: httpx.AsyncClient) -> None:
    # index.html references these same-origin assets; they must resolve 200.
    for asset in ("/miniapp/app.js", "/miniapp/styles.css"):
        res = await client.get(asset)
        assert res.status_code == 200, f"{asset} should be served"


# ---------------------------------------------------------------------------
# File upload (Mini App multipart path)
# ---------------------------------------------------------------------------


async def test_file_upload_creates_file(client: httpx.AsyncClient) -> None:
    res = await client.post(
        "/api/v1/files",
        headers=HEADERS,
        files={"file": ("notes.txt", b"hello e2e world", "text/plain")},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["original_filename"] == "notes.txt"
    assert body["mime_type"] == "text/plain"
    assert body["state"] == "queued"
    file_id = body["id"]

    listed = await client.get("/api/v1/files", headers=HEADERS)
    names = [f["original_filename"] for f in listed.json()]
    assert "notes.txt" in names

    # The upload is user-scoped: a different user cannot see it.
    other = make_init_data(user={"id": 888, "first_name": "O"})
    hidden = await client.get(
        "/api/v1/files", headers={"X-Telegram-Init-Data": other}
    )
    assert [f["id"] for f in hidden.json()] == []

    res = await client.delete(f"/api/v1/files/{file_id}", headers=HEADERS)
    assert res.status_code == 204


async def test_file_upload_rejects_unsupported_type(client: httpx.AsyncClient) -> None:
    res = await client.post(
        "/api/v1/files",
        headers=HEADERS,
        files={"file": ("virus.exe", b"MZ", "application/octet-stream")},
    )
    assert res.status_code == 201
    # Rejected uploads are persisted so the user can see why.
    assert res.json()["state"] == "rejected"


# ---------------------------------------------------------------------------
# Auth boundary: production has no bypass; the test-only entry point does
# ---------------------------------------------------------------------------


async def test_production_app_has_no_test_auth_bypass(
    session: AsyncSession,
) -> None:
    """The production ``create_app()`` authenticates only with initData and
    has no test-auth bypass: even with ``ASSISTANT_TEST_AUTH`` present in the
    environment, a request without valid initData is rejected with 401. The
    override lives solely in the test-only entry point, so it cannot be
    activated against the production app."""
    get_settings.cache_clear()
    os.environ["ASSISTANT_TEST_AUTH"] = "1"
    try:
        app = create_app()

        async def _override() -> AsyncIterator[AsyncSession]:
            yield session

        app.dependency_overrides[get_session] = _override
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            # No initData header: a real deployment must refuse this.
            assert (await c.get("/api/v1/me")).status_code == 401
    finally:
        del os.environ["ASSISTANT_TEST_AUTH"]
        get_settings.cache_clear()


async def test_test_only_entrypoint_installs_deterministic_user(
    session: AsyncSession,
) -> None:
    """``create_test_app()`` (the Playwright entry point) resolves the
    deterministic test user with no initData, so browser specs can exercise
    authenticated flows without a real Telegram identity."""
    from assistant.api.testing import create_test_app

    app = create_test_app()

    async def _override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        res = await c.get("/api/v1/me")
        assert res.status_code == 200
        assert res.json()["user"]["id"] == 999999

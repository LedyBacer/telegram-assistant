"""Test-only API entry point (Playwright E2E): ``uv run python e2e/support/test_app.py``.

The production app (``assistant.api.main``) authenticates every Mini App
request with a signed Telegram ``initData`` and has **no** test-auth bypass.
This module builds that same production app and then overrides the
``get_current_user`` dependency with a deterministic test user, so the
browser specs can exercise authenticated flows without a real Telegram user
or a signed ``initData``.

This file lives OUTSIDE ``src/`` on purpose (V4 §33): the production Docker
image copies only ``src/`` and installs only the ``assistant`` package, so
the test-auth override is structurally absent from every production
artifact. Nothing in the production runtime imports it; the production
:func:`create_app` never consults an environment flag to enable the
override. The override is applied only because the test harness (Playwright
``webServer`` in ``e2e/playwright.config.ts``) deliberately runs *this*
script.
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.api.auth import get_current_user
from assistant.api.main import create_app
from assistant.config import get_settings
from assistant.db import get_session
from assistant.logging import setup_logging
from assistant.models.users import User
from assistant.services.users import upsert_user

# Deterministic test identity shared by the browser specs and the seed helper.
TEST_USER_ID = 999999
TEST_USER_FIRST_NAME = "Test"
TEST_USER_LAST_NAME = "User"
TEST_USER_USERNAME = "e2e"


def install_test_auth(app: FastAPI) -> None:
    """Override ``get_current_user`` with the deterministic test user.

    Call this on a :func:`create_app` result to turn initData auth into a
    fixed identity for the E2E run. This is the *only* place the override
    exists; no production code path reaches it.
    """

    async def _test_user(session: AsyncSession = Depends(get_session)) -> User:
        user, _ = await upsert_user(
            session,
            user_id=TEST_USER_ID,
            first_name=TEST_USER_FIRST_NAME,
            last_name=TEST_USER_LAST_NAME,
            username=TEST_USER_USERNAME,
        )
        await session.commit()
        return user

    app.dependency_overrides[get_current_user] = _test_user


def create_test_app() -> FastAPI:
    """The production app with the deterministic test-user auth override."""
    app = create_app()
    install_test_auth(app)
    return app


app = create_test_app()


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    port = int(os.environ.get("ASSISTANT_API_PORT", "8000"))
    uvicorn.run("test_app:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()

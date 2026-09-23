"""FastAPI application entry point: ``python -m assistant.api.main``."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.api.auth import get_current_user
from assistant.api.routes import router as api_router
from assistant.config import get_settings
from assistant.db import dispose_engine, get_session
from assistant.logging import setup_logging
from assistant.models.users import User
from assistant.services.users import upsert_user


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()


def _install_test_auth(app: FastAPI, settings) -> None:
    """Replace the auth dependency with a deterministic test user.

    Only invoked when ``ASSISTANT_TEST_AUTH`` is set (Playwright E2E). Production
    never sets the flag, so a normal HTTP request can never trigger this path.
    """

    async def _test_user(session: AsyncSession = Depends(get_session)) -> User:
        user, _ = await upsert_user(
            session,
            user_id=settings.test_auth_user_id,
            first_name=settings.test_auth_first_name,
            last_name=settings.test_auth_last_name,
            username=settings.test_auth_username,
        )
        await session.commit()
        return user

    app.dependency_overrides[get_current_user] = _test_user


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Smart Personal Assistant", version="0.1.0", lifespan=_lifespan
    )
    app.include_router(api_router)

    if settings.test_auth_enabled:
        _install_test_auth(app, settings)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # Root URL: redirect to the Mini App entry point so a reverse proxy no
    # longer needs its own `/` → `/miniapp` workaround (SPEC: production URL).
    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/miniapp", status_code=307)

    miniapp_dir = Path(settings.miniapp_dir)
    if miniapp_dir.is_dir():
        index_file = miniapp_dir / "index.html"

        # Serve the entry point at exactly ``/miniapp`` (no trailing-slash
        # redirect) so the reverse proxy only needs the ``/`` → ``/miniapp`` hop.
        @app.get("/miniapp", include_in_schema=False)
        async def miniapp_index() -> FileResponse:
            return FileResponse(index_file, media_type="text/html")

        app.mount("/miniapp", StaticFiles(directory=miniapp_dir, html=True), name="miniapp")

    return app


app = create_app()


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    port = int(os.environ.get("ASSISTANT_API_PORT", "8000"))
    uvicorn.run("assistant.api.main:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()

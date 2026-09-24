"""FastAPI application entry point: ``python -m assistant.api.main``."""

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from assistant.api.readiness import check_readiness
from assistant.api.routes import router as api_router
from assistant.config import get_settings
from assistant.db import dispose_engine
from assistant.logging import log_context, setup_logging


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    """Build the production FastAPI app.

    Authentication is always the initData-verified ``get_current_user``
    dependency (see :mod:`assistant.api.auth`). There is **no** test-auth
    bypass here: a deterministic test user is installed only by the
    test-only entry point :func:`assistant.api.testing.create_test_app`,
    which Playwright's web server runs. The production app never consults
    an environment flag to enable any auth bypass, so setting such a flag
    in a (mis)configured environment cannot turn the real API into an
    open endpoint.
    """
    settings = get_settings()
    app = FastAPI(
        title="Smart Personal Assistant", version="0.1.0", lifespan=_lifespan
    )

    # Request/correlation id: reuse an incoming X-Request-Id (so a caller can
    # trace its request) or mint one; it is bound into the log context for the
    # whole request and echoed back in the response header (SPEC §23).
    @app.middleware("http")
    async def _request_id_middleware(request: Request, call_next) -> JSONResponse:
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        with log_context(request_id=request_id):
            response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response

    app.include_router(api_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # Readiness: 200 only when PostgreSQL is reachable. AI providers are
    # reported as ok/degraded components and never gate readiness; the probe
    # performs no inference (see assistant.api.readiness).
    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        ready, payload = await check_readiness()
        return JSONResponse(content=payload, status_code=200 if ready else 503)

    # Root URL: redirect to the Mini App entry point so a reverse proxy no
    # longer needs its own `/` → `/miniapp` workaround (SPEC: production URL).
    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/miniapp", status_code=307)

    miniapp_dir = Path(settings.miniapp_dir)
    if miniapp_dir.is_dir():
        index_file = miniapp_dir / "index.html"

        # Serve the entry point at exactly ``/miniapp`` (no trailing-slash
        # redirect) so the reverse proxy only needs the `/` → `/miniapp` hop.
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

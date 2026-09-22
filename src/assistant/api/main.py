"""FastAPI application entry point: ``python -m assistant.api.main``."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from assistant.api.routes import router as api_router
from assistant.config import get_settings
from assistant.db import dispose_engine
from assistant.logging import setup_logging


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Smart Personal Assistant", version="0.1.0", lifespan=_lifespan
    )
    app.include_router(api_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    miniapp_dir = Path(settings.miniapp_dir)
    if miniapp_dir.is_dir():
        app.mount("/miniapp", StaticFiles(directory=miniapp_dir, html=True), name="miniapp")

    return app


app = create_app()


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    uvicorn.run("assistant.api.main:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()

"""FastAPI application entry point: ``python -m assistant.api.main``."""

from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from assistant.config import get_settings
from assistant.logging import setup_logging


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Smart Personal Assistant", version="0.1.0")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
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

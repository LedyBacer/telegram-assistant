# Pinned uv version (matches the developer/CI tool) — the multi-stage copy
# avoids a network fetch and makes the build reproducible.
FROM ghcr.io/astral-sh/uv:0.12.17 AS uv

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

# Project metadata + the lockfile first (layer cache), then the source.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Install exactly what uv.lock resolves: no re-resolution, no dev group.
#   --frozen  fails the build if uv.lock is missing (production must never
#             resolve from pyproject.toml ranges at build time).
#   --no-dev  skips the pytest/ruff dev group.
# The acceptance suite's `uv lock --check` step separately fails if uv.lock
# drifts from pyproject.toml, so the lockfile cannot be silently bypassed.
RUN uv sync --frozen --no-dev

COPY alembic.ini ./
COPY alembic ./alembic
COPY miniapp ./miniapp

RUN useradd --create-home appuser \
    && mkdir -p /data/storage/files \
    && chown -R appuser:appuser /app /data
USER appuser
ENV HOME=/home/appuser \
    FILE_STORAGE_DIR=/data/storage/files

EXPOSE 8000

CMD ["python", "-m", "assistant.api.main"]

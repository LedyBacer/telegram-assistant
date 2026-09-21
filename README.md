# Telegram Assistant

Smart personal assistant and motivator delivered as a Telegram bot, REST API, and
Telegram Mini App. Built as a modular monolith: aiogram 3 bot, FastAPI, a durable
PostgreSQL-backed job worker, and OpenAI-compatible AI providers for structured
task drafting and document embeddings (pgvector).

## Runtime processes

| Process  | Entry point                   | Responsibility                                  |
| -------- | ----------------------------- | ----------------------------------------------- |
| `bot`    | `python -m assistant.bot.main`    | Telegram updates, commands, Mini App auth      |
| `api`    | `python -m assistant.api.main`    | FastAPI REST endpoints for the Mini App        |
| `worker` | `python -m assistant.worker.main` | Durable background jobs (SKIP LOCKED queue)    |
| `postgres` | PostgreSQL 17 + pgvector      | State, jobs, and vector search                 |

## Quick start (Docker)

```bash
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, PUBLIC_BASE_URL
docker compose up --build
```

## Local development

```bash
uv sync
cp .env.example .env
# point DATABASE_URL at a local PostgreSQL 17 with the pgvector extension
uv run alembic upgrade head
uv run python -m assistant.bot.main    # bot
uv run python -m assistant.api.main    # api
uv run python -m assistant.worker.main # worker
```

## Tests

```bash
uv run pytest
uv run ruff check .
```

Tests require a reachable PostgreSQL 17 with pgvector (see `DATABASE_URL`).

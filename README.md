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

## PostgreSQL setup

Docker: `docker compose up -d postgres` (healthchecked `pgvector/pgvector:pg17`,
data persisted in the `postgres_data` volume).

Local: any PostgreSQL 16+ with the `vector` extension
(`CREATE EXTENSION vector;`). The test/dev database `assistant` is used by
default (`DATABASE_URL` in `.env`); the suite also accepts
`TEST_DATABASE_URL`.

## Migrations

The schema is deployed exclusively through Alembic (never `create_all`):

```bash
uv run alembic upgrade head   # apply on a fresh/empty database
uv run alembic current        # inspect the applied revision
```

## Tests

```bash
uv run pytest           # full suite (needs a reachable PostgreSQL + pgvector)
uv run ruff check .     # lint gate
```

Tests require a reachable PostgreSQL 17 with pgvector (see `DATABASE_URL` /
`TEST_DATABASE_URL`). No real Telegram or OpenAI credentials are needed —
external AI/Telegram HTTP calls are mocked, and initData is signed locally.

## Mini App development

The Mini App is a static SPA (`miniapp/index.html` + `miniapp/app.js`,
Tailwind via CDN, vanilla JS) mounted by the API at `/miniapp`. Develop by
pointing `PUBLIC_BASE_URL` at the API (e.g. `http://localhost:8000`), open the
bot inside Telegram, and press the Mini App button — the bot links
`$PUBLIC_BASE_URL/miniapp`. Inside Telegram, `Telegram.WebApp.initData` is
sent with every request and verified server-side; outside Telegram the page
shows a hint to open it from the bot. For a phone-usable feel, keep the layout
single-column and test at 390 px width.

## Shutdown

- Foreground processes: `Ctrl-C` (bot and worker dispose their engines and
  close the bot session in their shutdown paths).
- Docker: `docker compose down` (keeps the database volume) or
  `docker compose down -v` (also drops the database volume).

## Backup considerations

- The single source of truth is PostgreSQL: `pg_dump` the `assistant`
  database (jobs, calendar, files metadata, facts, digests all live there).
- Uploaded file blobs live on disk in `storage/files` (compose mounts
  `./storage` on `api`, `bot`, `worker`, and `postgres`); back it up alongside
  the database dump, keyed by the server-generated storage keys.
- `.env` contains secrets — store it with the same care as the database
  credentials.

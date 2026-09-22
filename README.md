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
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN, CHAT_*/EMBEDDING_* vars, PUBLIC_BASE_URL
docker compose up --build
```

## Network exposure

Uvicorn is **never exposed directly to the network**:

- The `api` service publishes its port as `127.0.0.1:8000:8000`
  (loopback-only). Nothing else is published — PostgreSQL has **no**
  published port and is reachable only from inside the compose network.
- A future public Mini App is served through an **HTTPS reverse proxy**
  (e.g. Caddy/Traefik/nginx) that terminates TLS and forwards to
  `127.0.0.1:8000`. `PUBLIC_BASE_URL` must point at that proxy, not at the
  raw Uvicorn port.

`scripts/acceptance.sh` fails the run if any published port in
`docker-compose.yml` is not loopback-bound.

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

## AI providers (chat and embeddings)

Chat/generation and embeddings are configured **independently** and may be
served by two separate OpenAI-compatible servers (e.g. two llama.cpp
instances) — the application never assumes one host provides both:

- **Chat / structured generation** uses only `CHAT_BASE_URL`,
  `CHAT_API_KEY`, `CHAT_MODEL` (default `qwen3.5-9b-64k`),
  `CHAT_TIMEOUT_SECONDS` and `CHAT_THINKING_ENABLED`.
- **Embeddings / RAG** uses only `EMBEDDING_BASE_URL`,
  `EMBEDDING_API_KEY`, `EMBEDDING_MODEL` (default `multilingual-e5-small`)
  and `EMBEDDING_DIMENSIONS` (384). Vectors are stored as pgvector
  `vector(384)` with an HNSW cosine index.

Example (two llama.cpp servers):

```bash
CHAT_BASE_URL=http://llm-host:18085/v1
CHAT_API_KEY=replace-me
CHAT_MODEL=qwen3.5-9b-64k
CHAT_TIMEOUT_SECONDS=180
CHAT_THINKING_ENABLED=true

EMBEDDING_BASE_URL=http://llm-host:18086/v1
EMBEDDING_API_KEY=replace-me
EMBEDDING_MODEL=multilingual-e5-small
EMBEDDING_DIMENSIONS=384
```

`multilingual-e5-small` expects E5 prefixes, which the embedding provider
applies automatically and centrally: stored document chunks are embedded as
`passage: <text>` and search queries as `query: <text>`. The prefixes are
never persisted — chunk text and user queries are stored/returned in their
original form.

Embedding responses are validated against `EMBEDDING_DIMENSIONS`; a server
that returns vectors of a different size fails with a clear application
error instead of writing invalid data.

The embedding request uses only the OpenAI-compatible `model` + `input`
shape, so llama.cpp's `/v1/embeddings` is fully supported.

### Chat timeout and Qwen thinking mode

- **`CHAT_TIMEOUT_SECONDS`** (default `180`) — how long (seconds) the
  application waits for the chat provider to finish a completion before
  giving up. llama.cpp models can spend a long time generating, so the
  value is explicit instead of relying on the OpenAI client's implicit
  60-second default. On timeout the request fails cleanly with a localized
  error message; the timeout is logged with its configured value. The
  setting applies to chat and structured generation only — the embedding
  provider keeps its own (short) timeout.
- **`CHAT_THINKING_ENABLED`** (default `true`) — Qwen *thinking*
  (reasoning) mode, a llama.cpp/Qwen provider behavior, not a per-user
  preference. The mode is sent **explicitly** with every chat/structured
  completion via the documented llama.cpp OpenAI-compatible request field
  `chat_template_kwargs.enable_thinking` (through the OpenAI client's
  `extra_body`), never relying on a server default. `true` lets the model
  reason before answering (slower, better reasoning); `false` disables
  thinking (usually much lower latency, possibly reduced reasoning
  quality). It applies to normal assistant chat and structured task-draft
  generation; embeddings are unaffected. When thinking is enabled and a
  chat/structured request is about to run, the bot sends a short
  localized temporary status message ("Думаю…" / "Thinking…") in the
  user's persisted language and removes it as soon as the provider
  responds — including on timeout or provider error.

### Structured completion retries

Structured completion retries are bounded: at most 2 attempts in total. A
**malformed model response** (no JSON / schema mismatch) may be retried once
with corrective feedback. A **full inference timeout** is not a malformed
response: the provider is simply slow, so the request fails cleanly
(`AITimeoutError`) without immediately starting a second equally long
inference — the worst-case user wait for a structured call is therefore the
configured `CHAT_TIMEOUT_SECONDS`, not a multiple of it.

Legacy fallback: when the `CHAT_*` / `EMBEDDING_*` base-URL/key variables are
absent, `OPENAI_BASE_URL` / `OPENAI_API_KEY` are used for both providers.

## Languages (i18n)

The assistant is per-user internationalized. Supported languages:
**Russian (`ru`, default)** and **English (`en`)**. There is no global
language setting — each user's choice is stored in
`user_settings.language` and applies to that user only, in every surface:

- **Bot** — change via ⚙️ Settings → 🗣 Язык / Language or the `/language`
  command; the UI re-renders immediately in the new language.
- **Mini App** — all UI strings are loaded from the backend locale dictionary
  (`GET /api/v1/i18n/{locale}`); Settings → Language persists the change and
  reloads the dictionary in place (no local copy, no localStorage).
- **API** — `GET /settings` returns `language`; `PATCH /settings`
  `{"language": "en"}` updates it (unsupported codes → 422).

Background jobs (reminders, morning digest, motivation) use the recipient's
language **at execution time**, so a later switch takes effect immediately;
user-authored content (task titles, reminder text, facts, filenames) is never
translated. The AI chat is instructed explicitly to answer in the user's
language, while the structured task-draft schema stays language-neutral and
understands both Russian and English input.

**Adding a language:** register the code in `SupportedLanguage` and add its
display name in `LANGUAGE_NAMES`
(`src/assistant/i18n/service.py`), then drop
`src/assistant/i18n/locales/<code>.json` with the same key set as `ru.json`.
Russian is the fallback for any unknown language or missing key. No handler,
service, API, or Mini App code changes are needed.

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
bash scripts/acceptance.sh   # production-like end-to-end acceptance run
```

Tests require a reachable PostgreSQL 17 with pgvector (see `DATABASE_URL` /
`TEST_DATABASE_URL`). No real Telegram or OpenAI credentials are needed —
external AI/Telegram HTTP calls are mocked, and initData is signed locally.

`scripts/acceptance.sh` is a production-like verification run: a **fresh**
Docker PostgreSQL, `alembic upgrade head`, API start + `/healthz` check,
worker start with several digest-scheduling iterations (users with and
without settings rows — no `MissingGreenlet`), bot dispatcher wiring with
Telegram mocked, RU/EN onboarding and NL task-draft tests, the full pytest
suite against the fresh database, Ruff, `docker compose config` validation,
and the loopback-only port-exposure audit.

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

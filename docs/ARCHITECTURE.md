# Architecture

Modular monolith per SPEC.md. Four runtime processes share one Python image:

```
Telegram users ──> bot (aiogram 3, polling)
                     │  updates, commands, FSM dialogs, file uploads
Mini App (web)  ──> api (FastAPI + Pydantic v2)  ──> services ──> PostgreSQL 17 + pgvector
                     │
worker ─────────────────────> durable jobs table (FOR UPDATE SKIP LOCKED)
                     │
                  shared: services/ (business logic), ai/ (provider abstraction),
                          db/ (engine + models), config.py, logging.py
```

## Processes

- **bot** (`python -m assistant.bot.main`) — aiogram 3 Dispatcher. Owns all Telegram
  UX: commands, inline keyboards, FSM-driven dialogs (NL → AI draft → confirm →
  persist), file uploads (validated size/type), and Mini App links. Enqueues
  durable jobs; never does long work inline.
- **api** (`python -m assistant.api.main`) — FastAPI. Serves Mini App REST
  endpoints (all authed via verified Telegram initData) and static Mini App
  assets. Read/write parity with the bot for calendar/tasks/reminders/workouts.
- **worker** (`python -m assistant.worker.main`) — single loop: claim pending
  jobs (`SELECT ... FOR UPDATE SKIP LOCKED`), execute by `type`, record
  outcome; retry with backoff up to `JOB_MAX_ATTEMPTS`; requeue abandoned
  `running` jobs older than the lock TTL. Job types: file ingestion
  (download → extract → chunk → embed → store), morning digest generation
  (idempotent per user/day), reminder firing.
- **postgres** — single state store: relational tables, the `jobs` queue, and
  `Vector(1536)` columns with HNSW cosine indexes for hybrid retrieval.

## Key invariants

1. **Confirm-before-write for AI output.** Chat/draft flows return a structured
   Pydantic draft to the user; nothing AI-generated is persisted until an
   explicit user confirmation (button tap).
2. **Per-user isolation.** Every query — relational and vector — is scoped by
   `user_id`. Retrieval never crosses user boundaries.
3. **Durable background work.** Anything non-trivial (ingestion, digest,
   reminders) is a row in `jobs`; workers are stateless and crash-safe
   (abandoned-lock recovery + idempotency keys).
4. **Safe file handling.** Server-generated storage IDs; user-supplied names are
  metadata only; size limit `MAX_UPLOAD_SIZE_BYTES`; extraction limited to
  text/markdown/PDF/DOCX.
5. **Idempotent digests.** Digest generation for (user, day) is keyed so
  duplicate jobs/worker restarts produce exactly one digest.

## Code layout

```
src/assistant/
├── config.py          # pydantic-settings (env + .env)
├── logging.py
├── db/                # engine, session factory, Base
├── models/            # SQLAlchemy 2.x ORM models
├── ai/                # OpenAI-compatible provider (chat.parse + embeddings)
├── services/          # calendar, tasks, reminders, workouts, files, facts,
│                      # retrieval, digest, queue — shared by bot/api/worker
├── bot/               # aiogram routers, callbacks, FSM, keyboards
├── api/               # FastAPI app, routers, schemas, miniapp auth
├── worker/            # job loop + handlers
└── miniapp/           # static Mini App (vanilla JS + Tailwind)
alembic/               # async migrations
tests/                 # pytest + real PostgreSQL 17 + pgvector
```

## Migration strategy

Alembic async template. `alembic upgrade head` is run by the entry points is
NOT automatic — run it explicitly (and it is part of the CI/verification
checklist). Models are the single source of truth for metadata
(`Base.metadata`).

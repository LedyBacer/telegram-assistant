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
  `Vector(384)` columns with HNSW cosine indexes for hybrid retrieval.

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
6. **Per-user language (i18n).** Supported languages are registered once in
  `SupportedLanguage` (`src/assistant/i18n/service.py`); every user stores
  their choice in `user_settings.language` (NOT NULL, default and server
  default `ru`). There is no global/env-var language. Russian is the
  fallback for unknown languages and missing keys, and a key missing from
  Russian returns the key itself (never an exception).

## Internationalization (i18n)

- **Registry and translator.** `src/assistant/i18n/service.py` centralizes
  `SupportedLanguage`, `DEFAULT_LANGUAGE = "ru"`, `FALLBACK_LANGUAGE = "ru"`,
  `SUPPORTED_LANGUAGES`, `LANGUAGE_NAMES`, and the translator
  `t(language, key, **kwargs)` (`{param}` interpolation; safe on missing
  kwargs). Handlers, services, the API, and the worker only call `t()` —
  no hard-coded UI strings and no `"ru"`/`"en"` literals outside this
  module.
- **Locale files.** Flat `{"section.key": "value"}` JSON dictionaries at
  `src/assistant/i18n/locales/{ru,en}.json`, loaded once per process
  (`@cache`) and served as independent copies. `ru` and `en` must keep the
  same key set (enforced by a test).
- **User scope.** `upsert_user` creates `UserSettings` with the default
  language and never reads the Telegram client's `language_code`; new users
  get `ru`. Changes go through the bot (Settings → Language / `/language`)
  or the API (`PATCH /settings` with validation → 422 on unknown codes).
- **Execution-time resolution.** Background jobs (reminders, digest,
  motivation) read the recipient's language when they run, so switching
  language immediately affects the next delivery; user-authored content
  (reminder text, titles, facts, filenames) is never translated.
- **AI.** The chat system prompt carries an explicit answer-language
  instruction (`language_name`); the structured task-draft schema and its
  prompt are language-neutral and understand Russian and English alike.
  User messages are never pre-translated.
- **Mini App.** Single source of truth: `GET /api/v1/i18n/languages`
  (`[{code, label}]`) and `GET /api/v1/i18n/{locale}` (the flat dictionary).
  The SPA renders from the fetched dictionary, persists the choice via
  `PATCH /settings`, and reloads the dictionary on change — no divergent
  local copy. initData auth is unchanged.
- **Adding a language.** Register the code in `SupportedLanguage`, add its
  display name to `LANGUAGE_NAMES`, and drop `locales/<code>.json` with the
  full key set. Nothing else changes.

## Code layout

```
src/assistant/
├── config.py          # pydantic-settings (env + .env)
├── logging.py
├── db/                # engine, session factory, Base
├── models/            # SQLAlchemy 2.x ORM models
├── ai/                # OpenAI-compatible providers: independent chat and
│                      # embedding clients (separate servers/keys/models)
├── i18n/              # language registry + t() + locales/{ru,en}.json
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

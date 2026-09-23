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
  outcome; retry with exponential backoff up to `JOB_MAX_ATTEMPTS`; re-queue
  abandoned `running` jobs only when their lease expires. Each claim carries a
  unique owner token and a lease that a heartbeat renews; only the current
  owner with a live lease may complete/fail/renew. Job types: file ingestion
  (download → extract → chunk → embed → store), morning digest generation
  (idempotent per user/day), reminder firing. Between polling iterations the
  worker also runs a **proactive pass** (same cadence as digest scheduling):
  deterministic, state-derived nudges (weekly review on local Monday, workout
  nudge after 48 h without a workout) gated by per-user `ProactiveSettings`
  (enabled, quiet hours, daily cap, min interval), deduped by durable
  `NudgeDelivery` rows, plus expiry of stale proposed/confirmed actions.
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
7. **Bounded typed conversational actions.** Model-proposed mutations are
  `PendingAction` rows with a fixed `kind` (closed registry; built-ins:
  `create_item`, `cancel_item`), a kind-validated JSON payload, and a TTL;
  execution happens only on explicit user Confirm (bot inline button or
  Mini App). A stale target expires the action *and re-raises* inside the
  transaction; every API path persists that expiry (commit before the 409/400
  is raised) so an expired action can never replay.
8. **Facts are never auto-confirmed.** Whether proposed via `/remember`,
  the Mini App, or the chat turn engine, a fact enters `proposed` and only a
  user Confirm promotes it to `confirmed`; replacing a fact
  (`supersede_fact`) marks the old fact `superseded` (with provenance via
  `superseded_by`) and stores the new value as a distinct `proposed` fact.
9. **Proactivity is deterministic and deduped.** Nudges derive from stored
  state only (no AI), run through per-user anti-spam gates (enabled → quiet
  hours → daily cap → min interval), and **commit** the `NudgeDelivery`
  dedupe row *before* sending: nudge delivery is **at-most-once** (contrast
  the at-least-once job handlers below) — a send failure loses that nudge
  rather than re-sending it, and a crash between commit and send can never
  double-nudge.

## Job delivery semantics (durable, at-least-once, §5.6)

The `background_jobs` table is the **only** background-work store. Its delivery
guarantee is **durable at-least-once**, *not* exactly-once — and every handler
is written so that re-execution is safe:

- **Durability.** A job is a committed row before the ack that triggered it is
  returned to the client (file upload, reminder create, digest schedule). A
  process crash therefore never loses a job: it simply stays `pending` and is
  claimed later.
- **No double-claim.** Claiming is `UPDATE ... WHERE id IN (SELECT ... FOR
  UPDATE SKIP LOCKED)`, so concurrent workers can never claim the same job.
  The claimed row carries a unique **owner token** (`{worker_id}:{uuid}`) and a
  **lease** (`lease_until`).
- **Lease + heartbeat.** The owner renews the lease on a heartbeat
  (`LEASE_SECONDS / 2`). A job is treated as abandoned **only** when
  `lease_until < now()` — a slow-but-alive worker keeps its lease and is never
  re-claimed.
- **Bounded retries (no infinite loop).** Each failure/abandonment consumes one
  attempt and re-queues with exponential backoff (`30s, 60s, 120s, …`). When
  `attempts` reaches `max_attempts` the job is marked `failed` (with
  `last_error`) and stops. A poison job cannot retry forever.
- **Deduplication.** Handlers idempotency-key their side effects where a
  re-run would otherwise duplicate: reminder sends are keyed on the reminder
  (a sent reminder is never re-sent), digest delivery is keyed on
  (user, day), and file re-ingestion replaces prior chunks before inserting.
  Idempotent job *creation* (`ON CONFLICT (idempotency_key) DO NOTHING`) is a
  database-level no-op and never aborts the caller's transaction (§5.4).
- **Crash window.** The narrow window where at-least-once matters is between a
  handler doing external I/O and the owning worker recording completion. If the
  worker dies in that window, the lease lapses, `recover_abandoned` re-queues
  the job (consuming an attempt), and the idempotent handler runs again without
  duplicating user-visible effects.
- **No long transactions around external I/O (§5.5).** The ingestion handler
  commits each state transition *before* the network call that follows
  (Telegram download, embedding-model call), so no PostgreSQL transaction —
  and no held connection — spans external I/O. The chunk rows are written in a
  single short final transaction that is skipped if the job was cancelled
  mid-run, so a cancelled ingestion can never commit stale chunks.

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
├── services/          # calendar, reminders, workouts, files, facts, chat,
│                      # turns, actions, proactivity, digests, motivation,
│                      # notifications, jobs (queue), users — shared by
│                      # bot/api/worker
├── actions/           # action-kind registry (kind → payload schema + executor)
├── bot/               # aiogram routers, callbacks, FSM, keyboards
├── api/               # FastAPI app, routers, schemas, miniapp auth
├── worker/            # job loop + handlers + proactive pass
└── miniapp/           # static Mini App (vanilla JS ES modules)
alembic/               # async migrations (initial → vector 384 → language
                       # → job leases → pending actions → proactivity)
tests/                 # pytest + real PostgreSQL 17 + pgvector (351 tests)
e2e/                   # Playwright Mini App browser E2E (6 specs)
```

## Migration strategy

Alembic async template. `alembic upgrade head` is run by the entry points is
NOT automatic — run it explicitly (and it is part of the CI/verification
checklist). Models are the single source of truth for metadata
(`Base.metadata`).

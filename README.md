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

## V2 features

On top of the calendar/reminders/workouts/files/facts/chat core:

- **Conversational actions (confirm-before-write).** When chat implies a
  mutation (calendar item create/update/complete/cancel/delete, standalone
  reminders, workout log/schedule), the model proposes a *typed*
  `PendingAction` (closed kind registry, validated JSON payload, TTL) instead
  of writing. The proposal appears in the bot (inline buttons) and in the
  Mini App ⏳ Actions tab; only an explicit Confirm executes it. A stale
  target (e.g. the item was already gone) expires the proposal with a
  localized "no longer applies" message, and Reject/timeout are terminal.
- **Long-term memory.** Salient facts can also be proposed automatically by
  the chat turn engine; every fact — wherever it comes from — stays
  `proposed` until the user confirms it, and only `confirmed` facts reach the
  chat context. Replacing a fact (`/remember`, Mini App "Replace") proposes
  the new value as a replacement (linked by `replaces_fact_id`); the old
  `confirmed` fact stays confirmed until the user confirms the replacement —
  at which point it is atomically marked `superseded` (with provenance) — and
  a rejected replacement leaves it untouched.
- **Proactivity.** The worker runs a bounded, deterministic pass (no AI):
  a weekly review nudge on the user's local Monday and a workout nudge after
  48 h without a workout, gated by per-user settings (enabled, quiet hours,
  max nudges/day, min interval) and deduped durably — all adjustable in
  Mini App Settings ("Проактивные уведомления").
- **Hybrid file search.** File search runs a lexical (full-text tsvector) arm
  and a semantic (pgvector) arm **independently** — no lexical prerequisite,
  so a paraphrase with zero keyword overlap is still findable — and fuses the
  two ranked lists with RRF. Vector candidates beyond a configurable semantic
  distance are dropped (lexical hits are always kept). An embedding outage (or
  a chat-only deployment) degrades to lexical-only results instead of failing.
- **Workout scheduling.** Mini App "Schedule a workout" creates a calendar
  item + a start-time reminder (`POST /api/v1/workouts/schedule`).
- **File ingestion retry.** A failed file can be re-queued for indexing
  from the Mini App (`POST /api/v1/files/{id}/retry`); the old job is
  cancelled and a new one carries a fresh idempotency key.

## Quick start (Docker)

```bash
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN, CHAT_* vars, PUBLIC_BASE_URL
                       # (EMBEDDING_* is optional — see "AI providers")
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

Chat is the only **required** AI credential. A deployment without an
embedding provider (`EMBEDDING_*` left unset) runs in **chat-only** mode —
the whole application boots and stays fully useful:

- **Document uploads are rejected visibly** at registration (bot and
  Mini App) with a localized "document search is not available on this
  deployment" message — nothing is enqueued or written.
- **Document search degrades to lexical-only**: the vector arm is skipped
  by construction, keyword (full-text) results are returned as before, and
  the embedding provider is never called.
- **Readiness** reports the `ai_embedding` component as `unconfigured` (never
  `not_ready`) — a chat-only deployment is healthy by design.
- Chat, actions, facts, workouts, calendar, reminders, digests, and every
  other feature are unaffected. (An `files.ingest` job enqueued before a
  config change to chat-only fast-fails instead of retrying.)

Example (two llama.cpp servers; omit the `EMBEDDING_*` block for chat-only):

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
- **`CHAT_THINKING_ENABLED`** (Settings fallback `false`; the Qwen3.5
  `.env.example` intentionally sets `true`) — Qwen thinking/reasoning mode.
  The app sends `chat_template_kwargs.enable_thinking` explicitly on every
  chat/structured request. For Qwen3.5, thinking uses the model-card sampling
  family (`top_p=0.95`, `top_k=20`, `min_p=0`): ordinary chat uses
  `temperature=1.0`, while structured JSON uses the more conservative precise
  profile `temperature=0.6` / `presence_penalty=0` to retain schema reliability.
  Embeddings are unaffected. The bot shows a temporary localized
  "Думаю…" / "Thinking…" status and removes it on success/error/timeout.
- **`CHAT_THINKING_BUDGET_TOKENS`** (optional, unset = unrestricted) —
  forwards llama.cpp's per-request `thinking_budget_tokens`. Start unrestricted;
  if latency or overthinking is excessive, try a measured value such as `4096`.
- **`CHAT_REASONING_EFFORT`** is retained for custom templates/models, but it
  is not the recommended quality knob for stock Qwen3.5; use the thinking
  switch and optional token budget instead.

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
npm run test:e2e        # Mini App browser E2E (Playwright; see section above)
```

Tests require a reachable PostgreSQL 17 with pgvector (see `DATABASE_URL` /
`TEST_DATABASE_URL`). No real Telegram or OpenAI credentials are needed —
external AI/Telegram HTTP calls are mocked, and initData is signed locally.

`scripts/acceptance.sh` is a production-like verification run (22 steps):
`docker compose config`, the loopback-only port-exposure audit, a
**production image build from the frozen lock** (plus an import smoke of the
built image), a **fresh** Docker PostgreSQL, `alembic upgrade head`, API
start + `/healthz` + `/readyz` checks, worker digest-scheduling iterations
(users with and without settings rows — no `MissingGreenlet`, digests
persisted), targeted real-PostgreSQL steps (real `files.ingest` through
`JobWorker._run_job`, job lease/heartbeat, PendingAction confirmation +
concurrent-execution protection, bounded conversational flow with the fake
provider, chat with embeddings unavailable), bot dispatcher wiring with
Telegram mocked, RU/EN onboarding and NL task-draft tests, the full pytest
suite against the fresh database, Ruff, `uv lock --check` (lockfile
integrity), the production-auth no-test-bypass test, and the Mini App
Playwright E2E stage.

## Mini App development

The Mini App is a **vanilla JS static app** — no framework, no build pipeline.
Layout:

```text
miniapp/
    index.html        # shell; references /miniapp/... absolute paths
    styles.css        # Telegram theme CSS variables + component styles
    app.js            # boot, routing between tabs, view renderers
    js/
        api.js        # same-origin fetch client (initData header)
        telegram.js   # small Telegram WebApp wrapper (ready/expand/theme/
                      #   BackButton/haptics; no-op outside Telegram)
        ui.js         # DOM helpers (safe text rendering), bottom sheet,
                      #   Flatpickr date/time pickers
        state.js      # app state (language, theme, cache)
```

Flatpickr (4.6.13) loads from its pinned `cdn.jsdelivr.net` URLs in
`index.html`; the Playwright harness intercepts those exact URLs and
serves the files from `node_modules/flatpickr` so E2E is offline-deterministic.

- **Entry point:** `GET /` → **307** → `/miniapp` (implemented in FastAPI; no
  reverse-proxy workaround needed). `GET /miniapp` serves `index.html` with 200.
- **Theme:** colors come exclusively from the `--tg-theme-*` CSS variables that
  `js/telegram.js` copies from `Telegram.WebApp.themeParams` at boot and on
  `themeChanged` (light, dark, and custom client themes all work; `:root`
  fallbacks cover a plain browser). No hardcoded brand colors.
- **Date/time:** Flatpickr (24-hour, ru/en locale follows the app language,
  themed via the same CSS variables) — never the native browser pickers.
- **Selects:** a small reusable bottom sheet / action sheet (selected state,
  cancel, outside-click and Escape close, keyboard focus) replaces native
  `<select>` for short option lists.
- Develop by pointing `PUBLIC_BASE_URL` at the API (e.g. `http://localhost:8000`),
  open the bot inside Telegram, and press the Mini App button — the bot links
  `$PUBLIC_BASE_URL/miniapp`. Inside Telegram, `Telegram.WebApp.initData` is
  sent with every request and verified server-side; outside Telegram the page
  shows a hint to open it from the bot. Keep the layout single-column and test
  at 390 px width.
- **Adding translations:** frontend strings are backend keys — add the key to
  *both* `src/assistant/i18n/locales/ru.json` and `en.json` (parity is tested),
  then reference it through the `S(key)` helper. There is no local frontend
  locale copy.

### Mini App E2E tests (Playwright, dev/test only)

Playwright + Chromium are a **dev/test-only** dependency — they are never
installed into the production Docker image (the image is Python-only).

```bash
npm install                 # installs @playwright/test (devDependency)
npm run test:e2e            # full E2E suite (Playwright starts its own API)
npm run smoke               # read-only public smoke test (post-deploy)
```

The suite runs against an isolated stack on port 8123 (`e2e/playwright.config.ts`):
a dedicated E2E database (default `assistant_e2e`, overridable with
`E2E_DATABASE_URL` — plus optional `E2E_DATABASE_ADMIN_URL` for the
`CREATE DATABASE` connection) that `e2e/global-setup.ts` creates, migrates,
and `TRUNCATE ... CASCADE`s before every run, and an API process started by
Playwright itself. Viewport is 390x844 (ru-RU, UTC).

**Authenticated tests without a production bypass:** the production app
(`assistant.api.main`) authenticates every Mini App request with a signed
Telegram `initData` and has **no** test-auth bypass. The E2E API is started
from the separate test-only entry point `e2e/support/test_app.py`
(`uv run python e2e/support/test_app.py`), which wraps the production app
and installs the `get_current_user` dependency with a deterministic test
user. Because that script lives **outside `src/`** — the Docker image copies
only `src/` — and the production `create_app()` never consults any
environment flag for the override, the test-auth code is structurally absent
from every production artifact and the real API can never be turned into an
open endpoint from a (mis)configured environment. A normal production
request without valid `initData` gets `401`. The Telegram
WebApp client itself is stubbed in the browser via `page.addInitScript`
(`e2e/helpers/telegram-stub.ts`), providing `initData`, `themeParams`,
`ready()/expand()`, BackButton, HapticFeedback, and runtime theme switching;
the real `telegram.org` script is blocked in tests.

The specs (6) verify rendering (including the `[object HTMLDivElement]`
regression), themes via computed styles (light/dark/custom), layout (no
horizontal overflow, ≥44 px touch targets), a11y basics, a per-screen audit
(states, user content, task lifecycle), the full 20-step user scenario
(fact, file upload, task + calendar, bottom sheet, ru→en, reload
persistence, controlled API error), and the five V2 features end-to-end
against the real API + DB (actions confirm/stale-409/reject, workout
scheduling, file retry, fact supersede, proactive-settings defaults +
PATCH) via a small asyncpg seed helper (`e2e/helpers/seed.ts`). Every spec
also asserts browser-console health (uncaught exceptions and unexpected
failed same-origin requests fail the test). Reference screenshots (for
human review only) are written to the gitignored
`test-artifacts/screenshots/` directory.

After a manual deploy, verify the public URL read-only:

```bash
BASE_URL=https://telegram-assistant.bacer.ru bash scripts/public_smoke.sh
```

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

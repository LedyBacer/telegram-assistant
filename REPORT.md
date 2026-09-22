# Final Report — Smart Personal Assistant / Motivator

Date: 2026-09-22
Status: all 17 milestones complete; SPEC §31 Definition of Done verified;
production-ready per-user internationalization (i18n) added and verified;
focused production runtime-hardening pass (MissingGreenlet fix, onboarding
i18n completion, Qwen3.5/llama.cpp NL parsing, loopback-only API bind,
production-like acceptance run) implemented and verified end-to-end;
configurable chat timeout (`CHAT_TIMEOUT_SECONDS`) and explicit Qwen thinking
mode (`CHAT_THINKING_ENABLED`) with a localized temporary "Thinking…" status
UX implemented and verified end-to-end.

## 1. What was built

A production-oriented Telegram personal assistant (modular monolith) with
four runtime processes: `bot` (aiogram 3), `api` (FastAPI), `worker`
(durable PostgreSQL job queue), `postgres` (17 + pgvector). No Redis, Celery,
or other infrastructure beyond the SPEC baseline.

Features implemented (SPEC §1–§30):

- **Calendar** — tasks and events via structured text drafts (manual line
  format first, AI fallback for natural language), confirm-before-write,
  today/upcoming views, user-timezone normalization, complete/cancel/delete.
- **Reminders** — durable `reminder_send` jobs with idempotency keys and
  offset resolution; delivered through the Bot API by the worker; failures
  re-queue with exponential backoff; abandoned-lock recovery.
- **Workouts** — log with duration/effort, timezone-aware stats (total,
  minutes, this week, day streak), scheduled workouts create a calendar item
  plus a start-time reminder.
- **Files + semantic retrieval** — Telegram document uploads (server-side
  storage keys), durable ingestion job (txt/md/pdf/docx extraction,
  word-boundary overlapping chunks, batch embeddings into `Vector(384)`),
  hybrid retrieval (pgvector cosine + keyword boost) with citations,
  per-user chunk ownership, file deletion.
- **User facts** — proposed → confirmed/rejected/superseded lifecycle;
  never auto-confirmed; only CONFIRMED facts reach chat context.
- **Contextual AI chat** — bounded, selective context (recent messages,
  today/upcoming items, pending reminders, recent workouts, confirmed facts,
  top-3 file excerpts marked untrusted), persisted both sides only after a
  successful reply.
- **Morning digest** — per-(user, local date) idempotent scheduling with a
  daily pass in the worker; deterministic state-derived motivation line;
  real Telegram delivery with re-queue on failure.
- **Mini App** — vanilla-JS + Tailwind SPA (today/upcoming/new/workouts/
  files/facts/settings tabs) served statically; auth by Telegram initData
  HMAC verified with the bot token (constant-time compare, `auth_date`
  freshness with future-skew tolerance, strict user validation, bots
  rejected); authed `/api/v1` endpoints, all user-scoped.
- **Per-user internationalization (i18n)** — Russian (default) and
  English. Central registry + `t()` translator
  (`src/assistant/i18n/service.py`) over flat `locales/{ru,en}.json`
  dictionaries; per-user choice in `user_settings.language` (NOT NULL,
  server default `ru`, migration `e8a2c41b7f05`), never derived from the
  Telegram `language_code`; selectable in the bot (Settings → Language,
  `/language`) and Mini App (Settings), persisted in PostgreSQL,
  effective immediately everywhere; background jobs (reminders, digest,
  motivation) resolve the recipient's language at execution time; AI chat
  is instructed to answer in the user's language while the structured
  draft schema stays language-neutral; user-authored content is never
  translated; the Mini App renders exclusively from the backend locale
  dictionaries (`/api/v1/i18n/*`, no local copy).
- **AI layer** — `AIProvider` abstraction with *independent*
  OpenAI-compatible clients for chat/generation (`CHAT_BASE_URL` /
  `CHAT_API_KEY` / `CHAT_MODEL`) and embeddings (`EMBEDDING_BASE_URL` /
  `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` / `EMBEDDING_DIMENSIONS`,
  legacy `OPENAI_*` fallback) so the two capabilities may live on
  separate servers; E5 prefixes applied centrally (`passage: ` /
  `query: `), llama.cpp-compatible `/v1/embeddings` (model + input only)
  with client-side dimension validation, structured JSON drafts, bounded
  retries, `store=False` on chat completions to avoid provider-side
  conversation persistence — SPEC §15); **configurable chat timeout**
  (`CHAT_TIMEOUT_SECONDS`, default 180) drives the chat client `Timeout`
  for chat + structured generation while embeddings keep their own short
  timeout, with `APITimeoutError` mapped to the narrow `AITimeoutError`
  (logged, localized to the user, never a raw provider string); **explicit
  Qwen thinking mode** (`CHAT_THINKING_ENABLED`, default true) is sent on
  every chat/structured completion as
  `extra_body → chat_template_kwargs.enable_thinking` (the llama.cpp
  OpenAI-compatible field), built centrally and never applied to
  embeddings, and the bot shows a temporary localized "Думаю…" /
  "Thinking…" status while a slow AI op runs; lazy provider construction
  keeps tests credential-free.

## 2. Code layout

```
src/assistant/
  api/        FastAPI app, initData auth, /api/v1 routes, pydantic schemas
  bot/        aiogram entrypoint, handlers, callbacks, keyboards, FSM states
  ai/         provider protocol, independent chat/embedding providers,
              prompts, schemas
  i18n/       language registry (SupportedLanguage), t() translator,
              locales/{ru,en}.json
  models/     SQLAlchemy 2 async ORM (11 tables, pgvector HNSW index)
  services/   calendar, reminders, workouts, files, facts, chat, digests,
              motivation, notifications, jobs (queue), users
  worker/     durable job loop, handler registry, digest scheduler
  db/         async engine, session, Base
  config.py   pydantic-settings (env-driven)
miniapp/      index.html + app.js SPA (all UI strings from backend locales)
alembic/      async migration env + initial schema (c390315de59f) +
              embedding 1536→384 (7b492f548c86) + user_settings.language
              (e8a2c41b7f05)
tests/        16 test modules, 257 tests, real PostgreSQL
scripts/      acceptance.sh — production-like end-to-end verification run
docs/         ARCHITECTURE.md, ASSUMPTIONS.md, RESEARCH.md
```

Dependency versions (pinned in `uv.lock`): Python 3.12, aiogram 3.31.0,
FastAPI 0.141.1, SQLAlchemy 2.0.54, asyncpg, Alembic 1.20.0, Pydantic
2.13.5, openai 3.16.2, pgvector 0.8.6 (PostgreSQL 17.11).

## 3. Milestones (Git history)

| # | Commit | Content |
|---|--------|---------|
| 1 | `13186cf` | Bootstrap: config, DB, Docker, docs |
| 2 | `edaf241` | Schema + ORM models, initial migration |
| 3 | `69ff482` | Durable job queue + worker |
| 4 | `e884574` | aiogram 3 bot foundation |
| 5 | `04480e8` | Calendar + reminder services |
| 6 | `d384a33` | Workout tracking |
| 7 | `45008c7` | AI provider + structured drafts |
| 8 | `5f64177` | Files, ingestion, pgvector retrieval |
| 9 | `40c559d` | Facts lifecycle + contextual chat |
| 10 | `1802a1a` | Morning digest, motivation, real delivery |
| 11 | `5f52c77` | Mini App initData auth + /api/v1 |
| 12 | `47a7b99` | Migration-chain test (SPEC §26 gap closed) |
| 13 | — | This report + final verification |
| 14 | `17d2683` | Independent chat/embedding providers + `vector(384)` migration |
| 15 | `c378f71` | Per-user internationalization (ru/en): registry + `t()`, `user_settings.language` migration, bot/Mini App/API language selection, execution-time job localization, AI language instruction, 30 i18n tests |
| 16 | `8c3425b`, `965f760`, `f4f0529`, `35e6b35`, `8954951` | Production runtime-hardening pass: MissingGreenlet fix in digest worker + regression tests; onboarding/FSM i18n completion + localized validation errors; llama.cpp-compatible structured NL parsing for Qwen3.5; loopback-only API bind; `scripts/acceptance.sh` production-like verification run; 235 tests |
| 17 | `af62178` | Configurable chat timeout (`CHAT_TIMEOUT_SECONDS`) + explicit Qwen thinking mode (`CHAT_THINKING_ENABLED` → `chat_template_kwargs.enable_thinking`), narrow `AITimeoutError` with no-retry timeout vs retriable malformed response, and a localized temporary "Думаю…" / "Thinking…" status UX; 257 tests |

## 4. Verification (SPEC §31 + QWEN.md)

All checks executed on 2026-09-22:

| Check | Result |
|-------|--------|
| `uv sync` | OK (58 packages, lock resolved) |
| Docker Compose config | `docker compose config --quiet` — valid |
| Network exposure (M16) | `docker-compose.yml` publishes the API as `127.0.0.1:8000:8000` (loopback-only); PostgreSQL has **no** published port; `scripts/acceptance.sh` fails the run on any non-loopback published port |
| Application image builds | `docker compose build` — api, bot, worker images built |
| PostgreSQL healthy | `ta-pgvector` up (PostgreSQL 17.11 + pgvector 0.8.6) |
| Migrations from empty database | recreated the `assistant` DB and `alembic upgrade head` applied cleanly (initial schema + `7b492f548c86` 1536→384 + `e8a2c41b7f05` user_settings.language); also covered in-suite by `tests/test_migrations.py` (throwaway DB: head stamp, exact table set vs `Base.metadata`, pgvector extension, HNSW index) |
| Full pytest suite | **257 passed** (235 on the fresh migrated DB in `scripts/acceptance.sh`, then +22 `tests/test_thinking_ux.py` in Milestone 17); earlier states verified at 176, then 206, then 235 |
| Ruff | `ruff check .` — all checks passed (`ruff format` is not a project gate; pre-existing files are unformatted) |
| Chat timeout + thinking (M17) | `tests/test_thinking_ux.py` (22): settings defaults + positive/bounded validation; configured timeout reaches the chat client `Timeout.read` (connect/pool 10 s); embedding client stays on its own 60 s float timeout; `chat_template_kwargs.enable_thinking` present and correct on both chat and structured calls; `APITimeoutError` → `AITimeoutError` (subclass of `AIProviderError`) after exactly one provider call (no second long inference); RU/EN "Думаю…" / "Thinking…" status sent in the user's language and deleted on success, provider error, and timeout; a failing status delete does not break the flow; no status when thinking is disabled |
| `.env.example` loads (M17) | `.env.example` values instantiate `Settings` cleanly: `chat_timeout_seconds=180.0`, `chat_thinking_enabled=True` |
| Production-like acceptance run (M16) | `bash scripts/acceptance.sh` — 13 checks, all passed: fresh Docker PostgreSQL, `alembic upgrade head`, API start + `/healthz`, worker running digest-scheduling iterations against users with and without `UserSettings` rows with **no `MissingGreenlet`** and 2 digests persisted, bot dispatcher wiring with Telegram mocked, RU/EN onboarding tests, Qwen-style NL task-draft tests, full 235-test pytest suite on the fresh database, Ruff, compose config, loopback-only port audit |
| FastAPI application imports | OK (routes serve; FastAPI 0.141 materializes included routers lazily) |
| Bot application imports | OK (`assistant.bot.main`) |
| Worker smoke path | `python -m assistant.worker.main` started, polled an empty queue for 15 s, stopped cleanly (exit 0) |
| Concurrency test (PostgreSQL locking) | `tests/test_jobs.py` — concurrent claimers, no double-claim via `FOR UPDATE SKIP LOCKED`, against real PostgreSQL |
| Mini App auth tests | `tests/test_init_data.py` — 17 unit tests (valid/wrong-token/tampered/stale/future/missing/malformed payloads) + 4 authed-API 401 tests in `tests/test_api.py` |
| Real-PostgreSQL flows | items CRUD + reminders, workout stats, file search, facts lifecycle, digest scheduling, bot draft flows — all in the 235-test suite against a real PG 17 |
| i18n: column + default | `user_settings.language` VARCHAR(16) NOT NULL, column default `'ru'`; new users default `ru`; existing rows migrated to `ru` (asserted in `tests/test_i18n.py`) |
| i18n: ru/en parity | identical key sets in `locales/ru.json` / `locales/en.json` (enforced by `test_locale_key_parity_ru_en`); two users in two languages verified end-to-end (bot start, reminders, digest, API) |
| pgvector column + index | `file_chunks.embedding` is `vector(384)` (atttypmod 384) and `ix_file_chunks_embedding_hnsw` (hnsw, cosine) present in the catalog after `alembic upgrade head` |
| No TODO/stub/placeholder | grep of `src/` and `miniapp/` — none (only HTML `placeholder` input attributes) |
| README/docs | README.md + docs/ARCHITECTURE.md + docs/ASSUMPTIONS.md + docs/RESEARCH.md |
| Git working tree clean | `git status` clean after each milestone commit; nothing pushed to any remote |

Test-suite breakdown (collected): `test_i18n` (30), `test_ai` (30),
`test_bot_foundation` (25), `test_thinking_ux` (22), `test_api` (20),
`test_files` (17), `test_init_data` (17), `test_onboarding_i18n` (16),
`test_facts` (14), `test_reminders` (14), `test_digests` (13),
`test_jobs` (11), `test_chat` (10), `test_calendar` (9),
`test_workouts` (7), `test_migrations` (2) = 257.

External AI/Telegram HTTP calls are mocked in tests (fake providers,
sender stubs, locally signed initData); production integration code is
implemented and import-verified. Context7 MCP was not used in this run;
`docs/RESEARCH.md` records the library-specific findings that were
verified.

## 5. Assumptions and research

- `docs/ASSUMPTIONS.md` — all ambiguity resolutions (Mini App auth model,
  draft format, reminder semantics, digest timing, etc.).
- `docs/RESEARCH.md` — aiogram 3.31, FastAPI 0.141, openai 3.16, asyncpg,
  pgvector specifics confirmed during implementation.

## 6. How to run

```bash
uv sync
cp .env.example .env            # fill TELEGRAM_BOT_TOKEN, CHAT_*/EMBEDDING_* (or legacy OPENAI_*)
docker compose up postgres      # or point DATABASE_URL at any PG 16+ + pgvector
uv run alembic upgrade head
uv run python -m assistant.bot.main      # bot
uv run python -m assistant.api.main      # api (port 8000, /miniapp served)
uv run python -m assistant.worker.main   # worker
uv run pytest                     # full suite (needs PostgreSQL)
uv run ruff check .
bash scripts/acceptance.sh        # production-like end-to-end acceptance run
```

Or `docker compose up` for all four services.

## 7. Milestone 16 — Production runtime-hardening pass (2026-09-22)

A focused hardening pass on reported production issues and exposure; no
redesign of unrelated functionality.

1. **MissingGreenlet in the digest worker.** The production traceback
   (`worker.main.schedule_digests()` → `digests_service.ensure_digest_jobs()`
   → `_user_tz` → `user.settings.timezone`) was a lazy relationship load in an
   async context. Fix: `User.settings` is eager-loaded via
   `selectinload(User.settings)` in `ensure_digest_jobs()` and via
   `session.get(..., options=[selectinload(...)])` in the digest send path
   (`src/assistant/services/digests.py`). No `MissingGreenlet` catch, no
   implicit lazy DB I/O. Regression tests in `tests/test_digests.py`:
   `ensure_digest_jobs()` works against real PostgreSQL for users with and
   without a `UserSettings` row (and is idempotent), plus a test proving the
   old non-eager query shape raises `MissingGreenlet`. The acceptance run
   exercises the real worker polling loop over several digest-scheduling
   iterations, error-free.
2. **Onboarding i18n completed.** All onboarding/FSM prompts (start,
   timezone, digest-time, cancellation, task-creation confirmations,
   validation errors) use the persisted per-user language via `t()`; no
   hardcoded English for a Russian user and no hardcoded Russian for an
   English user; user-authored content is never translated. Covered by
   `tests/test_onboarding_i18n.py` (16 tests, RU and EN). Validation errors in
   the workouts/facts/files services raise `LocalizableError` with locale
   keys + parameters, surfaced localized by the bot and as `{"error": key}` by
   the API.
3. **Qwen3.5 / llama.cpp NL task parsing.** Structured parsing no longer
   depends on OpenAI-only `response_format=json_schema`; the JSON contract
   lives in the system prompt and responses are parsed with a robust
   `extract_json_object` (code fences, thinking preambles, trailing prose,
   braces in strings, last-balanced-object preference). Pydantic validation
   (`AITaskDraft`, `extra="forbid"`) and the explicit user confirmation before
   any DB write are preserved. Parsing failures log redacted, structured
   context (no secrets, no raw provider errors). Regression tests use
   realistic Qwen-style responses for "Мне нужно сегодня позвонить Сергею в
   17:00", "Сегодня напомни мне позвонить Сергею в 17:00" and the English
   equivalent, with relative-date resolution in the user's timezone, plus
   FSM state-isolation and `/cancel` tests (`tests/test_ai.py`,
   `tests/test_bot_foundation.py`).
4. **API exposure.** `docker-compose.yml` publishes the API as
   `127.0.0.1:8000:8000` (loopback-only); PostgreSQL is not published at all.
   A future public Mini App is served through an HTTPS reverse proxy
   forwarding to `127.0.0.1:8000` — documented in README ("Network
   exposure") and enforced by the acceptance script's port audit.
5. **Production-like acceptance run.** `scripts/acceptance.sh` (13 checks,
   all passed on 2026-09-22): compose config valid; loopback-only port
   audit; fresh `pgvector/pgvector:pg17` container; `alembic upgrade head`;
   API starts and answers `/healthz`; worker completes several
   digest-scheduling iterations with no `MissingGreenlet` and persists digests
   for a user with and a user without settings; bot imports and wires its
   dispatcher with Telegram mocked; RU and EN onboarding tests;
   Qwen-style NL task-draft tests; the full 235-test pytest suite against the
   fresh database; `ruff check .` clean.

Verification state after Milestone 16: **235 tests passing, Ruff clean,
compose valid, working tree clean, nothing pushed to any remote.**

## 8. Milestone 17 — Configurable chat timeout + Qwen thinking mode (2026-09-22)

A focused, self-contained addition to the AI layer and the bot UX; no
redesign of unrelated functionality.

1. **`CHAT_TIMEOUT_SECONDS` (default 180).** The OpenAI chat client is
   constructed with an explicit `Timeout(connect=10, read=CHAT_TIMEOUT_SECONDS,
   write=30, pool=10)` and `max_retries=0`, so the configured value — not the
   SDK's implicit 60 s — bounds a non-streaming completion (one read phase).
   The setting is validated (`ge=1`, `le=3600`) so it cannot be zero/negative
   or unreasonably large. It is applied to **both** `chat()` and
   `chat_structured()` (normal assistant chat and structured task-draft
   generation) and **deliberately not** to the embedding provider, which keeps
   its own short 60 s float timeout. On `APITimeoutError` the provider raises
   the narrow `AITimeoutError` (a subclass of `AIProviderError`, so existing
   bot `except AIProviderError` fallbacks keep catching it), logs a warning
   with the configured value, and the user sees a localized message — never a
   raw provider string.
2. **`CHAT_THINKING_ENABLED` (default `true`).** A global provider setting,
   sent **explicitly** on every chat/structured completion through the
   OpenAI client's `extra_body` as
   `{"chat_template_kwargs": {"enable_thinking": <bool>}}` — the documented
   llama.cpp OpenAI-compatible field. The option is built in one place
   (`OpenAIChatProvider._chat_options()`) so it can't drift between the chat
   and structured call paths, and it is never attached to embedding requests.
   `true` lets the model reason before answering (slower, better reasoning);
   `false` disables thinking for lower latency (possibly reduced reasoning
   quality). It is a provider behavior, not a per-user preference.
3. **Timeout vs malformed response.** A *transport/provider timeout* is not a
   bad model answer: `chat_structured()` catches `APITimeoutError` **before**
   the malformed-response path and fails cleanly after exactly one provider
   call (no immediate second equally-long inference) — worst-case wait is the
   configured `CHAT_TIMEOUT_SECONDS`, not a multiple of it. A *malformed
   structured response* (no JSON / schema mismatch) is retried once with
   corrective feedback, as before. Pydantic validation (`AITaskDraft`,
   `extra="forbid"`) and confirm-before-write are unchanged.
4. **Telegram UX — temporary "Thinking…" status.** When thinking is enabled
   and a slow AI op is about to run (draft or chat), the bot immediately sends
   a short localized temporary message — "Думаю…" (`ru`) / "Thinking…" (`en`),
   key `ai.thinking` — in the user's persisted language, then deletes it when
   the provider responds, including on provider error and timeout. Send/delete
   are best-effort: any Telegram failure is logged and swallowed so it never
   breaks the flow, and a failed delete cannot leave a stale message mid-flow
   (it is simply not removed). When thinking is disabled no status is sent.
   Existing typing actions are preserved. The status is only sent for real AI
   requests, not DB-only commands.
5. **Documentation.** `.env.example` gains `CHAT_TIMEOUT_SECONDS=180` and
   `CHAT_THINKING_ENABLED=true` with explanatory comments (no real addresses
   or secrets); README gains "Chat timeout and Qwen thinking mode" and
   "Structured completion retries" sections and lists both variables.

Verification state after Milestone 17: **257 tests passing, Ruff clean,
imports OK, `.env.example` loads into `Settings`, `docker compose config`
valid, ru/en locale parity, working tree clean, nothing pushed to any
remote.**

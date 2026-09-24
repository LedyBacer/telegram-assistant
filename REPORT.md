# Final Report — Smart Personal Assistant / Motivator

Date: 2026-09-23
Status: all 20 milestones complete, including the Personal Assistant V2
feature set (reliability, bounded conversational actions, long-term memory,
proactivity, lexical-gated hybrid RAG, and the V2 Mini App) with expanded
pytest/E2E acceptance coverage; SPEC §31 Definition of Done verified;
Mini App production-hardening pass (vanilla ES-module frontend,
`[object HTMLDivElement]` root-cause fix, Telegram-native theming, Flatpickr,
bottom sheet, full UI states, Playwright E2E harness, read-only public smoke
script) implemented and verified end-to-end;
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
- **Conversational actions (V2)** — model-proposed mutations are *typed*
  `PendingAction` rows (closed kind registry, per-kind Pydantic payload,
  TTL) with nothing executed without an explicit Confirm in the bot or the
  Mini App Actions inbox; stale targets expire the action in-transaction
  and the API persists that expiry before answering 409/400; Reject and
  timeout are terminal.
- **Long-term memory** — the chat turn engine can propose salient
  facts automatically (deduped against existing values); every fact
  stays `proposed` until a user confirm. Replacing a fact proposes the new
  value linked by `replaces_fact_id`; the old `confirmed` fact stays
  confirmed until the replacement is confirmed (then atomically
  `superseded`, with `superseded_by` provenance); a rejected replacement
  leaves the old fact untouched.
- **Proactivity (V2)** — bounded, deterministic worker pass (no AI calls):
  weekly review nudge on the user's local Monday, workout nudge after 48 h
  without a workout; per-user `ProactiveSettings` (enabled, quiet hours,
  max nudges/day, min interval) with durable `NudgeDelivery` dedupe written
  before sending.
- **Hybrid retrieval (V2 upgrade)** — lexical-gated: tsvector full-text arm
  must match before the embedding provider is called; lexical + vector
  ranked lists fused with RRF (K=60, minimum fused score); embedding
  outages degrade to lexical-only; deterministic citations from the fused
  ranking.
- **Job reliability (V2)** — durable queue hardened with real leases:
  owner tokens, lease TTL + heartbeat, abandoned-recovery only on lease
  expiry, `a1b2c3d4e5f6` migration.
- **Workout scheduling + file retry (V2)** — `POST /api/v1/workouts/schedule`
  (calendar item + start-time reminder) and `POST /api/v1/files/{id}/retry`
  (failed-only, fresh idempotency key, old job cancelled).
- **Mini App** — vanilla-JS ES-module Mini App (today/upcoming/new/**actions**/
  workouts/files/facts/settings views; no framework, no build step;
  Telegram-native `--tg-theme-*` theming, Flatpickr date/time, bottom-sheet
  selects; V2 screens: Actions inbox with confirm/reject, workout
  scheduling, file-retry button, inline fact replacement, proactive
  settings card; verified by a Playwright E2E harness — see Milestones 18
  and 20) served
  statically; auth by Telegram initData
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
  Qwen thinking mode** (`CHAT_THINKING_ENABLED`, default false — the
  fast/no-think profile; opt-in per deployment) is sent on
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
  models/     SQLAlchemy 2 async ORM (14 tables, pgvector HNSW index)
  services/   calendar, reminders, workouts, files, facts, chat, turns,
              actions, proactivity, digests, motivation, notifications,
              jobs (queue), users
  actions/    action-kind registry (kind → payload schema + executor)
  worker/     durable job loop (lease + heartbeat), handler registry,
              digest scheduler, proactive pass
  db/         async engine, session, Base
  config.py   pydantic-settings (env-driven)
miniapp/      index.html + app.js + js/{api,telegram,ui,state}.js SPA
              (all UI strings from backend locales)
alembic/      async migration env + chain: initial schema (c390315de59f) →
              embedding 1536→384 (7b492f548c86) → user_settings.language
              (e8a2c41b7f05) → job leases (a1b2c3d4e5f6) →
              pending_actions (f7a8b9c0d1e2) → proactivity (9c8d7e6f5a4b)
tests/        21 test modules, 351 tests, real PostgreSQL
e2e/          Playwright Mini App browser E2E (6 specs, isolated assistant_e2e DB)
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
| 18 | `38bc65e`, `07f5ff8`, `1f88366`, `71923e5`, `ffd3e56`, `da857bf`, `a33fb9d` | Mini App production-hardening pass (vanilla ES modules, theming, Flatpickr, bottom sheet, Playwright harness) + per-screen audit spec |
| 19 | `9a2e7e5`, `fea8049`, `62e4dc2`, `a3dabd7`, `caee60f`, `631ca52`, `124a54c` | V2 core: job leases; calendar/reminder invariants; durable `PendingAction` engine (typed kinds, confirm flow, TTL expiry); automatic memory proposals with dedupe/supersede; lexical-gated hybrid RAG (RRF); bounded deterministic proactivity with anti-spam gates + nudge dedupe |
| 20 | `6324766`, `eee632f`, `c0e94e7`, `82ea1d5` | V2 surface + acceptance: Mini App API extension (actions inbox, proactive settings, fact supersede, file retry, workout schedule); Mini App V2 screens; commit-before-409/400 expiry fix; V2 E2E spec + seed helper; 351 pytest tests, 6/6 Playwright specs |

## 4. Verification (SPEC §31 + QWEN.md)

All checks executed on 2026-09-23 (V2 final state; V1 checks re-verified
inside the same run):

| Check | Result |
|-------|--------|
| `uv sync` | OK (58 packages, lock resolved) |
| Docker Compose config | `docker compose config --quiet` — valid |
| Network exposure (M16) | `docker-compose.yml` publishes the API as `127.0.0.1:8000:8000` (loopback-only); PostgreSQL has **no** published port; `scripts/acceptance.sh` fails the run on any non-loopback published port |
| Application image builds | `docker compose build` — api, bot, worker images built |
| PostgreSQL healthy | `ta-pgvector` up (PostgreSQL 17.11 + pgvector 0.8.6) |
| Migrations from empty database | fresh `assistant` + `assistant_e2e` databases, `alembic upgrade head` applied the full chain cleanly (initial schema → `7b492f548c86` 1536→384 → `e8a2c41b7f05` user_settings.language → `a1b2c3d4e5f6` job leases → `f7a8b9c0d1e2` pending_actions → `9c8d7e6f5a4b` proactivity); also covered in-suite by `tests/test_migrations.py` (throwaway DB: head stamp, exact table set vs `Base.metadata`, pgvector extension, HNSW index) |
| Full pytest suite | **351 passed** (351 also on the fresh migrated DB inside `scripts/acceptance.sh`); earlier states verified at 176 → 206 → 235 → 257, then +94 V2 tests (actions, turns/memory, proactivity, hybrid retrieval, lease hardening, API V2, Mini App shell) |
| Mini App browser E2E (M18–M20) | `npm run test:e2e` — **6/6 specs passed** (a11y, miniapp 20-step scenario, per-screen audit, screenshots, theme, v2-features) against an isolated `assistant_e2e` DB + API on port 8123; the V2 spec exercised the five new features end-to-end and caught a real rollback bug in the confirm route (fixed in `c0e94e7`) |
| Ruff | `ruff check .` — all checks passed (`ruff format` is not a project gate; pre-existing files are unformatted) |
| Chat timeout + thinking (M17) | `tests/test_thinking_ux.py` (22): settings defaults + positive/bounded validation; configured timeout reaches the chat client `Timeout.read` (connect/pool 10 s); embedding client stays on its own 60 s float timeout; `chat_template_kwargs.enable_thinking` present and correct on both chat and structured calls; `APITimeoutError` → `AITimeoutError` (subclass of `AIProviderError`) after exactly one provider call (no second long inference); RU/EN "Думаю…" / "Thinking…" status sent in the user's language and deleted on success, provider error, and timeout; a failing status delete does not break the flow; no status when thinking is disabled |
| `.env.example` loads (M17) | `.env.example` values instantiate `Settings` cleanly: `chat_timeout_seconds=180.0`, `chat_thinking_enabled=True` |
| Production-like acceptance run (M16) | `bash scripts/acceptance.sh` — 13 checks, all passed on 2026-09-23: fresh Docker PostgreSQL, `alembic upgrade head` (full V2 chain), API start + `/healthz`, worker running digest-scheduling iterations against users with and without `UserSettings` rows with **no `MissingGreenlet`** and 2 digests persisted, bot dispatcher wiring with Telegram mocked, RU/EN onboarding tests, Qwen-style NL task-draft tests, full **351**-test pytest suite on the fresh database, Ruff, compose config, loopback-only port audit |
| FastAPI application imports | OK (routes serve; FastAPI 0.141 materializes included routers lazily) |
| Bot application imports | OK (`assistant.bot.main`) |
| Worker smoke path | `python -m assistant.worker.main` started, polled an empty queue for 15 s, stopped cleanly (exit 0) |
| Concurrency test (PostgreSQL locking) | `tests/test_jobs.py` — concurrent claimers, no double-claim via `FOR UPDATE SKIP LOCKED`, against real PostgreSQL |
| Mini App auth tests | `tests/test_init_data.py` — 17 unit tests (valid/wrong-token/tampered/stale/future/missing/malformed payloads) + 4 authed-API 401 tests in `tests/test_api.py` |
| Real-PostgreSQL flows | items CRUD + reminders, workout stats + scheduling, file search (hybrid — lexical and vector arms independent, RRF-fused), facts lifecycle incl. supersede, digest scheduling, bot draft flows, pending actions, nudge dedupe — all in the 351-test suite against a real PG 17 |
| i18n: column + default | `user_settings.language` VARCHAR(16) NOT NULL, column default `'ru'`; new users default `ru`; existing rows migrated to `ru` (asserted in `tests/test_i18n.py`) |
| i18n: ru/en parity | identical key sets in `locales/ru.json` / `locales/en.json` — **264 keys each** (enforced by `test_locale_key_parity_ru_en`); two users in two languages verified end-to-end (bot start, reminders, digest, API) |
| pgvector column + index | `file_chunks.embedding` is `vector(384)` (atttypmod 384) and `ix_file_chunks_embedding_hnsw` (hnsw, cosine) present in the catalog after `alembic upgrade head` |
| No TODO/stub/placeholder | grep of `src/` and `miniapp/` — none (only HTML `placeholder` input attributes) |
| README/docs | README.md + docs/ARCHITECTURE.md + docs/ASSUMPTIONS.md + docs/RESEARCH.md |
| Git working tree clean | `git status` clean after each milestone commit; nothing pushed to any remote |

Test-suite breakdown (collected, 351): `test_api` (33), `test_i18n` (30),
`test_ai` (30), `test_turns` (27), `test_bot_foundation` (25),
`test_thinking_ux` (23), `test_files` (22), `test_jobs` (18),
`test_init_data` (17), `test_onboarding_i18n` (16), `test_actions` (15),
`test_reminders` (14), `test_facts` (14), `test_calendar` (14),
`test_digests` (13), `test_chat` (12), `test_proactivity` (11),
`test_workouts` (7), `test_minapp_shell` (6), `test_storage_shared` (2),
`test_migrations` (2).

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

## 9. Milestone 18 — Mini App production-hardening pass (2026-09-23)

Turned the prototype Mini App into a small, coherent, Telegram-native app and
proves it with realistic browser E2E tests. Frontend and test infrastructure
only; backend touched in three focused places.

1. **Frontend rewritten as small vanilla ES modules.**
   `miniapp/{index.html, styles.css, app.js, js/{api,telegram,ui,state}.js}`
   plus vendored Flatpickr — no framework, no build pipeline, no Tailwind.
   `js/telegram.js` is a small WebApp abstraction (ready/expand/theme/
   BackButton/haptics) that no-ops outside Telegram; `js/ui.js` provides safe
   DOM construction and shared components; `js/api.js` is the same-origin
   fetch client; `js/state.js` holds app state (language, theme, cache).
2. **`[object HTMLDivElement]` root-cause fix.** The bug was inserting a DOM
   node into a text-producing context; rendering now uses a safe `el()`
   constructor with user-authored content (titles, filenames, fact text)
   inserted only as text nodes — no `innerHTML` for untrusted data. A
   dedicated E2E step asserts the exact rendered text and the absence of the
   string `object` in the DOM.
3. **Entry point in FastAPI.** `GET /` returns a **307** redirect to
   `/miniapp`; `GET /miniapp` serves `index.html` (200); all asset URLs in
   the shell are absolute `/miniapp/...` paths (the page is served without a
   trailing slash).
4. **Telegram-native theming.** All colors derive from ~13 `--tg-theme-*`
   CSS variables copied from `Telegram.WebApp.themeParams` at boot and on
   `themeChanged` (light, dark, and custom client themes; `:root` fallbacks
   for a plain browser). No hardcoded brand colors. Verified programmatically
   via computed styles under three injected themes — no visual inspection
   claims (the model has no vision; screenshots are review-only artifacts).
5. **Flatpickr replaces native pickers** (24-hour, ru/en locale follows the
   app language, themed through the same variables) and a **bottom
   sheet/action sheet replaces native `<select>`** (selected state with
   `aria-selected`, cancel, outside-click and Escape close, keyboard focus).
6. **Complete UI state.** Every data screen renders loading / empty /
  populated / error states; a controlled injected 500 (E2E) proves the error
   state without touching server code.
7. **Focused backend fixes.** (a) Calendar `list_items` anchors on
   `coalesce(starts_at, due_at)` so due-date-only items appear in
   today/upcoming/range views; (b) a focused `POST /api/v1/files` multipart
   endpoint + `register_local_upload()` so the Files screen (and E2E) can
   upload — `_run_pipeline` reads local bytes when `telegram_file_id` is
   `None`; (c) test-only auth: `ASSISTANT_TEST_AUTH=1` makes `create_app()`
   override the `get_current_user` dependency with a deterministic test user.
   The override is purely in-process and env-gated; a production request
   without the env var gets **401** without valid `initData` (verified
   against a live server on port 8199: `401` for `/api/v1/me` and
   `/api/v1/items`, `307`/`200` for the public pages).
8. **Playwright E2E harness (dev/test only).** Playwright 1.63 + Chromium as
   a `devDependency`; the Dockerfile stays Python-only, so the production
   image contains no Playwright/Chromium. Isolated stack on port 8123:
   `e2e/global-setup.ts` (wired as Playwright `globalSetup`) creates,
   migrates, and resets the `assistant_e2e` database with a **single**
   `TRUNCATE ... RESTART IDENTITY CASCADE` statement (per-table TRUNCATE is
   refused by the FK graph) before every run. Viewport 390x844, ru-RU, UTC.
   The Telegram WebApp client is stubbed via `page.addInitScript`
   (deterministic `initData`, `themeParams`, `ready()/expand()`,
   BackButton, HapticFeedback, runtime theme switching); the real
   `telegram.org` script is blocked. Four specs: a11y basics, the 20-step
   user scenario (redirect → whoami → facts → file upload → task with
   Flatpickr due date → calendar → priority bottom sheet → ru→en switch →
   reload persistence → controlled 500 error state), theme computed-style
   checks, and screenshot capture. A console guard fails the run on uncaught
   page exceptions, unexpected console errors, and failed same-origin
   requests (URL-less generic "Failed to load resource" console duplicates
   are skipped because the response handler is authoritative at URL level).
   Programmatic assertions cover no horizontal overflow at 390 px, ≥44 px
   touch targets, and `ready()/expand()` counters. Six reference screenshots
   are written to the gitignored `test-artifacts/screenshots/` directory.
9. **`scripts/public_smoke.sh`** — read-only post-deploy check (GET-only:
   root 307/302, `/miniapp` + 8 assets 200, `/healthz` 200, shell references
   the entry modules); exits non-zero on any failure.
10. **Documentation.** README gains the ES-module architecture section,
    theming/Flatpickr/bottom-sheet notes, the "Adding translations"
    workflow, and the E2E + smoke-test sections; `PROGRESS.md` updated;
    `.gitignore` covers `test-artifacts/` and `node_modules/`.

11. **Per-screen audit spec + public-URL check.** `e2e/tests/screens-audit.e2e.ts`
    adds a programmatic per-screen audit (calendar states + day selection +
    `has-events` marker, upcoming/new/workouts/files/facts states, file
    upload with status badge and no storage-path leak, fact exact-text
    rendering, full task lifecycle create → complete → delete with
    `alertdialog` confirmation, settings rows for language/timezone/digest
    time + motivation switch with immediate ru→en→ru UI updates, and
    touch-target/overflow/forbidden-literal checks on all seven sections).
    `scripts/public_smoke.sh` was executed read-only against the live public
    URL: the production server currently runs an **older deployed version**
    (`/` → 302 via the reverse-proxy workaround, `/miniapp` → 307, new ES
    module assets 404), so the script correctly reports FAIL until the
    manual deployment ships this code — exactly its designed post-deploy
    role (it passes against the current code locally).

12. **Fixes found by the per-screen audit.** Running the audit spec
    surfaced three real issues, all fixed and covered by tests:
    (a) `calendar_service.list_range` filtered to `scheduled` items only,
    so a completed task vanished from the Mini App month view — it now
    returns items of every status (completed/cancelled items stay on
    their day with a status badge and a delete action); new regression
    test `test_list_range_includes_completed`;
    (b) the settings motivation switch input was 30px tall (below the
    44px touch-target floor) — it now has a 44px hit target with the
    30px track drawn centered inside it (`background-size`, knob
    re-centered);
    (c) the console guard false-positived on Chromium's spurious
    `net::ERR_ABORTED` `requestfailed` event for requests that already
    completed with `204 No Content` (verified: the same request fires
    both a 204 `response` and the `requestfailed`; the server log shows
    `DELETE → 204` and the row was deleted) — the guard now suppresses
    `requestfailed` for any request that produced a response.

Verification state after Milestone 18: **264 tests passing (fresh
`assistant_test` DB), Ruff clean, `docker compose config` valid, full
Playwright E2E suite 5/5 at 390x844 (a11y, 20-step scenario, per-screen
audit, screenshots, theme), `scripts/public_smoke.sh` PASS locally and
executed read-only against the public URL (FAILs only because the public
server still runs a pre-deployment version), production-auth negative
check 401 without `ASSISTANT_TEST_AUTH`, 6 reference screenshots in
gitignored `test-artifacts/screenshots/` (review-only — no
visual-inspection claims), working tree clean, nothing pushed to any
remote.**

## 10. Milestone 19 — V2 core: reliability, actions, memory, RAG, proactivity (2026-09-23)

The Personal Assistant V2 feature set, implemented as focused, bounded
changes to the existing modular monolith (no new infrastructure, no new
runtime processes).

1. **Durable job hardening (`9a2e7e5`, `fea8049`).** The SKIP LOCKED queue
   gained real leases: each claim carries a unique owner token and a
   `lease_until`; a heartbeat renews it, and a job is treated as abandoned
   *only* when its lease lapses — a slow-but-alive worker is never
   re-claimed. Migration `a1b2c3d4e5f6` (lease columns). Calendar/reminder
   invariants tightened (one item per identity, reminder ownership and
   offset rules).
2. **Bounded conversational actions (`62e4dc2`, `a3dabd7`).** New
   `pending_actions` table (migration `f7a8b9c0d1e2`): `kind` (closed
   registry, built-ins `create_item` / `cancel_item` in
   `src/assistant/actions/`), per-kind Pydantic payload, summary, TTL.
   `services/actions.py`: `propose_action` / `confirm_action` /
   `reject_action` / `execute_action` / `expire_actions`. Confirm executes
   the kind's executor in the same transaction; a stale target (item
   missing/already cancelled) expires the action *and* re-raises
   `ActionStaleError`; a malformed payload expires it with `ValueError`.
   The bot surfaces proposals as inline confirm/reject buttons; the turn
   engine (`services/turns.py`) is the only proposer, so the model can
   never write directly.
3. **Automatic long-term memory (`caee60f`).** The chat turn engine may
   extract salient facts and store them as `proposed` user facts, deduped
   against existing values; they enter the existing
   proposed/confirmed/rejected/superseded lifecycle and only reach chat
   context after an explicit user confirm. `supersede_fact` replaces a
   value by marking the old fact `superseded` (with `superseded_by`) and
   storing the new one as a distinct proposed fact.
4. **Lexical-gated hybrid RAG (`631ca52`).** `services/files.py` search now
   runs a tsvector full-text arm (language-neutral `simple` config,
   `ts_rank`) as a *gate*: the embedding provider is called only when the
   lexical arm matches. Lexical and vector ranked lists are fused with RRF
   (K=60); a minimum fused score drops tail candidates; an embedding outage
   degrades to lexical-only results. Citations are derived
   deterministically from the fused ranking; user-scope isolation is
   unchanged.
5. **Bounded proactivity (`124a54c`).** New `proactive_settings` +
   `nudge_deliveries` tables (migration `9c8d7e6f5a4b`).
   `services/proactivity.py` runs a deterministic pass (no AI): weekly
   review on the user's local Monday (once per ISO week) and a workout
   nudge when the last workout is older than 48 h (once per local day).
   Anti-spam gates in order: enabled → quiet hours (user timezone) → daily
   cap → min interval. The `NudgeDelivery` dedupe row is flushed *before*
   sending, so a crash between write and send can never double-nudge. The
   worker runs the pass between polling iterations (digest cadence) and
   expires stale proposed actions; per-user commit/rollback isolation means
   one failed nudge never poisons the rest of the pass. All data access
   uses explicit SELECTs (no ORM instance state — `Session.rollback`
   expires objects and async lazy reload raises `MissingGreenlet`).

## 11. Milestone 20 — V2 surface, acceptance coverage, and final docs (2026-09-23)

1. **Mini App API extension (`6324766`).** New `/api/v1` endpoints:
   `GET /actions?status=&limit=` (proposed-first, newest),
   `POST /actions/{id}/confirm` (404 unknown, 400 terminal, 409 stale,
   idempotent replay of an executed action), `POST /actions/{id}/reject`;
   `POST /facts/{id}/supersede` (proposes the new value, 404 cross-user);
   `GET/PATCH /proactive-settings` (auto-created row, partial PATCH,
   pydantic bounds → 422); `POST /files/{id}/retry` (only `failed`;
   fresh `file:{id}:retry:{n}` idempotency key, old job cancelled);
   `POST /workouts/schedule` (calendar item `Workout: <name>` + start-time
   reminder).
2. **Mini App V2 screens (`eee632f`).** New ⏳ Actions tab (inbox with
   confirm/reject, status badges, localized stale toast), workout
   scheduling card (Flatpickr datetime + validation), file-retry button on
   failed file cards, inline fact "Replace" form (one open at a time,
   survives re-renders), and the proactive-settings card in Settings
   (switches + quiet-hours pickers + option sheets, isolated fetch
   failure). 21 new i18n keys per language.
3. **Rollback bug caught by the new E2E (`c0e94e7`).** The V2 E2E spec
   failed on the first run with `Expected: "истекло", Received:
   "ожидает"`: the confirm route raised 409/400 *before* committing, so
   the session dependency rolled back the in-transaction `expired`
   marking and the stale action stayed `proposed` forever (would replay on
   retry). The pytest suite masked it because `tests/test_api.py`
   overrides `get_session` with the raw shared fixture session, which never
   rolls back — the real server did. Fix: commit inside both exception
   branches before re-raising.
4. **V2 E2E acceptance (`82ea1d5`).** `e2e/tests/v2-features.e2e.ts`
   exercises the five V2 features end-to-end against the real API + DB
   (seeded via `e2e/helpers/seed.ts` asyncpg helper against the isolated
   `assistant_e2e` DB; `global-setup.ts` now also truncates the three V2
   tables): actions confirm/stale-409/reject, workout scheduling
   (missing-time validation, Flatpickr 23:50 pick), file retry of a
   DB-seeded `failed` file, fact supersede (new "предложен" / old
   "заменён" — new `miniapp.fact_superseded` locale key, ru/en parity now
   264/264), proactive-settings defaults (weekly on, 22:00→08:00, max 3,
   120 min) + PATCH + restore. The controlled 409 is allowed through the
   console guard (`guard.allow`).
5. **Docs.** README gains the "V2 features" section and updated E2E
   description; `docs/ARCHITECTURE.md` gains invariants 7–9, the worker
   proactive pass, and the updated layout; `docs/ASSUMPTIONS.md` gains
   entries 20–23 (typed action registry, deterministic proactivity,
   lexical-gated RRF retrieval, automatic memory proposals).

Verification state after Milestone 20 (all executed 2026-09-23): **351
pytest tests passing (incl. the fresh-database run inside
`scripts/acceptance.sh`), full Playwright suite 6/6, `uv run ruff check`
clean, ru/en locale parity 264/264, `bash scripts/acceptance.sh` all 13
checks green (fresh Docker PostgreSQL + full migration chain), working
tree clean, nothing pushed to any remote.**

# Telegram Assistant — Milestone Report (V3 final)

Date: 2026-09-24
Scope: V1 core (SPEC §1–§31) + V2 upgrades (SPEC §42–§48) + the V3
hardening priorities (Goal P1–P55). This report replaces the
2026-09-23 V2 report; where claims below differ from the old one, this
version is authoritative (notably: retrieval arms are now *independent*
— the old "lexical-gated" description was superseded in P17; the test
count is 496, not 351; the locale key parity figure is no longer a
hardcoded constant — it is enforced by `test_locale_key_parity_ru_en`).

**Claim scope (honest by design):** everything below is verified by
checks listed in §4 that run in this repository (unit/integration tests
against real PostgreSQL, browser E2E, a production-like acceptance
script, a Docker Compose build). Nothing here claims the system is
"production-ready" in an operational sense — it has not been run
against live Telegram or a live AI provider, load-tested, or
security-audited.

## 1. What was built

A self-hosted, AI-augmented personal assistant with three runtime
processes (bot, api, worker) over one PostgreSQL instance (pgvector):

- **Telegram bot (aiogram 3)** — conversation in the user's language
  (ru default / en): calendar events + reminders, workout logging and
  scheduled workouts, files (upload → ingest → RAG retrieval), facts
  (user memory) with replacement lifecycle, morning digest, motivational
  messages, onboarding. Private-chats-only with localized guard
  rejections. All outgoing text is plain-text safe (no HTML parse mode)
  and split at Telegram's message-length limits by a central helper.
- **FastAPI Mini App backend** — vanilla-JS ES-module Mini App (no
  framework, no build step; Flatpickr self-hosted in `miniapp/vendor`,
  no CDN) serving today/upcoming/new/tasks/events/**actions inbox**/
  workouts/files (search with loading/empty/error states, source +
  chunk + excerpt)/facts (replacement badge + superseded-by link)/
  settings (language, timezone via searchable IANA picker, proactive
  toggles, memory). Auth by Telegram initData HMAC verified with the
  bot token; the test-only auth bypass lives in the test harness, not
  the API code. Dates/times render in the user's configured timezone; a
  generation token + per-render AbortController prevent stale-view
  rendering under rapid tab switching.
- **Bounded conversational engine** — formal turn state machine with a
  two-call structure (structured intent/lookup, then fold/mutation) over
  independent OpenAI-compatible chat and embedding clients.
  Deterministic entity resolution in read tools; recent-entity reference
  sections in turn context; mutation previews derived from typed action
  payloads; conversation action kinds include `log_workout` and
  `schedule_workout`. A turn-mode invariant keeps the small local model
  (Qwen-class 9B) bounded: one structured call per turn phase, no tool
  loops.
- **Durable PendingAction engine** — typed action kinds with payload
  schemas, confirm/reject flow, TTL expiry, atomic confirm+execute
  (row-locked, idempotent), optimistic stale-data guard (baseline
  `updated_at`), read paths that never mutate expiry.
- **Retrieval (RAG)** — two *independent* arms (no lexical gate): a
  tsvector full-text arm with OR-style, operator-safe tsquery and a
  pgvector arm with a configurable distance bound for meaningful
  relevance filtering; adjacent-chunk proximity merging and
  position-aware citations. Embeddings are optional: chat-only
  deployments degrade cleanly when the embedding client is unconfigured.
- **Durable job queue + worker** — PostgreSQL-backed with lease +
  heartbeat; the worker owns claim + final job state while handlers own
  domain transactions (no cross-handler rollback); digest scheduling is
  conflict-tolerant (`ON CONFLICT`); bounded, off-loop file ingestion
  with streamed upload reads; delete-after-commit file lifecycle with
  terminal artifact reaping; nudge delivery is at-most-once (dedupe row
  committed before send).
- **Proactivity V3** — deterministic weekly summary, overdue nudge,
  context-aware workout nudge, all under bounded anti-spam gates and
  concurrent-pass safety.
- **Observability** — structured JSON logging with per-unit correlation
  context (request/job/turn ids); `/healthz` liveness and `/readyz`
  readiness where PostgreSQL is a hard gate and an unconfigured AI
  provider degrades (not gates) readiness.
- **i18n** — per-user language (ru/en) persisted in `user_settings`,
  central `t()` translator over flat locale dictionaries; bot, Mini App
  and background jobs resolve language at execution time.

## 2. Code layout

```
src/assistant/
  api/        FastAPI app, initData auth, /api/v1 per-domain routers,
              pydantic schemas, /healthz + /readyz
  bot/        aiogram entrypoint, per-domain handler routers,
              callbacks, keyboards, FSM states
  ai/         provider protocol, independent chat/embedding providers,
              prompts, schemas, turn state machine
  i18n/       language registry, t() translator, locales/{ru,en}.json
  models/     SQLAlchemy 2 async ORM (15 tables, pgvector HNSW index)
  services/   calendar, reminders, workouts, files, facts, chat, turns,
              actions, proactivity, digests, motivation, notifications,
              jobs (queue), users
  actions/    action-kind registry (kind → payload schema + executor)
  worker/     durable job loop (lease + heartbeat), handler registry,
              digest scheduler, proactive pass
  db/         async engine, session, Base
  logging.py  structured JSON logging, correlation context
  config.py   pydantic-settings (env-driven, audited — no dead vars)
miniapp/      index.html + app.js + js/ SPA + vendor/ (Flatpickr
              self-hosted); all UI strings from backend locales
alembic/      async migration env, 9 revisions (initial schema →
              embedding 1536→384 → user_settings.language → proactivity
              → job leases → fact replaces → pending_actions →
              overdue_nudge → fact_key_hash)
tests/        26 test modules, 496 tests, real PostgreSQL (no mocks of
              the database; AI via in-test fake providers)
e2e/          Playwright Mini App browser E2E (14 specs, isolated
              assistant_e2e database)
scripts/      acceptance.sh — 22-step production-like verification run
.github/      credential-free CI workflow
docs/         ARCHITECTURE.md, ASSUMPTIONS.md, RESEARCH.md
```

Dependency versions (pinned in `uv.lock`): Python ≥3.12, aiogram 3.31.0,
FastAPI 0.141.1, SQLAlchemy 2.0.54, asyncpg 0.31.0, Alembic 1.20.0,
Pydantic 2.13.5, openai 3.16.2, pgvector 0.5.0 (PostgreSQL 17.11 +
pgvector).

## 3. V3 priorities (P1–P55), grouped

Full per-priority detail is in `PROGRESS.md`. Grouped by theme:

- **Concurrency & transaction safety (P1–P9, P14–P15):** worker owns
  job state, handlers own domain transactions; digest scheduling
  `ON CONFLICT`; at-most-once nudge delivery; Phase A/B/C bot turn
  lifecycle so no DB transaction spans model/embedding I/O; atomic
  confirm+execute with row locking + idempotency; optimistic
  stale-data guard; read paths never mutate action expiry; calendar/
  reminder temporal invariant + stable ordering + offset dedupe.
- **Conversation quality (P10–P13, P16, P41):** deterministic entity
  resolution in read tools; recent-entity context sections; mutation
  previews from typed payloads; `log_workout` / `schedule_workout`
  action kinds; collision-resistant fact dedupe + model-referenced
  replacement; correct fact replacement lifecycle; bounded Qwen 9B
  runtime with turn-mode invariant and Qwen-output fixture tests.
- **Retrieval (P17–P20):** independent lexical/vector arms (the old
  lexical gate was removed); OR-style operator-safe tsquery;
  configurable vector distance bound; adjacent-chunk merging +
  position-aware citations.
- **Robustness (P21–P25, P27):** bounded off-loop ingestion with
  streamed reads; delete-after-commit + artifact reaping; plain-text
  safe output; message-length splitting; private-chats-only guard;
  stale-render race fix (generation token + AbortController).
- **Mini App UX (P28–P37):** user-timezone rendering everywhere;
  workout log submits picked datetime as `started_at`; tri-state PATCH
  edit flow with `ends_at` on cards; reminder presets with shared
  offset validation; file search states; action inbox inspection; fact
  status badges; IANA timezone picker (`Intl.supportedValuesOf`);
  self-hosted Flatpickr; today-dashboard stat chips.
- **Deployment & operations (P38–P55):** Proactivity V3 (weekly
  summary, overdue nudge, workout nudge, concurrent-pass safety);
  production image installs from the lockfile (`uv sync --frozen
  --no-dev`) with a lock-drift check; test auth bypass removed from
  production code; optional embedding degradation (chat-only mode);
  `/healthz` + `/readyz`; structured JSON logging with correlation
  context; per-domain router split; env-var audit (dead `APP_TIMEZONE`
  removed); documentation reality audit; credential-free CI workflow;
  acceptance script expanded to cover all 18 §51 checks (22 steps);
  §52 regression-scenario audit (28 scenarios, 3 gaps closed with
  targeted tests); §53 V2-behavior preservation audit (all 14
  behaviors have live coverage).

## 4. Verification (Goal §51/§55 — Definition of Done)

All checks executed on 2026-09-24 via `bash scripts/acceptance.sh`
(22 steps) plus in-suite coverage; full pass in the same run:

| # | Check | Result |
|---|-------|--------|
| 1 | `uv sync` (lock resolved) | OK |
| 2 | Ruff (`uv run ruff check .`) | All checks passed |
| 3 | Import check (all entrypoints import cleanly) | OK |
| 4 | uv lock drift check (frozen install matches lockfile) | OK |
| 5 | Docker Compose config valid; no non-loopback published ports (API is `127.0.0.1:8000:8000`, Postgres unpublished) | OK |
| 6 | `docker compose build` (api, bot, worker images) | Built |
| 7 | Fresh PostgreSQL databases (`assistant` + `assistant_e2e`) created | OK |
| 8 | `alembic upgrade head` from empty database — full 9-revision chain | Applied cleanly |
| 9 | Migration invariants (`tests/test_migrations.py`: head stamp, exact table set vs `Base.metadata`, pgvector extension, HNSW index) on a throwaway DB | Passed (part of suite) |
| 10 | Full pytest suite against real PostgreSQL | **496 passed** |
| 11 | Playwright Mini App E2E (14 specs, seeded `assistant_e2e` DB) | **14 passed** |
| 12 | Real-Postgres flows exercised in-suite: turns, actions confirm/expiry, ingestion, both retrieval arms, digest scheduling, proactive passes, job leases | Covered in 496 |
| 13 | Credential-free CI workflow in `.github/workflows/` (lint + tests in container, no secrets required) | Present |
| 14 | No required TODO/stub/fake implementation (grep audit of `src/` and `miniapp/`) | Clean |
| 15 | `PROGRESS.md` matches actual state | Updated |
| 16 | Documentation matches code (P49 audit: 7 of 10 drifted areas fixed, 3 judged acceptable and recorded) | Done |
| 17 | Git working tree clean after final commit | Verified at commit time |

The 496-test suite includes the §52 regression matrix (28 scenarios,
each mapped to a named test in `PROGRESS.md`), the §53 V2-behavior
preservation set, and targeted P52 gap tests (conversational workout
log/schedule proposals; Russian inflection/paraphrase retrieval).

External AI/Telegram HTTP calls are mocked in tests (fake providers,
sender stubs, locally signed initData); production integration code is
implemented and import-verified. Context7 MCP documentation was used
for the library-specific findings recorded in `docs/RESEARCH.md`.

## 5. Known limitations (honest list)

- **No live-provider verification.** AI behavior is tested with
  deterministic in-test fake providers plus Qwen output fixtures.
  Integration with a real llama.cpp/Qwen deployment is untested here.
- **No live Telegram verification.** Bot logic is tested against
  in-test updates; no Bot API token is used in CI. Polling-mode
  behavior under real load (edits, long polls) is unverified.
- **Single-node assumptions.** One Postgres, one worker process
  (lease+heartbeat makes >1 workers possible but untested at scale);
  no HA, no backup/restore runbook, no metrics/alerting beyond
  `/healthz`/`/readyz` and JSON logs.
- **Retrieval quality ceiling.** The two-arm RAG is sound (tests
  prove each arm, filtering, merging, citations) but real-world
  recall/precision with an actual embedding model is unmeasured.
- **Small-model prompt brittleness.** The bounded turn protocol
  (P41) constrains a 9B-class model; weaker models may need prompt or
  schema tuning — fixture tests guard the current contract, not
  arbitrary models.
- **Mini App is vanilla JS by design** — no framework, no bundler;
  this is a deliberate constraint, noted here so it is not mistaken
  for a gap.

## 6. How to run

```bash
uv sync
cp .env.example .env            # fill TELEGRAM_BOT_TOKEN, CHAT_*/EMBEDDING_*
docker compose up postgres      # or point DATABASE_URL at any PG 16+ + pgvector
uv run alembic upgrade head
uv run python -m assistant.bot.main      # bot
uv run python -m assistant.api.main      # api (port 8000, /miniapp served)
uv run python -m assistant.worker.main   # worker
uv run pytest                     # full suite (needs PostgreSQL)
uv run ruff check .
bash scripts/acceptance.sh        # production-like end-to-end acceptance run
```

Or `docker compose up` for all four services. See `README.md` for
configuration details, `docs/ARCHITECTURE.md` for the design
rationale, and `PROGRESS.md` for the full V3 priority log.

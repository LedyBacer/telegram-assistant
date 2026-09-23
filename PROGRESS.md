# Progress

Status: V3 PRIORITY 2 COMPLETE (atomic PendingAction lifecycle +
concurrency). Next: V3 Priority 3 (expiry semantics — no secret mutation in
read paths).

## V3 — Priority 2: atomic PendingAction confirm + execute

Confirm and execute are now one atomic, row-locked unit so a double-click
(the same action confirmed from the Telegram bot and the Mini App, or two
racing requests) cannot double-apply a non-idempotent mutation (SPEC §3):

- `services/actions.confirm_and_execute_action()` loads the action row with
  `session.get(..., with_for_update=True)` (a `SELECT ... FOR UPDATE`), then
  lazily expires, idempotently returns a stored `last_result` for an
  `executed` action, rejects terminal states, transitions
  `proposed` → `confirmed`, and delegates to `execute_action` in the same
  transaction. Two concurrent calls serialize on the row lock: the first
  confirms + executes + commits; the second re-reads the terminal row and
  returns the stored result without re-applying.
- `api/routes.py` `POST /actions/{id}/confirm` and
  `bot/handlers.py` `on_action` confirm branch both now use the atomic path.
  `ActionStaleError` commits the in-transaction expiry then 409; `ValueError`
  (rejected/expired/no-longer-valid) commits then 400.

Tests (`tests/test_actions.py`): `test_confirm_and_execute_is_idempotent`
(second call returns the stored result, item not re-moved) and
`test_concurrent_confirm_executes_once` (two `asyncio.gather` sessions each on
their own connection race a `create_item` action; the FOR UPDATE lock serializes
them and the mutation lands exactly once). Verified: 356 pytest pass, Ruff clean.

## V3 — Priority 1: worker transaction ownership (commit this session)

`worker/main.py::_run_job` no longer wraps a job handler in an outer
transaction. New ownership contract (SPEC §5): the worker owns job CLAIMING
(`poll_once`) and the FINAL job state (`_complete` / `_fail`, each a short
transaction on its own session); each handler owns its domain transactions and
commits between external I/O stages, so no DB transaction spans the Telegram /
embedding network calls. Handlers updated:
- `reminders._handle_reminder_send` and `digests._handle_digest_send` now use
  Phase A (read + commit) → Phase B (Telegram send, no open tx) → Phase C
  (re-fetch + stamp `sent_at` exactly once) — durable at-least-once delivery,
  `sent_at` written after the send so a crash re-sends rather than drops.
- `files` ingestion already committed between download/extract/embed/chunk-write.

Tests: `tests/test_worker_ingest.py` (3) drive a REAL `files.ingest` job through
`JobWorker._run_job` — happy path (file → `indexed`, job → `completed`, lease
cleared), owner-token/lease protection (a stale token cannot complete the job),
and cancel-mid-ingest (the fake embedder blocks at the embed stage until the
cancel commits, so the run's chunk-write is provably skipped). Test infra:
`tests/conftest.py` now forces `DATABASE_URL` to the test database (the shell
exports the Docker hostname `postgres`, which is unresolvable from the host, so
app-side `get_session_factory()` calls like `files._record_failure` were
order-dependent on `test_api.py` importing first). Verified: 354 pytest pass,
Ruff clean.

---

## V2 (baseline before this V3 effort)

## P13 — Final documentation and report (this session)

- **`docs/ASSUMPTIONS.md`**: entries 20–23 (bounded typed conversational
  actions with the closed `kind` registry — built-ins `create_item` /
  `cancel_item`; deterministic deduped proactivity; lexical-gated RRF
  hybrid retrieval, K=60; automatic memory proposals that stay
  `proposed` until confirmed).
- **`docs/ARCHITECTURE.md`**: invariants 7–9 (bounded typed actions with
  commit-before-409/400; facts never auto-confirmed, `supersede_fact`
  semantics; deterministic deduped proactivity), worker paragraph now
  covers the proactive pass, code layout updated (`actions/` registry,
  proactivity service, 351 tests, 6 E2E specs, full migration chain).
- **`README.md`**: new "V2 features" section (conversational actions,
  long-term memory, proactivity, lexical-gated hybrid search, workout
  scheduling, file ingestion retry); E2E paragraph updated to 6 specs
  incl. the V2 spec via the `e2e/helpers/seed.ts` asyncpg seed helper.
- **`REPORT.md`**: header brought current (all 20 milestones complete);
  §1/§2/§3/§4 updated with milestones 18–20 (commits `38bc65e`…`a33fb9d`,
  `9a2e7e5`…`124a54c`, `6324766`…`82ea1d5`) and current verification
  numbers (351 pytest, 6/6 Playwright, 264/264 locale parity, acceptance
  13/13 green on 2026-09-23); new "## 10. Milestone 19 — V2 core" and
  "## 11. Milestone 20 — V2 surface, acceptance coverage, and final docs"
  sections with the final verification state.
- Every documentation claim was verified against tool results in this
  session (migration chain grepped in `alembic/versions`, action registry
  grepped in `src/assistant/actions/`, RRF/lexical gate read in
  `services/files.py`, locale parity computed, test counts from this
  session's pytest/Playwright/Ruff runs).
- `scripts/acceptance.sh` intentionally left unchanged: a SPEC grep found
  no acceptance/E2E mandate there, and all 13 production-like checks pass.

## P12 — Tests/acceptance for the V2 features

New E2E spec `e2e/tests/v2-features.e2e.ts` (single full-flow test) covers
the five V2 features against the real API + DB: (1) Actions inbox — three
seeded proposed actions: confirm → "Сохранено." + badge "выполнено" +
created item in Today; stale target confirm → 409 toast "Это действие
больше не актуально." + badge "истекло" after re-fetch, 0 buttons; reject
→ "отклонено". (2) Workout scheduling — missing-time validation toast,
Flatpickr date + 23:50 time pick, "Workout: <name>" lands in Today.
(3) File retry — a DB-seeded `failed` file (real bytes on disk) →
"Повторить индексацию" → "в очереди". (4) Fact supersede — replace form on
a confirmed fact → new proposed fact "предложен", old fact badge "заменён"
(no replace button). (5) Proactive settings card — default values
(weekly on, 22:00/08:00/3/120 мин), switch toggle → "Настройки сохранены."
+ restore. Supporting: `e2e/helpers/seed.ts` (`runDbScript` runs asyncpg
Python against the isolated `assistant_e2e` DB from the repo root);
`global-setup.ts` now also truncates `pending_actions`,
`proactive_settings`, `nudge_deliveries`. E2E caught a real production
bug the pytest suite masked: the confirm route raised 409/400 before
committing, so the session dependency rolled back the in-transaction
`expired` marking (pytest's client overrides `get_session` with the raw
shared session and never rolls back). Fix in
`src/assistant/api/routes.py`: commit before raising in both the
`ActionStaleError` and `ValueError` branches. i18n: added
`miniapp.fact_superseded` (ru "заменён" / en "superseded") +
`FACT_STATE_KEYS`/`FACT_STATE_TONES` entry in `miniapp/app.js`.
`scripts/acceptance.sh` left as-is (SPEC does not mandate E2E there); it
stays green. Verified: full Playwright 6/6, full pytest 351 passing,
Ruff clean, ru/en locale parity 264/264, `scripts/acceptance.sh` all green
(fresh DB + migrations).

Prior: PRIORITY 11 COMPLETE (Mini App integration of the V2 features).
`miniapp/app.js` + locales + CSS: (1) new ⏳ Actions tab (second in the
bottom nav) — pending-proposals inbox from `GET /actions`: kind icon,
summary, "expires {when}" meta, status badge (pending/done/rejected/
expired), Confirm/Reject buttons on proposed; 409 → localized
"no longer applies" toast; (2) workout scheduling card (name + start
datetime + optional minutes → `POST /workouts/schedule`); (3) "Retry
indexing" button on failed file cards (`POST /files/{id}/retry`); (4)
inline "Replace" form on proposed/confirmed fact cards
(`POST /facts/{id}/supersede`, one open at a time, survives re-renders);
(5) proactive-settings card in Settings (three role=switch rows:
enabled/weekly review/workout nudge, quiet hours from/until via the
time picker, max-per-day and min-interval via option sheets) with
isolated fetch failure (a /proactive-settings error never breaks the
core settings screen). `motivationRow` generalized to `switchRow`.
21 new i18n keys per language (ru/en parity holds). E2E updated: a11y
nav count 7→8, screens-audit settings rows 3→7 / switches 1→4,
"actions" added to the touch-target loop. Verified: full pytest 351
passing, Ruff clean, full Playwright suite 5/5 (incl. per-screen
audit), ru/en locale parity.

Prior: PRIORITY 10 COMPLETE (Mini App API extension). Added the API
surface for the V2 features (all in `src/assistant/api/routes.py` +
`schemas.py`, 12 new tests in `tests/test_api.py`): (1) pending-actions
inbox — `GET /actions?status=&limit=` (proposed-first, newest),
`POST /actions/{id}/confirm` (get → 404, rejected/expired → 400,
confirm+execute in one transaction; stale target → `ActionStaleError` →
409 + expired; idempotent replay returns executed without re-running),
`POST /actions/{id}/reject` (terminal states → 400); (2) fact supersede —
`POST /facts/{id}/supersede` proposes a `replace_fact` action (old fact
stays active until confirmed, 404 cross-user/rejected); (3) proactive
settings — `GET/PATCH /proactive-settings` (row auto-created, partial
PATCH, pydantic bounds 422); (4) file ingestion retry —
`POST /files/{id}/retry` + `files_service.retry_file` (only `failed`
retryable, `rejected`/other states → 400; new job with
`file:{id}:retry:{n}` idempotency key, old job cancelled,
`extra.retry_count` incremented); (5) workout scheduling —
`POST /workouts/schedule` creates a calendar item + reminder at
`starts_at`. Verified: full suite 351 passing, Ruff clean.

Prior: PRIORITY 9 COMPLETE (bounded proactivity pass, SPEC §11, commit
124a54c). Priority 9 done: deterministic state-derived triggers only (no
model calls) — `weekly_review` on Monday in the user's local time, once per
ISO week; `workout` once per user-local day when the last workout is None or
older than 48 h (`WORKOUT_STALE_HOURS`). New models `ProactiveSettings`
(per-user: enabled, weekly_review_enabled, workout_nudge_enabled,
quiet_hours_start/end wrapping midnight, default 22:00→08:00,
max_nudges_per_day, min_interval_minutes) + `NudgeDelivery` (durable dedupe
per (user_id, kind, period_key)); migration
`20260923_9c8d7e6f5a4b_proactivity`. `services/proactivity.py`:
`evaluate_user(session, user_id: int, now, send)` — all data via explicit
SELECTs (no ORM instance state: `Session.rollback` expires every object in
the session, async lazy reload raises MissingGreenlet); anti-spam gates in
order enabled → quiet hours (user TZ) → daily cap → min interval; delivery
row flushed BEFORE send so a crashed send still dedupes.
`expire_stale_actions` marks past-expiry proposed/confirmed pending actions
expired (bulk UPDATE). `run_proactive_pass`: per-user commit/rollback
isolation (one failed send rolls back only that user; retries next pass),
returns {nudges_sent, actions_expired}. Worker: `proactive_pass()` in the
run loop every `digest_interval` seconds (default 30). i18n `proactive.*`
keys in ru/en. Tests: `tests/test_proactivity.py` (11 — triggers, dedupe,
quiet-hours wrap, daily cap, min interval, disabled, user scoping, action
expiry, pass isolation + retry). Verified: full suite 339 passing, Ruff
clean.

Prior: MILESTONE 17 COMPLETE (configurable chat timeout + explicit Qwen
thinking mode with Telegram UX). Milestone 17 done: (1) `CHAT_TIMEOUT_SECONDS`
(default 180, `1 <= x <= 3600`) drives the chat OpenAI client `Timeout`
(`read=chat timeout`, `connect=10`, `write=30`, `pool=10`, `max_retries=0`)
for normal chat AND structured generation; the embedding provider keeps its own
short (60 s) float timeout; `APITimeoutError` is mapped to the narrow
`AITimeoutError` (subclass of `AIProviderError`) with the configured value
logged and no raw provider text reaching users. (2) `CHAT_THINKING_ENABLED`
(default `true`) is sent EXPLICITLY on every chat/structured completion as
`extra_body → chat_template_kwargs.enable_thinking` (the llama.cpp
OpenAI-compatible mechanism), built centrally in `OpenAIChatProvider._chat_options()`;
it never touches embeddings. (3) Timeout vs malformed response: a full
inference timeout fails cleanly after exactly ONE provider call (no second
equally-long inference); a malformed structured response may be retried once
with corrective feedback (Pydantic validation unchanged, `extra="forbid"`).
(4) Telegram UX: when thinking is enabled, a short localized temporary status
message ("Думаю…" / "Thinking…", key `ai.thinking`) in the user's persisted
language is sent before a slow AI op (draft + chat) and deleted on success,
provider error, and timeout; send/delete failures are logged and swallowed so
they never break the flow; no status when thinking is disabled; existing
typing actions preserved. (5) `.env.example` + README document both settings.
Tests: 22 new in `tests/test_thinking_ux.py` (settings defaults/validation,
timeout reaching client / embeddings unaffected, explicit llama.cpp option on
chat + structured, clean no-retry timeout, RU/EN status lifecycle incl.
provider error + timeout + delete-failure, disabled → no status); 4 existing
single-message bot tests pin `chat_thinking_enabled=False` (their assertions
are about persistence/fallback, not UX). Verified: full suite 257 passing,
Ruff clean, imports OK, `.env.example` loads into `Settings`, `docker compose
config` valid, ru/en locale parity.

Milestone 16 COMPLETE (production runtime-hardening pass). Milestone 16
done: (1) worker `MissingGreenlet` fix — `digests.ensure_digest_jobs` eager-
loads `User.settings` via `selectinload` (also `digest_send` handler);
regression tests in `tests/test_digests.py` (users WITH and WITHOUT a
`UserSettings` row, repeated passes, plus a test pinning the lazy-load hazard).
(2) Onboarding i18n complete — every onboarding/FSM/validation user-facing
string resolves through `t()` / `LocalizableError` in the user's persisted
language (timezone, digest time, cancellation, task creation, workout + fact
+ file validation, incl. `workouts.err_notes`); RU/EN onboarding + FSM
state-isolation tests in `tests/test_onboarding_i18n.py`; user-authored
content never translated. (3) Qwen3.5/llama.cpp NL task parsing —
`chat_structured` no longer sends OpenAI-only `response_format`; JSON contract
in the system prompt; `extract_json_object` (fences, thinking preambles,
trailing prose, braces-in-strings; prefers the LAST balanced object, skips
nested ones); corrective-feedback retry (2 attempts); structured logging with
secrets redacted (`_redact_for_log`); explicit confirm-before-mutate preserved;
relative dates ("сегодня"/"today") resolved in the user's timezone (prompt
carries tz + now); regression tests with realistic Qwen-style responses in
RU/EN. (4) API loopback-only: compose `api` port now `127.0.0.1:8000:8000`;
Postgres publishes no ports; README "Network exposure" documents the HTTPS
reverse-proxy path for a future public Mini App. (5) `scripts/acceptance.sh`
— production-like verification: fresh Docker Postgres, `alembic upgrade head`,
API start + `/healthz`, worker with several digest-scheduling iterations (no
`MissingGreenlet`), bot dispatcher wiring (Telegram mocked), RU/EN onboarding
tests, NL draft tests, full pytest on the fresh DB, Ruff, `docker compose
config`, loopback-only port-exposure audit. Verified: acceptance run fully
green, full suite 235 passing, Ruff clean.

Milestone 15 (production-ready per-user
internationalization) done: `src/assistant/i18n/` registry + `t()` translator
with `locales/{ru,en}.json` (RU default + fallback, never crashes on missing
keys), `user_settings.language` (NOT NULL, server default `ru`) with new
Alembic migration `e8a2c41b7f05`, bot language picker (Settings button +
`/language`, immediate re-render, persisted), background jobs resolving the
recipient's language at execution time, explicit AI answer-language
instruction + language-neutral draft schema, Mini App rendering from the
backend locale dictionaries (`/api/v1/i18n/*`) with settings persistence,
and API `language` in settings with 422 on unsupported codes. Verified:
fresh-DB migration, full suite 206 passing, Ruff clean, api/bot/worker
import-verified, ru/en locale parity, two-user two-language tests,
no global env-var language.

Milestone 14 (independent chat/embedding
providers + 384-dim pgvector) done: split `CHAT_*` / `EMBEDDING_*` config
with legacy `OPENAI_*` fallback, independent AsyncOpenAI clients behind a
composite `OpenAICompatibleProvider`, E5 prefixes centralized in the
embedding provider, llama.cpp-compatible `/v1/embeddings` (model+input
only) with client-side dimension validation, migration
`7b492f548c86` shrinking `file_chunks.embedding` to `vector(384)` with the
HNSW cosine index rebuilt. Verified on a fresh database: `alembic upgrade
head` clean, full suite 176 passing, `ruff check` clean, api/bot/worker
import-verified, DB column `vector(384)` + `ix_file_chunks_embedding_hnsw`
confirmed in the catalog.

## Completed

- Autonomous project scaffold and specification created.
- Architecture/assumptions/research docs written (`docs/ARCHITECTURE.md`,
  `docs/ASSUMPTIONS.md`, `docs/RESEARCH.md`).
- Bootstrap: `pyproject.toml` + `uv.lock`, package skeleton under `src/assistant`,
  config via pydantic-settings (`assistant/config.py`), async SQLAlchemy engine
  (`assistant/db/engine.py`), FastAPI app with `/healthz` + static Mini App mount
  (`assistant/api/main.py`), Alembic async migration environment (`alembic/`),
  Dockerfile + docker-compose (postgres/api/bot/worker), Mini App stub.
- Verified: `uv run` import of the app succeeds; DB connection + pgvector 0.8.6
  reachable via `assistant.db.get_engine()`; `alembic heads` loads cleanly.
- Milestone 2: ORM models for all 11 SPEC §21 entities in `src/assistant/models/`
  (users, user_settings, calendar_items, reminders, workout_logs, user_files,
  file_chunks with `Vector(1536)` + HNSW cosine index, chat_messages, user_facts,
  background_jobs, digests). Async Alembic env; initial migration
  `c390315de59f_initial_schema` applied to real PostgreSQL 17 (pgvector extension and
  HNSW index verified in catalog). Ruff clean.

- Milestone 3: durable PG job queue service (`src/assistant/services/jobs.py` —
  `FOR UPDATE SKIP LOCKED` claim, status transitions, exponential-backoff retries
  (30 s base), abandoned-lock TTL recovery, idempotency keys) + worker entrypoint
  `python -m assistant.worker.main`; 11 real-PostgreSQL tests in `tests/test_jobs.py`
  (including concurrent-claimer no-double-claim) passing; Ruff clean.
- Milestone 4: aiogram 3 bot foundation under `src/assistant/bot/` —
  `callbacks.py` (pydantic `CallbackData`: Menu/Settings/Item/Draft),
  `keyboards.py` (main menu incl. conditional Mini App WebApp button, settings,
  draft confirm/cancel), `states.py` (`TaskDraftStates` FSM), `middlewares.py`
  (DB session + user upsert), `handlers.py` (`/start`, `/help`, `/cancel`, main
  menu, settings, draft confirm/cancel, structured task-draft text parsing with
  validation, chat-message persistence), `main.py` (bot polling entrypoint).
  25 credential-free tests in `tests/test_bot_foundation.py` (callbacks,
  keyboards, draft parser, and 3 real-PostgreSQL handler flows) passing;
  full suite 36 passing; Ruff clean.

- Milestone 5: calendar service (`src/assistant/services/calendar.py` — create/
  get/update/complete/cancel/delete/list_today/list_upcoming/list_range with
  user-TZ normalization and user scoping) + reminder service
  (`src/assistant/services/reminders.py` — durable `reminder_send` jobs with
  idempotency keys, offset resolution for item-linked reminders, idempotent
  delivery handler registered via `assistant.worker.handlers`). Bot wiring:
  `items_kb` per-item complete/cancel buttons, `ItemCallback` handler,
  today/upcoming routed through the service, draft confirm creates the item
  plus optional `remind:` offsets (ASSUMPTIONS #11). `tests/test_calendar.py`
  (9) + `tests/test_reminders.py` (13) against real PostgreSQL; conftest
  autouse TRUNCATE of all tables in the fixture session's transaction.
  Full suite 59 passing; Ruff clean.
- Milestone 6: workout service (`src/assistant/services/workouts.py` — log/get/
  list/stats with timezone-aware streaks, schedule_workout creating a calendar
  item + start-time reminder). `calendar.create_item` gained an `extra` kwarg.
  Bot wiring: `WorkoutStates` FSM, `workouts_kb`, workouts menu section shows
  stats + recent logs, `log_workout` / `schedule_workout` flows in `on_text`
  with `name, minutes, effort` and `name, YYYY-MM-DD HH:MM` parsing.
  `tests/test_workouts.py` (7) against real PostgreSQL including ownership
  isolation; full suite 66 passing; Ruff clean.
- Milestone 7: AI layer `src/assistant/ai/` — `AIProvider` protocol +
  `OpenAICompatibleProvider` (AsyncOpenAI, narrow `AIProviderError` /
  `AIOutputValidationError`, bounded retries, json_schema response_format,
  `store=False` on completions to avoid provider-side storage — SPEC §15),
  `AITaskDraft` Pydantic schema, `DRAFT_SYSTEM` prompt. Bot flow: manual line
  format first, AI fallback for natural language (ASSUMPTIONS #12-13); AI
  draft stored typed in FSM state, previewed with ambiguities, confirmed
  before persisting (SPEC §6). Lazy `get_ai_provider()` keeps tests
  credential-free. `tests/test_ai.py` (9) incl. full NL->preview->confirm
  flow with a fake provider; openai 3.16.2 API specifics in RESEARCH.md.
  Full suite 75 passing; Ruff clean.

- Milestone 8: file uploads + ingestion + pgvector retrieval.
  `src/assistant/services/files.py` — `register_upload` (server-side UUID
  storage key, never the user filename; unsupported/oversize persisted as
  `rejected` with a visible error, no job), durable `files.ingest` job handler
  (download -> extract (txt/md/pdf/docx, `FileUploadError` on unreadable) ->
  chunk (word-boundary, overlapping) -> batch embed -> replace chunks ->
  `indexed`), idempotent on replay, visible `failed` state written in a
  separate transaction on error; `delete_file` (rows + job + disk),
  user-scoped `list_files`/`get_file`; `retrieve_chunks` hybrid (cosine
  distance over `Vector(1536)` + keyword ilike boost 0.25) with
  `format_citations`. Job-handler registration moved to leaf module
  `src/assistant/worker/registry.py` to break the worker<->services circular
  import (reminders + files register there; `worker.main` reads
  `registry.handlers`). AI provider gained `embed()` (batch, `text-embedding-
  3-small` default) and `embedding_model` config. Bot: `on_document` registers
  uploads and reports rejections. Config: `file_storage_dir`,
  `embedding_batch_size`. `tests/test_files.py` (17) against real PostgreSQL
  with a fake download + fake embedder; full suite 92 passing; Ruff clean.

- Milestone 9: user facts lifecycle + contextual AI chat (SPEC §14-15).
  `src/assistant/services/facts.py` — `propose_fact` (starts `proposed`;
  normalized dedupe key, category, provenance, confidence), idempotent
  `confirm_fact`/`reject_fact`, `supersede_fact` (old proposed/confirmed ->
  `superseded` with `superseded_by`, new fact created as `proposed`), owner-
  scoped `get_fact`/`list_facts`/`delete_fact`, and `confirmed_lines` (only
  CONFIRMED facts ever reach chat context; `flush` only, caller owns the tx).
  Facts are NEVER auto-confirmed: the user confirms via /remember buttons.
  `src/assistant/services/chat.py` — `build_context` assembles a selective,
  bounded block (recent messages up to `chat_history_messages`, today's and
  upcoming items, pending reminders, recent workouts, confirmed fact lines,
  top-3 retrieved file chunks + citations — never the whole DB); `render_
  context` marks all of it untrusted data; `chat()` calls the provider and
  persists both user and assistant `ChatMessage` rows only after a
  successful reply (flush only). `CHAT_SYSTEM` prompt in `ai/prompts.py`
  (answer in the user's language, no inventing, treat excerpts/facts as
  untrusted data). Config: `chat_history_messages`. Bot: `FactCallback` +
  `fact_kb`, `/remember` (propose + confirm/reject/delete buttons), `/facts`
  (status-listed), `on_fact` (idempotent confirm/reject/delete), free text in
  `on_text` now routes through `chat_service.chat` with an `AIProviderError`
  fallback that persists only the user message. `tests/test_facts.py` (11) +
  `tests/test_chat.py` (9) against real PostgreSQL with a fake provider;
  full suite 116 passing; Ruff clean.

- Milestone 10: durable morning digest + motivation + real delivery
  (SPEC §16-17). `src/assistant/services/notifications.py` — worker-side
  Telegram sender (lazy shared `Bot`, same token as the bot process;
  `send_text` raises on failure so jobs re-queue with backoff).
  `src/assistant/services/motivation.py` — deterministic, state-derived
  one-line motivation (overdue / upcoming workout / streak / empty
  schedule / generic), disabled by `motivation_enabled`.
  `src/assistant/services/digests.py` — `build_digest` (today, overdue,
  upcoming-7d, workout stats, motivation line), `schedule_todays_digest`
  (one `DigestDelivery` per (user, local date) via the `uq_digests_user_day`
  constraint + `digest:{user}:{date}` idempotency key; past digest times
  fire immediately), `ensure_digest_jobs` (all-users pass), `digest_send`
  handler (send-then-stamp `sent_at`, idempotent on replay).
  `calendar.list_overdue` added. Worker: periodic `schedule_digests()` pass
  every `digest_schedule_interval_seconds` (default 30 s), `notifications.
  close()` on shutdown; `worker/handlers.py` registers the digest handler.
  `reminders._handle_reminder_send` now delivers the message through the
  Bot API before marking sent (failures re-queue). Config:
  `digest_schedule_interval_seconds`. `tests/test_digests.py` (11) against
  real PostgreSQL with a fake sender; `tests/test_reminders.py` gained an
  autouse sender stub. Full suite 127 passing; Ruff clean.

- Milestone 11: Mini App (initData HMAC) + /api/v1 authed endpoints
  (SPEC §18-20). `src/assistant/api/auth.py` — `verify_init_data`:
  Telegram's exact initData HMAC (secret = HMAC-SHA256(b"WebAppData",
  token), over the sorted data-check-string; constant-time
  `hmac.compare_digest`), duplicate-parameter rejection, hash
  well-formedness, `auth_date` freshness (max age + future-skew
  tolerance), strict user validation (positive int id, bots rejected,
  JSON shape); `get_current_user` FastAPI dependency (initData header ->
  verified user -> upsert + commit -> 401 on any `InitDataError`).
  `src/assistant/api/schemas.py` — pydantic v2 request/response models
  (`ORMModel` with `from_attributes`). `src/assistant/api/routes.py` —
  `APIRouter(prefix="/api/v1")`: `GET /me`; calendar `today` /
  `upcoming?days`; items CRUD (`POST` 201 with source=miniapp + optional
  `remind_offsets_minutes`, `PATCH`, `complete` / `cancel`, `DELETE` 204);
  workouts (`POST` 201, `GET` list, `GET /stats`); files (`GET` list,
  `DELETE` 204, `GET /search?q&top_k` -> 502 when the embedding provider
  is unavailable); reminders (`GET`, `POST` 201, `POST {id}/cancel`);
  facts (`GET`, `POST` 201 provenance=miniapp, `confirm` / `reject`,
  `DELETE` 204); settings (`GET`, `PATCH` with IANA timezone validation
  -> 422). Naive datetimes are interpreted in the user's timezone;
  every route is user-scoped (404 across users); service `ValueError` ->
  400; pydantic errors -> 422. `src/assistant/api/main.py` — FastAPI
  lifespan (dispose engine) + `/health`/`/healthz` + static `/miniapp`
  mount. `miniapp/` — vanilla-JS SPA (tabs: today / upcoming / new /
  workouts / files / facts / settings) sending the raw
  `Telegram.WebApp.initData` as `X-Telegram-Init-Data`; all user input
  rendered via `textContent`; 401 -> "reopen the app" message.
  `tests/test_init_data.py` (17, pure unit, signed payloads with the real
  algorithm) + `tests/test_api.py` (21, real PostgreSQL via
  `httpx.ASGITransport` + `dependency_overrides`, signed initData, fake
  embedder). Ruff config: `extend-immutable-calls` for FastAPI
  `Depends`/`Query` (B008). `ruff format` is NOT a project gate —
  pre-existing files are unformatted; `ruff check` is the standard.
  Full suite 164 passing; Ruff clean.

- Milestone 12: full test-suite coverage check against SPEC §26. Every
  §26 bullet verified against the suite: health (test_api), user
  isolation (test_api/test_facts/test_workouts/test_files), calendar CRUD
  (test_calendar), structured task-draft validation (test_bot_foundation),
  confirm-before-write (test_bot_foundation + test_ai), reminder
  persistence/idempotency (test_reminders), background job claiming +
  concurrent SKIP LOCKED + worker completion/failure + abandoned-job
  recovery (test_jobs), workouts (test_workouts), file metadata lifecycle
  + chunk ownership isolation + semantic retrieval filtering
  (test_files), user fact states (test_facts), Mini App initData valid /
  invalid signature + expired auth_date (test_init_data), major API
  validation errors (test_api). External AI/Telegram HTTP calls mocked
  everywhere (fake providers / sender stubs). One gap found and closed:
  migrations were never exercised by tests — added
  `tests/test_migrations.py`: (1) creates a throwaway database
  (`ta_migration_test`), runs the real `alembic upgrade head` chain
  against it, asserts the stamped head revision, exact table set vs
  `Base.metadata`, pgvector extension, and the HNSW embedding index, then
  drops the database; (2) ORM-metadata table-set drift guard. The main
  test database is never touched. Full suite 166 passing; Ruff clean.

- Milestone 13: final verification (SPEC §31 + QWEN.md) + REPORT.md.
  Verified: `uv sync` OK; `docker compose config --quiet` valid;
  `docker compose build` produced api/bot/worker images; PostgreSQL
  17.11 + pgvector 0.8.6 healthy; `alembic upgrade head` applied from an
  empty database (`ta_fresh_final`, dropped afterwards); full suite 166
  passing on the dev DB and again on the fresh migrated DB;
  `ruff check .` clean; `assistant.api.main` / `assistant.bot.main` /
  `assistant.worker.main` import-verified (FastAPI 0.141 materializes
  included routers lazily — 26 /api/v1 endpoints confirmed by the API
  tests); worker smoke path ran 15 s on an empty queue and exited 0;
  SKIP LOCKED concurrency tests and 17 initData unit tests pass; grep
  found no TODO/FIXME/stub/placeholder in `src/` or `miniapp/`; README +
  docs present. `REPORT.md` written; nothing pushed to any remote.

- Milestone 14: independent chat/embedding providers (OpenAI-compatible).
  `src/assistant/config.py` — `chat_base_url` / `chat_api_key` /
  `chat_model` and `embedding_base_url` / `embedding_api_key` /
  `embedding_model` / `embedding_dimensions` (default 384); a
  `model_validator` fills them from the legacy `OPENAI_API_KEY` /
  `OPENAI_BASE_URL` when the provider-specific variables are absent and
  requires at least one credential source. `src/assistant/ai/provider.py`
  — `OpenAIChatProvider` (chat + chat_structured, `store=False`) and
  `OpenAIEmbeddingProvider` (`embed_documents` / `embed_query`) as
  independent AsyncOpenAI clients; the embedding provider applies the E5
  prefixes centrally (`passage: ` for stored chunks, `query: ` for
  queries) and validates every returned vector against
  `EMBEDDING_DIMENSIONS` (`EmbeddingDimensionError` on mismatch); requests
  send only `model` + `input` so llama.cpp `/v1/embeddings` works.
  `OpenAICompatibleProvider` is a composite over the two; `build_ai_provider`
  wires them from settings. The `AIProvider` protocol now exposes
  `embed_documents` / `embed_query` (replacing `embed`);
  `services/files.py` updated accordingly. `models/files.py`:
  `EMBEDDING_DIMENSIONS = 384`. Migration
  `7b492f548c86_embedding_vector_384`: drops the HNSW index,
  `batch_alter_table` drops + re-adds the column as `Vector(384)`
  (pre-production: existing vectors discarded), recreates the HNSW cosine
  index; downgrade restores `vector(1536)`. Tests: `tests/test_ai.py`
  rewritten for the split clients (independent base URLs/keys/models,
  legacy fallback precedence, credential requirement, E5 prefix
  application, llama.cpp request shape, dimension validation, composite
  routing); fake providers in `test_chat.py` / `test_files.py` /
  `test_api.py` adopt the new protocol. `.env.example`, README, and
  `docs/` updated. Verified: fresh-DB migration, 176 passing, Ruff clean,
  imports OK, `vector(384)` + HNSW index in the catalog.

- Milestone 15: production-ready per-user internationalization (i18n).
  `src/assistant/i18n/service.py` — `SupportedLanguage` (StrEnum: `ru`,
  `en`), `DEFAULT_LANGUAGE`/`FALLBACK_LANGUAGE = "ru"`,
  `SUPPORTED_LANGUAGES` (derived from the enum), `LANGUAGE_NAMES`,
  `language_name()`, `is_supported()`, `load_locale()` (independent copy of
  the `@cache`d flat dict), and `t(language, key, **kwargs)`: unknown
  language → RU, key missing from active locale → RU, key missing from RU →
  the key itself (never raises), `{param}` interpolation with a safe-format
  fallback; `Translator`/`for_language()` binding. Locale dictionaries at
  `src/assistant/i18n/locales/{ru,en}.json` (same key set, parity enforced
  by a test). Migration `e8a2c41b7f05_user_settings_language`:
  `user_settings.language` `VARCHAR(16) NOT NULL DEFAULT 'ru'`, server
  default, existing rows set to `'ru'`; downgrade drops the column.
  `models/users.py`: `language` column with Python + server defaults.
  `services/users.py` `upsert_user` never reads the Telegram
  `language_code` — new users keep the default `ru`. Bot: `LanguageCallback`
  (`prefix="lang"`), `settings_kb` gained a localized 🗣 language button,
  `language_kb` (one button per supported code, localized labels),
  `cmd_language` + `on_language` (persist, re-render settings + answer
  immediately in the new language, reject unsupported codes); every
  localized bot surface (start, help, menu, settings, drafts, tasks,
  workouts, files, facts, reminders, digests, motivation, errors,
  keyboards) now renders through `t(user.language, ...)`.
  `services/reminders.py`: `create_item_reminders` stores the raw user
  text; the "Напоминание: / Reminder:" wrapper is applied at delivery in
  the recipient's CURRENT language. `services/digests.py` +
  `motivation.py`: built at execution time in the recipient's language.
  `ai/prompts.py` `CHAT_SYSTEM` gained `{language}` ("Always answer in
  {language}"); `DRAFT_SYSTEM` stays language-neutral; user text is never
  translated. API: `Settings`/`SettingsUpdate` expose `language` (422 on
  unsupported codes); new authed endpoints `GET /api/v1/i18n/languages`
  (`[{code, label}]`) and `GET /api/v1/i18n/{locale}` (404 on unknown).
  Mini App `miniapp/app.js`: all UI strings fetched from the backend
  locale dictionary for the user's language (single source of truth, no
  localStorage copy), Settings tab gains a language select, saving
  persists via `PATCH /settings` and reloads the dictionary; initData
  auth unchanged. Ruff config: per-file `F811` ignore for test fixture
  imports. Tests: `tests/test_i18n.py` (30 — service/registry, parity,
  fallbacks, interpolation, column defaults, no language_code override,
  two-user scoping, persistence, start/language bot flows, reminder +
  digest language at execution, AI language instruction, neutral draft
  prompt, API read/update/422, i18n endpoints) + targeted updates in
  existing suites (reminders/digests/facts/files/chat/api) to the new
  execution-time wrapper semantics. README + docs updated. Verified:
  fresh-DB migration, 206 passing, Ruff clean, imports OK.

- Milestone 17: configurable chat timeout + explicit Qwen thinking mode
  (see status block at top): `CHAT_TIMEOUT_SECONDS` (default 180) drives the
  chat `Timeout` for chat + structured generation (embeddings keep their own
  60 s timeout), `APITimeoutError` → narrow `AITimeoutError` logged with the
  configured value; `CHAT_THINKING_ENABLED` (default true) sent explicitly via
  `extra_body → chat_template_kwargs.enable_thinking` on every chat/structured
  call (central `_chat_options()`, never embeddings); a full inference timeout
  fails cleanly after one provider call while a malformed structured response
  may be retried once; a temporary localized "Думаю…" / "Thinking…" status
  message (key `ai.thinking`) in the user's language is sent before and removed
  after slow AI ops (success / provider error / timeout), with send/delete
  failures swallowed; `.env.example` + README updated; 22 new tests in
  `tests/test_thinking_ux.py` + 4 existing single-message bot tests pin
  thinking off. Full suite 257 passing; Ruff clean.

- Milestone 16: production runtime-hardening pass (see status block at top):
  worker `MissingGreenlet` fix (eager `selectinload(User.settings)` in
  `ensure_digest_jobs` + `digest_send` handler) with regression tests;
  onboarding i18n complete via `t()`/`LocalizableError` (RU/EN + FSM
  state-isolation tests); llama.cpp/Qwen3.5-compatible structured parsing
  (no `response_format`, `extract_json_object`, corrective retry, redacted
  structured logging, relative dates in the user's timezone) with RU/EN
  regression tests; API published loopback-only
  (`127.0.0.1:8000:8000`, README "Network exposure"); new
  `scripts/acceptance.sh` production-like verification run (fully green).
  Full suite 235 passing; Ruff clean.

## Current milestone

- Milestone 18: Mini App production-hardening pass. Frontend rewritten as
  small vanilla ES modules (`miniapp/{index.html,styles.css,app.js,
  js/{api,telegram,ui,state}.js}`, vendored Flatpickr — no framework, no
  build step); root-cause fix of the `[object HTMLDivElement]` rendering
  bug (safe `el()` DOM construction, user content as text nodes, no
  innerHTML); `GET /` → 307 → `/miniapp` implemented in FastAPI;
  Telegram-native theming via `--tg-theme-*` variables (light/dark/custom,
  runtime `themeChanged`); Flatpickr (24h, ru/en) replaces native
  pickers; reusable bottom sheet/action sheet replaces native selects
  (selected state, Escape/outside-click/cancel, keyboard focus, ARIA);
  loading/empty/populated/error states on every data screen; backend fix:
  calendar `list_items` anchors on `coalesce(starts_at, due_at)` so
  due-date-only items appear in range views. E2E: Playwright + Chromium
  (devDependency only, never in the prod image) with a deterministic
  Telegram WebApp stub, env-gated `ASSISTANT_TEST_AUTH` dependency
  override (verified inert in production: 401 without initData),
  390x844 viewport, isolated `assistant_e2e` DB truncated via a single
  `TRUNCATE ... CASCADE` in `e2e/global-setup.ts` (wired as Playwright
  `globalSetup`), 5 specs green (a11y, 20-step scenario, per-screen
  audit, screenshots, theme), console guard (suppresses Chromium's
  spurious `net::ERR_ABORTED` on completed `204 No Content` requests),
  programmatic theme/layout/a11y/touch-target assertions, 6 reference
  screenshots in gitignored `test-artifacts/screenshots/`;
  `scripts/public_smoke.sh` read-only post-deploy check (executed against
  the live public URL: it correctly FAILs there only because the public
  server still runs a pre-deployment version); README fully updated.
  The per-screen audit spec found and fixed three real issues:
  `list_range` now returns items of every status (completed/cancelled
  items stay on their calendar day so the UI can show a badge and offer
  deletion), the settings motivation switch got a full 44px hit target
  (the 30px track is drawn centered inside it), and the audit's flatpickr
  Escape step focuses the picker input (flatpickr listens for Escape on
  its input, not the document).
  Verification: 264 pytest passing (fresh DB), Ruff clean, `docker
  compose config` valid, full Playwright suite 5/5 (incl. a per-screen
  audit spec), smoke PASS locally, prod-auth negative check 401, working
  tree clean, nothing pushed.

## Next

- Manual deployment of the current `main` to
  https://telegram-assistant.bacer.ru, then re-run
  `BASE_URL=https://telegram-assistant.bacer.ru bash
  scripts/public_smoke.sh` (expected PASS after deploy).
- Future work (out of scope for this run): real Telegram/OpenAI
  credential smoke tests, HTTPS reverse proxy deployment for the Mini
  App, CI pipeline, observability, additional languages (ru/en
  supported today).

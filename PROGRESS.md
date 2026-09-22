# Progress

Status: ALL 14 MILESTONES COMPLETE. Milestone 14 (independent chat/embedding
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

## Current milestone

- None — all 14 milestones complete.

## Next

- Project is complete. Future work (out of scope for this run): real
  Telegram/OpenAI credential smoke tests, CI pipeline, observability.

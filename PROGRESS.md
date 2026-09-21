# Progress

Status: Milestone 8 (file uploads + ingestion + pgvector retrieval) complete
and green — files service, `files.ingest` worker handler, bot document
handler, `embed()` on the AI provider, 17 real-PostgreSQL tests; full suite
92 passing, Ruff clean; commit in progress.

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
  `AIOutputValidationError`, bounded retries, json_schema response_format),
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

## Current milestone

- Milestone 8 verified; commit in progress.

## Next

1. Milestone 8: commit.
2. Milestone 9: User facts lifecycle + contextual chat.

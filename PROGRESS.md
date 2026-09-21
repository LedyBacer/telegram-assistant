# Progress

Status: Milestone 3 (durable job queue) complete.

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

## Current milestone

- Milestone 3 verified and committed.

## Next

1. Milestone 4: aiogram 3 bot skeleton (`/start`, main menu, FSM, CallbackData,
   `src/assistant/bot/main.py`).
2. Calendar/reminders services + handlers.
3. Workouts services + handlers.

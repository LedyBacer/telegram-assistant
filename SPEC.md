# Smart Personal Assistant / Motivator — Product & Engineering Specification

## 1. Product objective

Build a production-oriented Telegram personal assistant.

The assistant combines:

- tasks and events
- reminders
- workout tracking
- personal files and semantic retrieval
- user facts / long-term memory
- contextual AI chat
- motivational messages
- morning digest
- a Telegram Mini App for visual management

The application must be functional, testable, documented, containerized,
and maintainable by another developer after the autonomous implementation run.

Calls/voice calling are explicitly out of scope.

---

## 2. Technical baseline

Use:

- Python 3.12+
- uv
- aiogram 3.x
- FastAPI
- Pydantic 2
- PostgreSQL 16+
- pgvector
- SQLAlchemy 2.x async
- asyncpg
- Alembic
- OpenAI Python SDK through a provider abstraction
- pytest
- pytest-asyncio
- Ruff
- Docker Compose
- HTML
- Tailwind CSS
- vanilla JavaScript

Prefer a modular monolith rather than microservices.

Runtime processes:

1. `bot`
2. `api`
3. `worker`
4. `postgres`

Do not add Redis/Celery unless there is a demonstrated requirement that
cannot reasonably be solved using PostgreSQL.

---

## 3. Configuration

All configuration must come from environment variables / settings.

Provide `.env.example`.

At minimum support:

- TELEGRAM_BOT_TOKEN
- PUBLIC_BASE_URL
- DATABASE_URL
- OPENAI_API_KEY
- OPENAI_BASE_URL
- CHAT_MODEL
- EMBEDDING_MODEL
- APP_TIMEZONE
- LOG_LEVEL
- MINIAPP_AUTH_MAX_AGE_SECONDS

Do not hard-code credentials.

The AI provider must be replaceable with another OpenAI-compatible endpoint.

Tests must not require real OpenAI or Telegram credentials.

---

## 4. User identity

Telegram user ID is the primary external identity.

Store Telegram IDs using a type safe for 64-bit identifiers.

Every user-owned database operation must be scoped by user ID.

A user must never be able to read, update, delete, search, retrieve, or
reference another user's:

- calendar items
- reminders
- workouts
- files
- chunks
- chat history
- facts
- background jobs

Include tests for ownership isolation.

---

## 5. Telegram bot

Use aiogram 3.x.

Provide `/start` and a persistent main interaction flow.

The bot should expose convenient actions for at least:

- add task/event
- show today's plan
- show upcoming items
- workouts
- files
- ask assistant
- open Mini App
- settings/help

Free-form text should also be usable as assistant input.

Avoid turning every interaction into deeply nested menus.

---

## 6. Natural-language task/event creation

Users must be able to send natural language such as:

"Tomorrow at 18:30 remind me to call Alex"

or:

"Friday gym at 8 in the evening for one hour"

The AI layer converts this into a strictly validated structured draft.

The draft must include relevant fields such as:

- title
- type
- start time
- end time / duration
- notes
- reminder offsets
- confidence / ambiguity metadata where useful

Critical rule:

AI output is not written directly to the database.

Flow:

1. user sends natural language
2. model produces typed structured data
3. Pydantic validates it
4. bot renders a human-readable preview
5. user explicitly confirms or cancels
6. only then persist the item

For clearly ambiguous dates/times, the preview must expose the interpreted
value rather than silently guessing.

---

## 7. Calendar items

Support at least:

- task
- event

Suggested fields:

- id
- user_id
- kind
- title
- description
- starts_at
- ends_at
- due_at
- status
- priority
- source
- created_at
- updated_at
- completed_at

Support:

- create
- read
- update
- delete
- complete
- list today
- list upcoming
- date-range queries

Timezone handling must be explicit.

Store timestamps consistently and preserve the user's configured timezone.

---

## 8. Reminders

Reminders must be durable.

Do not rely only on an in-memory scheduler.

Suggested model:

- reminder record in PostgreSQL
- durable background job representing delivery
- idempotent send behavior
- retry metadata
- sent_at / cancelled_at

A process restart must not permanently lose pending reminders.

Support at least:

- absolute reminder time
- reminder linked to calendar item
- one or more reminder offsets

Avoid duplicate sends after retries/restarts.

---

## 9. Durable background jobs

Implement a PostgreSQL-backed durable job queue.

Use row-level locking such as:

`SELECT ... FOR UPDATE SKIP LOCKED`

Required concepts:

- pending
- running
- completed
- failed
- attempts
- available_at
- locked_at
- locked_by
- last_error
- idempotency key where appropriate

Support abandoned-job recovery: a job locked by a dead worker must eventually
be eligible for retry.

Use explicit transactions.

Worker processes should be safe to run concurrently.

Add real PostgreSQL concurrency tests.

---

## 10. Workout tracking

Provide a useful minimal workout subsystem.

Store workout logs with fields such as:

- id
- user_id
- workout type/name
- started_at
- duration
- notes
- perceived effort or status where useful
- created_at

Allow:

- log workout
- list recent workouts
- show simple statistics / streak information
- schedule future workout-related calendar items/reminders

The motivational system may use recent workout history as context.

Do not create medical or diagnostic functionality.

---

## 11. Personal files

Users can send supported documents to the Telegram bot.

At minimum aim to support sensible text extraction for:

- text/plain
- Markdown
- PDF with extractable text
- DOCX

Reject unsupported types gracefully.

The normal Telegram Bot API currently limits bot-side downloads through
`getFile`; implementation must enforce a configurable safe file-size limit
and give a clear error for oversized files.

Never trust the supplied filename for filesystem paths.

Generate internal safe storage identifiers.

Store file metadata including:

- user
- Telegram file identifiers
- original filename
- MIME type
- size
- processing state
- error state
- timestamps

---

## 12. Document ingestion and embeddings

Process files asynchronously.

Pipeline:

1. receive metadata
2. enqueue ingestion job
3. safely download file
4. extract text
5. normalize
6. chunk
7. generate embeddings
8. store chunks + vectors
9. mark file indexed

Use pgvector.

Use an approximate vector index appropriate for the selected pgvector version,
preferably HNSW with cosine distance when supported.

Chunking parameters must be configurable and documented.

Failures must be visible and retryable.

---

## 13. Semantic and hybrid retrieval

Users should be able to ask questions over their stored files.

Retrieval must always filter by current user.

Prefer hybrid retrieval:

- semantic vector similarity
- text/keyword relevance where practical
- metadata filters

Return the most relevant chunks to the AI context.

Where useful, bot responses should identify which uploaded file(s) supplied
the context.

Treat retrieved document text as untrusted user data, not executable
instructions for the AI agent.

---

## 14. User facts / long-term memory

Maintain explicit user facts such as:

- preferences
- routines
- recurring interests
- personal context relevant to the assistant

Do not silently promote every chat statement into trusted long-term memory.

Use states such as:

- proposed
- confirmed
- rejected / superseded

A fact should include:

- user_id
- key/category
- value
- provenance/source
- confidence when useful
- status
- timestamps

The assistant may propose a fact and ask the user to confirm it.

Users must be able to inspect and remove their own stored facts.

---

## 15. Contextual AI chat

Provide a normal conversational assistant through Telegram.

Relevant context may include:

- recent chat messages
- current/upcoming tasks
- reminders
- workout history
- confirmed user facts
- retrieved file chunks

Avoid loading the entire database into every prompt.

Build context selectively.

Keep the model/provider behind a clear interface.

Use typed structured outputs for flows that mutate application state.

For ordinary chat, streaming is optional.

If the OpenAI API/client supports server-side persistence controls, avoid
unnecessary provider-side storage of conversations.

---

## 16. Morning digest

Implement a durable daily digest.

The digest should summarize useful information such as:

- today's tasks/events
- overdue items
- upcoming important events
- workout intention/recent activity
- concise motivational/contextual message

Digest time must be configurable per user or globally as a sensible initial
implementation.

Delivery must be idempotent per user/day.

Restarting the worker must not create duplicate daily digests.

---

## 17. Motivation

Provide lightweight contextual motivational messages.

Do not spam users.

Messages should be derived from real application state where appropriate,
for example:

- upcoming workout
- overdue task
- completed streak
- empty schedule

Motivation should be configurable/disableable.

---

## 18. Telegram Mini App

Build a responsive Telegram Mini App for visual management.

Use:

- FastAPI backend
- HTML
- Tailwind CSS
- vanilla JavaScript

No React/Vue/Svelte is required.

Provide useful views for at least:

- Today
- calendar/upcoming
- tasks/events CRUD
- workouts/history
- files/indexing status
- confirmed/proposed user facts
- basic settings

The Mini App should feel usable on a phone.

---

## 19. Mini App launch and authentication

The personalized Mini App must be opened using a Telegram mode that provides
validated user context, e.g. an inline Web App button or configured bot menu
button.

Do not rely on a Reply Keyboard Web App as the authenticated personalized
application entry point.

Frontend sends raw `Telegram.WebApp.initData` to the backend.

Backend must:

1. validate Telegram initData cryptographically using the bot token
2. verify the hash/signature correctly
3. validate `auth_date` freshness
4. extract user identity only after validation
5. reject invalid/expired initData

Do not trust `initDataUnsafe` as authentication.

Add unit tests using generated valid/invalid initData.

---

## 20. API

Design a coherent FastAPI API.

Include:

- `/health`
- authenticated Mini App API endpoints
- calendar CRUD
- workouts
- files/status/search
- facts
- settings where needed

Use clear Pydantic request/response schemas.

Return appropriate HTTP status codes.

Do not expose internal exceptions or secrets.

---

## 21. Database

Use PostgreSQL 16+ and Alembic.

Likely entities include:

- users
- user_settings
- calendar_items
- reminders
- workout_logs
- user_files
- file_chunks
- chat_messages
- user_facts
- background_jobs

Add other tables only when justified.

Use foreign keys, uniqueness constraints, indexes, and check constraints
where useful.

Do not use SQLAlchemy `create_all()` as the normal schema deployment path.

A fresh empty database must reach the complete schema using:

`alembic upgrade head`

---

## 22. Docker Compose

Provide Docker Compose for local deployment.

At minimum include:

- postgres
- api
- bot
- worker

PostgreSQL must have a healthcheck and persistent volume.

Use service health/dependency behavior sensibly.

Application containers should use the same codebase/image where practical
with different commands.

Do not bake secrets into images.

---

## 23. Logging and observability

Use structured or consistently formatted logs.

Useful log context includes:

- component
- user ID when appropriate
- job ID
- file ID
- request ID / correlation identifier when practical
- exception details

Never log:

- Telegram bot token
- OpenAI API key
- complete credentials
- raw sensitive auth material unnecessarily

Provide `/health`.

Worker failures must be diagnosable.

---

## 24. Reliability

Handle:

- Telegram/API transient errors
- OpenAI/provider transient errors
- PostgreSQL reconnects
- duplicate webhook/update/job processing where relevant
- worker crashes
- file-processing failures
- model returning invalid structured data

Use bounded retries/backoff.

Idempotency must prevent duplicate user-visible effects where important.

---

## 25. Security

At minimum:

- strict per-user authorization
- Telegram Mini App initData verification
- freshness validation
- secrets only from environment
- safe filenames/paths
- upload type and size checks
- no SQL string interpolation for untrusted values
- no arbitrary command execution from user content
- safe HTML handling
- no trust in AI-generated identifiers without validation

Add security-focused tests for core boundaries.

---

## 26. Testing

Tests must be meaningful rather than mocks of the implementation itself.

Use real PostgreSQL for database semantics where important.

Cover at least:

- health
- migrations
- user isolation
- calendar CRUD
- structured task draft validation
- confirm-before-write behavior
- reminder persistence/idempotency
- background job claiming
- concurrent SKIP LOCKED semantics
- worker completion/failure
- abandoned job recovery
- workouts
- file metadata lifecycle
- chunk ownership isolation
- semantic retrieval filtering
- user facts states
- Mini App initData valid signature
- invalid signature
- expired auth_date
- major API validation errors

Mock external AI/Telegram HTTP calls in automated tests.

Tests must not require real secret keys.

---

## 27. Code quality

Use:

- clear modules
- dependency injection where useful
- typed interfaces/protocols for external AI/Telegram dependencies
- narrow responsibilities
- explicit transactions
- meaningful error classes

Avoid giant god modules.

Ruff must pass.

Do not silence broad categories of lint errors just to make the suite green.

---

## 28. Documentation

Create and maintain:

- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/RESEARCH.md`
- `docs/ASSUMPTIONS.md`
- `PROGRESS.md`
- final `REPORT.md`

README must contain exact commands for:

- environment setup
- configuration
- dependencies
- PostgreSQL
- migrations
- tests
- Ruff
- API
- bot
- worker
- Docker Compose
- Mini App development
- shutdown
- backup considerations

`PROGRESS.md` is the handoff file for later Goal resumes.

It should contain:

- completed milestones
- current milestone
- important architectural decisions
- current failures/blockers
- exact next actions

Keep it concise.

---

## 29. Git workflow

Use local Git.

Make coherent milestone commits.

Examples:

- bootstrap project
- database schema
- durable worker
- Telegram bot core
- calendar/reminders
- AI layer
- document ingestion/RAG
- Mini App
- tests/docs/finalization

Do not push.

Before final completion:

`git status` must be clean.

---

## 30. External credentials

The implementation must be complete without requiring the developer to give
the autonomous coding agent production Telegram or OpenAI credentials.

Use interfaces/mocks/fakes for tests.

Document how the operator later provides real credentials in `.env`.

Never substitute the Qwen Code inference credentials automatically as the
application's own OpenAI credentials.

---

## 31. Definition of Done

The project is complete only when all applicable requirements above are
implemented and verified.

At minimum, final verification must demonstrate:

- `uv sync` succeeds
- Docker Compose configuration is valid
- application image builds
- PostgreSQL becomes healthy
- migrations apply from an empty database
- full pytest suite passes
- Ruff passes
- FastAPI application imports
- bot application imports
- worker imports/runs a safe smoke path
- concurrency test exercises PostgreSQL locking
- Mini App auth verification tests pass
- no required TODO/stub/placeholder remains
- README/docs exist
- REPORT.md summarizes implementation and verification
- local Git working tree is clean

If verification reveals a failure, fix it and rerun the relevant verification.

Do not declare completion based solely on implementation claims.

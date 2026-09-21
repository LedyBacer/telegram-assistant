# Autonomous development instructions

This repository is developed primarily by an autonomous Qwen Code agent.

Always read `SPEC.md` before making architectural decisions.

## Working style

- Work autonomously. Do not wait for user confirmation for ordinary implementation decisions.
- If a requirement is ambiguous, make the safest reasonable assumption and record it in `docs/ASSUMPTIONS.md`.
- Maintain `PROGRESS.md` as a compact handoff document.
- Make small, coherent local Git commits at meaningful milestones.
- Never push to a remote repository.
- Never commit secrets, API keys, tokens, `.env`, database data, downloaded user files, or credentials.
- Do not replace working requirements with easier mock implementations merely to satisfy tests.
- Do not leave required TODOs, stubs, fake implementations, or placeholder endpoints.

## Research

Use Context7 MCP for current library/API usage when appropriate, especially:

- aiogram 3
- FastAPI
- Pydantic 2
- SQLAlchemy 2 async
- Alembic
- OpenAI Python SDK
- pgvector integrations
- APScheduler if used

Record material research decisions in `docs/RESEARCH.md`.

Do not claim Context7 was used unless its MCP tools were actually called.

## Architecture

Prefer a modular monolith.

Primary runtime processes:

- bot
- api
- worker
- postgres

Do not introduce Redis, Celery, Kafka, RabbitMQ, Kubernetes, microservices,
or another infrastructure dependency unless SPEC.md explicitly requires it.

Background work must be durable in PostgreSQL.

## Python

- Python 3.12+
- uv / pyproject.toml / uv.lock
- type annotations
- async I/O where appropriate
- SQLAlchemy 2.x async API
- asyncpg
- Pydantic v2
- Alembic
- pytest
- pytest-asyncio
- Ruff

## Verification

Do not mark the Goal complete merely because code exists.

Before completion:

- use a fresh database
- run migrations
- run the complete test suite
- run Ruff
- verify imports
- verify Docker Compose configuration/build
- inspect Git status
- exercise important flows using real PostgreSQL
- write `REPORT.md`

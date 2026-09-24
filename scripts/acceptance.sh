#!/usr/bin/env bash
# Production-like acceptance run for the Telegram Assistant (see REPORT.md).
#
# Steps (V3 P51 — the full §51 check list):
#    1. Docker Compose validation
#    2. Port exposure audit (no public API/Postgres bind)
#    3. Production image build from the frozen lock (Dockerfile: uv sync --frozen)
#    4. Fresh Docker PostgreSQL (throwaway container, own port)
#    5. Alembic `upgrade head` on the fresh database
#    6. API starts (loopback only) and answers /healthz + /readyz
#    7. Seed users (one WITH settings, one WITHOUT)
#    8. Worker stays alive and completes digest-scheduling iterations —
#       no MissingGreenlet
#    9. Worker REAL job execution: files.ingest through JobWorker._run_job
#      (pytest: tests/test_worker_ingest.py) — intermediate commits, lease/
#      owner-token completion, cancellation commits no chunks
#   10. Job lease/heartbeat semantics (pytest: tests/test_jobs.py)
#   11. PendingAction confirmation + concurrent-execution protection
#       (pytest: tests/test_actions.py -k confirm — includes the two-session
#       concurrent-confirm test)
#   12. Representative bounded conversational flow with the fake provider
#       (pytest: tests/test_turns.py)
#   13. Ordinary chat with embeddings unavailable (pytest: targeted
#       chat-only / no-embedding tests)
#   14. Bot imports and wires its dispatcher with Telegram mocked
#   15. Russian onboarding strings
#   16. English onboarding strings
#   17. NL structured task draft with realistic llama.cpp-style responses
#   18. Complete pytest suite (against the fresh database)
#   19. Ruff
#   20. Lockfile integrity (uv.lock in sync with pyproject.toml)
#   21. Production auth: the normal app has no test-auth bypass
#       (pytest: tests/test_minapp_shell.py::test_production_app_has_no_test_auth_bypass)
#   22. Mini App Playwright E2E (Playwright acceptance stage; isolated
#       assistant_e2e database + test-only assistant.api.testing entrypoint)
#
# Reminder/digest delivery smoke: step 8 runs the real worker against the
# fresh database with Telegram mocked (fake bot token) and asserts the
# digest rows persist.
#
# Requires: docker, curl, uv, node + npm (step 22, against the dev
# PostgreSQL on localhost:5432 which the E2E config uses for its own
# assistant_e2e database). No real AI or Telegram credentials are needed
# anywhere. Usage: bash scripts/acceptance.sh
set -euo pipefail

cd "$(dirname "$0")/.."

PG_CONTAINER="ta-acceptance-pg"
PG_PORT="${PG_PORT:-5433}"
API_PORT="${API_PORT:-8100}"
IMAGE_TAG="telegram-assistant:acceptance"
WORKER_LOG="$(mktemp)"
API_LOG="$(mktemp)"
trap 'docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true; rm -f "$WORKER_LOG" "$API_LOG"' EXIT

step() { printf '\n=== %s ===\n' "$*"; }

step "1. Docker Compose validation"
docker compose config -q

step "2. Port exposure audit (no public API/Postgres bind)"
published=$(awk '
    /ports:[[:space:]]*$/ { p = 1; next }
    p && /^[[:space:]]+-[[:space:]]/ { print; next }
    p { p = 0 }
' docker-compose.yml)
if [ -n "$published" ] && \
   echo "$published" | grep -vE '^[[:space:]]+-[[:space:]]+"127\.0\.0\.1:[0-9]+:[0-9]+"$' | grep -q .; then
    echo "FAIL: non-loopback published port found in docker-compose.yml:"
    echo "$published"
    exit 1
fi
echo "OK: every published port is loopback-only (Postgres publishes none)"

step "3. Production image build (frozen lock)"
# The Dockerfile installs with `uv sync --frozen`: the build fails if
# uv.lock is missing, and step 20 verifies the lock matches pyproject.toml,
# so the image always contains the exact tested dependency versions.
docker build -t "$IMAGE_TAG" . >/dev/null
# Smoke the built artifact: the API entrypoint must import and the app must
# boot far enough to serve /healthz (process liveness, no DB needed).
# Settings validate provider credentials at import (chat required,
# embeddings optional since P42) — fake values, no network call happens.
docker run --rm \
    -e TELEGRAM_BOT_TOKEN="123456789:TEST-acceptance" \
    -e DATABASE_URL="postgresql+asyncpg://assistant:assistant@127.0.0.1:5432/assistant" \
    -e PUBLIC_BASE_URL="http://127.0.0.1:8000" \
    -e CHAT_API_KEY="fake-acceptance" -e CHAT_BASE_URL="http://127.0.0.1:9/v1" \
    "$IMAGE_TAG" \
    python -c "from assistant.api.main import app; from assistant.bot.main import main; print('imports OK')"
# V4 §33: the test-auth entry point lives in e2e/support/ (outside src/), so
# the installed package in the production image must not contain it.
docker run --rm "$IMAGE_TAG" \
    python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('assistant.api.testing') is None else 1)"
echo "OK: image built from uv.lock, API/bot entrypoints import, no test-auth module in image"

step "4. Fresh Docker PostgreSQL"
docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$PG_CONTAINER" \
    -p "127.0.0.1:${PG_PORT}:5432" \
    -e POSTGRES_USER=assistant -e POSTGRES_PASSWORD=assistant -e POSTGRES_DB=assistant \
    pgvector/pgvector:pg17 >/dev/null
for _ in $(seq 1 30); do
    if docker exec "$PG_CONTAINER" pg_isready -U assistant -d assistant >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
docker exec "$PG_CONTAINER" pg_isready -U assistant -d assistant

export DATABASE_URL="postgresql+asyncpg://assistant:assistant@127.0.0.1:${PG_PORT}/assistant"
export TEST_DATABASE_URL="$DATABASE_URL"
export TELEGRAM_BOT_TOKEN="123456789:TEST-acceptance"

step "5. Alembic upgrade head"
uv run alembic upgrade head

step "6. API starts and answers /healthz + /readyz (loopback only)"
uv run uvicorn assistant.api.main:app --host 127.0.0.1 --port "$API_PORT" >"$API_LOG" 2>&1 &
API_PID=$!
for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${API_PORT}/healthz" | grep -q '"status":"ok"'; then
        break
    fi
    sleep 1
done
curl -fsS "http://127.0.0.1:${API_PORT}/healthz" | grep -q '"status":"ok"'
# Readiness: 200 only when Postgres is reachable (the API is up against a
# fresh database at this point); AI providers are reported as components and
# never gate readiness. The probe performs no inference.
curl -fsS "http://127.0.0.1:${API_PORT}/readyz" | grep -q '"status":"ready"'
kill "$API_PID" 2>/dev/null || true
wait "$API_PID" 2>/dev/null || true
echo "OK: /healthz (liveness) and /readyz (Postgres reachable) answered"

step "7. Seed users (one WITH settings, one WITHOUT)"
uv run python - <<'PY'
import asyncio
from datetime import time

from assistant.db import dispose_engine, get_session_factory
from assistant.models.users import User, UserSettings

async def main():
    factory = get_session_factory()
    async with factory() as session:
        # User WITH settings; digest far in the future so the worker does
        # not need to (and cannot, with the fake token) deliver it.
        user_a = User(id=1, first_name="Acceptance")
        session.add(user_a)
        session.add(UserSettings(user_id=1, timezone="UTC", digest_time=time(23, 59),
                                 motivation_enabled=False, language="ru"))
        # User WITHOUT any UserSettings row.
        session.add(User(id=2, first_name="NoSettings"))
        await session.commit()
    await dispose_engine()

asyncio.run(main())
PY

step "8. Worker: real execution — digest scheduling iterations, no MissingGreenlet"
WORKER_POLL_INTERVAL_SECONDS=0.5 DIGEST_SCHEDULE_INTERVAL_SECONDS=2 \
    uv run python -m assistant.worker.main >"$WORKER_LOG" 2>&1 &
WORKER_PID=$!
sleep 12
kill "$WORKER_PID" 2>/dev/null || true
wait "$WORKER_PID" 2>/dev/null || true
if grep -q "MissingGreenlet" "$WORKER_LOG"; then
    echo "FAIL: MissingGreenlet in worker log:"; grep -n "MissingGreenlet" "$WORKER_LOG"; exit 1
fi
if grep -q "worker poll iteration failed" "$WORKER_LOG"; then
    echo "FAIL: worker poll iteration failed:"; grep -n "poll iteration failed" "$WORKER_LOG"; exit 1
fi
grep -q "scheduled 2 new digest(s)" "$WORKER_LOG"
echo "OK: worker stayed alive, scheduled digests for both users"
docker exec "$PG_CONTAINER" psql -U assistant -d assistant -tAc \
    "SELECT count(*) FROM digests" | grep -qx 2
echo "OK: 2 digest deliveries persisted"

step "9. Real files.ingest through JobWorker._run_job (not the handler directly)"
# Proves the worker transaction-ownership contract on a real cross-layer
# path: intermediate commits inside ingestion, lease/owner-token completion,
# and a cancelled run committing no chunks.
uv run pytest tests/test_worker_ingest.py -q

step "10. Job lease/heartbeat semantics"
uv run pytest tests/test_jobs.py -q

step "11. PendingAction confirmation + concurrent-execution protection"
# Includes the two independent sessions confirming one action concurrently
# (real PostgreSQL row lock) and the TTL/expiry/replay matrix.
uv run pytest tests/test_actions.py -k confirm -q

step "12. Bounded conversational flow (fake provider)"
uv run pytest tests/test_turns.py -q

step "13. Ordinary chat with embeddings unavailable"
uv run pytest \
    "tests/test_ai.py::test_settings_allow_chat_only_without_embedding" \
    "tests/test_turns.py::test_ordinary_turn_makes_no_embedding_calls" \
    -q

step "14. Bot imports and wires dispatcher (Telegram mocked, no API calls)"
uv run python - <<'PY'
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from assistant.bot.handlers import router
from assistant.bot.middlewares import DBSessionMiddleware

bot = Bot(token="123456789:TEST-acceptance",
          default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
dp.message.outer_middleware(DBSessionMiddleware())
dp.callback_query.outer_middleware(DBSessionMiddleware())
dp.include_router(router)
assert router.name
print("OK: bot dispatcher wired (router=%r)" % router.name)
PY

step "15. RU onboarding strings"
uv run pytest tests/test_onboarding_i18n.py -k ru -q

step "16. EN onboarding strings"
uv run pytest tests/test_onboarding_i18n.py -k en -q

step "17. NL structured task draft (llama.cpp-style responses)"
uv run pytest tests/test_ai.py -q

step "18. Full test suite (fresh database)"
# The full suite includes the file-upload test, which writes to the configured
# storage dir; point it at a writable temp dir (matching the canonical gate) so
# the step does not depend on /data being writable in the host environment.
timeout 900 env FILE_STORAGE_DIR="$(mktemp -d)" uv run pytest -q

step "19. Ruff"
uv run ruff check .

step "20. Lockfile integrity (production must build from uv.lock, not pyproject ranges)"
# `uv lock --check` exits non-zero if uv.lock is missing or would be changed
# to match pyproject.toml — i.e. a dependency added/edited without a matching
# `uv lock`. This is what keeps `uv sync --frozen` in the Dockerfile from
# silently installing a drifted (bypassed) set of pins.
uv lock --check
echo "OK: uv.lock is in sync with pyproject.toml"

step "21. Production auth has no test-auth bypass"
# The production create_app() never consults any environment flag for the
# test-auth override; the override lives only in e2e/support/test_app.py
# (outside src/, absent from the production image — see step 3).
uv run pytest "tests/test_minapp_shell.py::test_production_app_has_no_test_auth_bypass" -q
uv run pytest "tests/test_minapp_shell.py::test_package_has_no_test_auth_module" -q

step "22. Mini App Playwright E2E (Playwright acceptance stage)"
# Isolated E2E database (E2E_DATABASE_URL, default assistant_e2e on the
# dev PostgreSQL; created + migrated + truncated by e2e/global-setup.ts)
# served by the test-only entrypoint; the real telegram.org script is
# blocked and initData is a deterministic stub.
npm ci
npm run test:e2e

step "All acceptance checks passed"

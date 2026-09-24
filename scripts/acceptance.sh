#!/usr/bin/env bash
# Production-like acceptance run for the Telegram Assistant (see REPORT.md).
#
# Steps:
#   1.  Fresh Docker PostgreSQL (throwaway container, own volume)
#   2.  Alembic `upgrade head` on the fresh database
#   3.  API starts (loopback only) and answers /healthz
#   4.  Worker starts and stays alive
#   5.  Worker completes several digest-scheduling iterations with users
#       WITH and WITHOUT UserSettings rows — no MissingGreenlet
#   6.  Bot imports and wires its dispatcher with Telegram mocked (no API calls)
#   7.  Russian onboarding strings (pytest: tests/test_onboarding_i18n.py)
#   8.  English onboarding strings (same file)
#   9.  NL structured task draft with realistic llama.cpp-style responses
#       (pytest: tests/test_ai.py)
#   10. Complete pytest suite (against the fresh database)
#   11. Ruff
#   12. Docker Compose validation
#   13. No public (non-loopback) port exposure for API/Postgres
#
# Requires: docker, curl, uv. Usage: bash scripts/acceptance.sh
set -euo pipefail

cd "$(dirname "$0")/.."

PG_CONTAINER="ta-acceptance-pg"
PG_PORT="${PG_PORT:-5433}"
API_PORT="${API_PORT:-8100}"
WORKER_LOG="$(mktemp)"
API_LOG="$(mktemp)"
trap 'docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true; rm -f "$WORKER_LOG" "$API_LOG"' EXIT

step() { printf '\n=== %s ===\n' "$*"; }

step "12. Docker Compose validation"
docker compose config -q

step "13. Port exposure audit (no public API/Postgres bind)"
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

step "1. Fresh Docker PostgreSQL"
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

step "2. Alembic upgrade head"
uv run alembic upgrade head

step "3. API starts and answers /healthz (loopback only)"
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

step "Seed users (one WITH settings, one WITHOUT)"
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

step "4+5. Worker: several digest-scheduling iterations, no MissingGreenlet"
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

step "6. Bot imports and wires dispatcher (Telegram mocked, no API calls)"
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

step "7+8. RU and EN onboarding strings"
uv run pytest tests/test_onboarding_i18n.py -q

step "9. NL structured task draft (llama.cpp-style responses)"
uv run pytest tests/test_ai.py -q

step "10. Full test suite (fresh database)"
# The full suite includes the file-upload test, which writes to the configured
# storage dir; point it at a writable temp dir (matching the canonical gate) so
# the step does not depend on /data being writable in the host environment.
timeout 900 env FILE_STORAGE_DIR="$(mktemp -d)" uv run pytest -q

step "11. Ruff"
uv run ruff check .

step "14. Lockfile integrity (production must build from uv.lock, not pyproject ranges)"
# `uv lock --check` exits non-zero if uv.lock is missing or would be changed
# to match pyproject.toml — i.e. a dependency added/edited without a matching
# `uv lock`. This is what keeps `uv sync --frozen` in the Dockerfile from
# silently installing a drifted (bypassed) set of pins.
uv lock --check
echo "OK: uv.lock is in sync with pyproject.toml"

step "All acceptance checks passed"

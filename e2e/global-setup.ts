import { execFileSync, execSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const db = process.env.E2E_DATABASE ?? "assistant_e2e";
// Full URL of the isolated E2E database. Defaults to the local dev
// Postgres; any machine/CI can point E2E_DATABASE_URL elsewhere.
const rawDbUrl =
  process.env.E2E_DATABASE_URL ??
  `postgresql://assistant:assistant@localhost:5432/${db}`;
// Accept both `postgresql://` and `postgresql+asyncpg://` and derive the
// asyncpg (script) and SQLAlchemy (alembic/webServer) forms from one source.
const asyncpgUrl = rawDbUrl.replace(/^postgresql\+asyncpg:\/\//, "postgresql://");
// Maintenance connection used for CREATE DATABASE: the `postgres` database
// of the server that hosts the E2E database (explicitly overridable).
// Swap only the last path segment (lastIndexOf — the URL's `://` contains
// slashes that a regex would match first).
const adminUrl =
  process.env.E2E_DATABASE_ADMIN_URL ??
  asyncpgUrl.slice(0, asyncpgUrl.lastIndexOf("/")) + "/postgres";
const sqlalchemyUrl = rawDbUrl.replace(/^postgresql:\/\//, "postgresql+asyncpg://");

/**
 * Prepare an isolated, freshly-migrated E2E database and start every E2E
 * run from empty tables so the scenario is deterministic.
 *
 * Steps (against the server named by E2E_DATABASE_URL, defaulting to the
 * local dev Postgres on :5432):
 *   1. CREATE DATABASE if missing,
 *   2. alembic upgrade head (idempotent),
 *   3. TRUNCATE all application tables RESTART IDENTITY.
 */
export default function globalSetup(): void {
  const env = {
    ...process.env,
    E2E_DB_URL: asyncpgUrl,
    E2E_ADMIN_URL: adminUrl,
  };
  // execFileSync (no shell) so asyncpg's "$1" placeholder is not expanded
  // by the shell; the connection URLs are passed via env (no interpolation).
  const runPy = (script: string): void =>
    execFileSync("uv", ["run", "python", "-c", script], {
      cwd: root,
      stdio: "inherit",
      env,
    });

  // 1. Create the isolated database if it does not exist yet (a brand-new
  //    server/DB has no tables, so the TRUNCATE runs AFTER the migration).
  runPy(`
import asyncio
import os

import asyncpg

DB_URL = os.environ["E2E_DB_URL"]
ADMIN_URL = os.environ["E2E_ADMIN_URL"]
# Database name from the URL path (for the existence check / CREATE).
DB = DB_URL.rsplit("/", 1)[-1].split("?")[0]

async def main() -> None:
    admin = await asyncpg.connect(ADMIN_URL)
    exists = await admin.fetchval(
        "select 1 from pg_database where datname=$1", DB
    )
    if not exists:
        await admin.execute(f'CREATE DATABASE "{DB}"')
    await admin.close()

asyncio.run(main())
`);

  // 2. Migrate the isolated database to head (idempotent).
  execSync(
    `uv run alembic upgrade head`,
    {
      cwd: root,
      stdio: "inherit",
      env: {
        ...process.env,
        DATABASE_URL: sqlalchemyUrl,
      },
    },
  );

  // 3. TRUNCATE all application tables so the run starts from empty.
  runPy(`
import asyncio
import os

import asyncpg

DB_URL = os.environ["E2E_DB_URL"]

TABLES = (
    "digests", "file_chunks", "user_files", "chat_messages", "user_facts",
    "workout_logs", "reminders", "calendar_items", "background_jobs",
    "user_settings", "users",
    # V2 tables (actions inbox + proactivity) so their state resets too.
    "pending_actions", "proactive_settings", "nudge_deliveries",
)

async def main() -> None:
    conn = await asyncpg.connect(DB_URL)
    try:
        # A single multi-table TRUNCATE: PostgreSQL refuses to TRUNCATE a
        # table while rows in a FK-referencing table (e.g. reminders ->
        # calendar_items, file_chunks -> user_files) still reference it, so a
        # per-table loop silently leaves stale rows behind. CASCADE in one
        # statement clears everything atomically.
        await conn.execute(
            "TRUNCATE "
            + ", ".join(f'"{t}"' for t in TABLES)
            + " RESTART IDENTITY CASCADE"
        )
    finally:
        await conn.close()

asyncio.run(main())
`);
}

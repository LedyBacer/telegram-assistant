import { execFileSync, execSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const db = process.env.E2E_DATABASE ?? "assistant_e2e";

/**
 * Prepare an isolated, freshly-migrated `assistant_e2e` database and start
 * every E2E run from empty tables so the scenario is deterministic.
 *
 * Steps (all against the local Postgres the dev container exposes on :5432):
 *   1. CREATE DATABASE if missing,
 *   2. alembic upgrade head (idempotent),
 *   3. TRUNCATE all application tables RESTART IDENTITY.
 */
export default function globalSetup(): void {
  const script = `
import asyncio
import asyncpg

DB = "${db}"

TABLES = (
    "digests", "file_chunks", "user_files", "chat_messages", "user_facts",
    "workout_logs", "reminders", "calendar_items", "background_jobs",
    "user_settings", "users",
)

async def main() -> None:
    admin = await asyncpg.connect(
        "postgresql://assistant:assistant@localhost:5432/postgres"
    )
    exists = await admin.fetchval(
        "select 1 from pg_database where datname=$1", DB
    )
    if not exists:
        await admin.execute(f'CREATE DATABASE "{DB}"')
    await admin.close()

    conn = await asyncpg.connect(
        f"postgresql://assistant:assistant@localhost:5432/{DB}"
    )
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
`;

  // 1. Create the DB (if missing) + TRUNCATE all application tables.
  // execFileSync (no shell) so asyncpg's "$1" placeholder is not expanded
  // by the shell.
  execFileSync("uv", ["run", "python", "-c", script], {
    cwd: root,
    stdio: "inherit",
  });

  // 2. Migrate the isolated database to head (idempotent).
  execSync(
    `uv run alembic upgrade head`,
    {
      cwd: root,
      stdio: "inherit",
      env: {
        ...process.env,
        DATABASE_URL: `postgresql+asyncpg://assistant:assistant@localhost:5432/${db}`,
      },
    },
  );
}

import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const db = process.env.E2E_DATABASE ?? "assistant_e2e";

/** The isolated E2E database name (matches playwright.config.ts). */
export const e2eDbName = db;

/**
 * Asyncpg-form URL of the isolated E2E database — the same env var and
 * default as e2e/global-setup.ts, so seed scripts always hit the database
 * globalSetup prepared.
 */
export const e2eDbUrl = (
  process.env.E2E_DATABASE_URL ??
  `postgresql://assistant:assistant@localhost:5432/${db}`
).replace(/^postgresql\+asyncpg:\/\//, "postgresql://");

/**
 * Run a complete Python script (asyncpg available) from the repo root against
 * the isolated E2E database. Used to seed state that no UI flow can create
 * (e.g. proposed pending actions, a `failed` file row).
 *
 * The web server's cwd is the repo root, so relative paths in the script
 * (e.g. the file storage dir) resolve exactly like in the API process.
 */
export function runDbScript(script: string): void {
  execFileSync("uv", ["run", "python", "-c", script], {
    cwd: root,
    stdio: "inherit",
  });
}

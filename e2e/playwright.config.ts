import { defineConfig, devices } from "@playwright/test";

// Isolated port + database for the E2E run so it never collides with a
// developer's local API or touches the real `assistant` database.
const E2E_PORT = Number(process.env.E2E_PORT ?? 8123);
const E2E_DB = process.env.E2E_DATABASE ?? "assistant_e2e";
// Must match e2e/global-setup.ts (same env vars, same defaults): the API
// server and the DB prep must always agree on the isolated database.
// Accept `postgresql://` or `postgresql+asyncpg://`; the app needs the
// SQLAlchemy asyncpg form.
const E2E_DB_URL = (
  process.env.E2E_DATABASE_URL ??
  `postgresql+asyncpg://assistant:assistant@localhost:5432/${E2E_DB}`
).replace(/^postgresql:\/\//, "postgresql+asyncpg://");
const BASE = `http://127.0.0.1:${E2E_PORT}`;

export default defineConfig({
  testDir: "./tests",
  testMatch: /\.e2e\.ts$/,
  // Reset the isolated E2E database (create + migrate + TRUNCATE) before
  // every run so the scenario assertions start from empty tables.
  globalSetup: "./global-setup.ts",
  // Screenshots land in a gitignored artifact dir (see .gitignore).
  outputDir: "../test-results",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: !!process.env.CI,
  reporter: [["list"]],
  use: {
    baseURL: BASE,
    // Primary mobile viewport (iPhone 14-class). No horizontal overflow is
    // allowed at this width.
    viewport: { width: 390, height: 844 },
    // Deterministic locale/timezone so date formatting assertions are stable.
    locale: "ru-RU",
    timezoneId: "UTC",
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chromium"] },
    },
  ],
  webServer: {
    // The test-only entry point (outside src/, so it is absent from the
    // production image) wraps the production app and installs a
    // deterministic test user (no initData). The production
    // `assistant.api.main` has no such bypass — see tests/test_minapp_shell.py.
    command: "uv run python e2e/support/test_app.py",
    cwd: "..",
    url: `${BASE}/healthz`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: {
      ...process.env,
      ASSISTANT_API_PORT: String(E2E_PORT),
      DATABASE_URL: E2E_DB_URL,
      PUBLIC_BASE_URL: BASE,
      TELEGRAM_BOT_TOKEN: "e2e-test-token",
      OPENAI_API_KEY: "e2e-key",
      OPENAI_BASE_URL: "http://127.0.0.1:9/v1",
      FILE_STORAGE_DIR: "storage/e2e-files",
      LOG_LEVEL: "WARNING",
    },
  },
});

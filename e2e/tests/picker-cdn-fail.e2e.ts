import { test, expect } from "@playwright/test";
import { installTelegramStub } from "../helpers/telegram-stub";
import { createConsoleGuard } from "../helpers/console-guard";
import { goTab } from "../helpers/app";

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);

/**
 * §23: When the Flatpickr CDN fails to load, clicking a picker field shows
 * a visible error toast instead of silently doing nothing.
 *
 * We do NOT use openApp() because it installs the local Flatpickr CDN
 * interception (which would serve the JS successfully). Instead we set up
 * the Telegram stub and console guard manually, then intercept the exact
 * jsDelivr URLs with abort() so window.flatpickr is never defined.
 */
test("picker CDN failure shows visible error toast", async ({ page }) => {
  const guard = createConsoleGuard(page, base);
  await installTelegramStub(page);

  // Abort the Flatpickr CDN assets (exact URLs from e2e/helpers/flatpickr-cdn.ts).
  await page.route(
    "https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/flatpickr.min.js",
    (route) => route.abort(),
  );
  await page.route(
    "https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/flatpickr.min.css",
    (route) => route.abort(),
  );
  await page.route(
    "https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/l10n/ru.js",
    (route) => route.abort(),
  );

  // Navigate to the app.
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.waitForURL("**/miniapp", { timeout: 15_000 });
  await page
    .locator("#view .calendar, #view .state-error")
    .first()
    .waitFor({ state: "visible", timeout: 20_000 });

  // Navigate to the New view where picker fields are present.
  await goTab(page, "new");
  // The start picker is the third .picker-field (kind=0, priority=1, starts=2).
  const startPicker = page.locator("#view .picker-field").nth(2);
  await startPicker.click();

  // The toast should appear with the picker-unavailable message.
  await expect(page.locator("#toast")).toContainText(
    "Не удалось загрузить выбор даты",
  );

  guard.assertClean();
});

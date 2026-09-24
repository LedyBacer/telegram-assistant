import { test, expect, type Page } from "@playwright/test";
import { installTelegramStub } from "../helpers/telegram-stub";
import { installFlatpickrCdn } from "../helpers/flatpickr-cdn";
import { createConsoleGuard } from "../helpers/console-guard";

// V5 §14: a boot-time Russian fallback dictionary means the UI renders real
// Russian text even when the /api/v1/i18n/{lang} fetch fails, and the <html>
// lang + <title> track the active language.
test.use({ timezoneId: "UTC" });

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);

test("UI renders Russian from the fallback when the i18n fetch fails", async ({
  page,
}: {
  page: Page;
}) => {
  // Set up the guard (and pre-allow the i18n abort) BEFORE navigating, so the
  // deliberately-failed i18n request is not reported as a missing asset.
  const guard = createConsoleGuard(page, base);
  guard.allow("/api/v1/i18n");
  await installTelegramStub(page);
  await installFlatpickrCdn(page);
  // Fail the i18n fetch (a real network abort) to exercise the catch path.
  await page.route("**/api/v1/i18n/**", (route) => route.abort());

  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.waitForURL("**/miniapp", { timeout: 15_000 });
  await page
    .locator("#view .calendar, #view .state-error")
    .first()
    .waitFor({ state: "visible", timeout: 20_000 });
  try {
    // html lang + title are set from the locale at boot (ru by default).
    await expect
      .poll(() => page.evaluate(() => document.documentElement.lang)).toBe("ru");
    await expect
      .poll(() => page.evaluate(() => document.title)).toBe("Ассистент");

    // The nav rendered from the fallback dictionary: the Today tab is real
    // Russian, and no raw "miniapp.*" i18n key leaked into the DOM.
    await expect(page.locator("#nav .nav-btn").filter({ hasText: "Сегодня" })).toBeVisible();
    const rawKeys = await page.evaluate(() =>
      Array.from(document.querySelectorAll("#app *"))
        .map((n) => n.textContent || "")
        .filter((t) => /miniapp\.[a-z]/.test(t))
    );
    expect(rawKeys).toHaveLength(0);
  } finally {
    guard.assertClean();
  }
});

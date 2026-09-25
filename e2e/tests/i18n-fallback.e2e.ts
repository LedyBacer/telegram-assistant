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
    // V5.1 P1 #14: the static shell is localized too.
    await expect(page.locator("#app-title")).toHaveText("Ассистент");
    await expect(page.locator("#nav")).toHaveAttribute("aria-label", "Навигация");
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

// V5.1 P1 #13: the client's reported language (language_code "en") picks the
// ENGLISH bootstrap fallback before /me resolves — so a user with an English
// client never sees Russian (or raw keys) on first paint, even when the i18n
// fetch fails. P1 #14: the static shell (#app-title, #nav aria-label) is
// localized too.
test("UI renders English from the fallback when the client language is en", async ({
  page,
}: {
  page: Page;
}) => {
  const guard = createConsoleGuard(page, base);
  guard.allow("/api/v1/i18n");
  await installTelegramStub(page);
  // Override the reported client language AFTER the stub is installed but
  // BEFORE the app modules run (tgLanguage reads initDataUnsafe + user).
  await page.addInitScript(() => {
    const w = (window as any).Telegram.WebApp;
    w.initDataUnsafe.user.language_code = "en";
    w.user.language_code = "en";
    w.language = "en";
  });
  await installFlatpickrCdn(page);
  // Fail the i18n fetch (the remote EN dictionary never loads) and hold
  // /me in flight: while /me is pending the ONLY dictionary in play is
  // the bootstrap fallback, so the first paint must be English (P1 #13).
  let releaseMe = () => {};
  const meGate = new Promise<void>((resolve) => {
    releaseMe = resolve;
  });
  await page.route("**/api/v1/i18n/**", (route) => route.abort());
  await page.route("**/api/v1/me", async (route) => {
    await meGate;
    await route.continue();
  });

  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.waitForURL("**/miniapp", { timeout: 15_000 });
  // First paint from the EN fallback: the localized shell (P1 #14), the
  // <title>, and the <html> lang are all English — no Russian leaked.
  await expect(page.locator("#app-title")).toHaveText("Assistant");
  await expect(page.locator("#nav")).toHaveAttribute("aria-label", "Navigation");
  await expect
    .poll(() => page.evaluate(() => document.title))
    .toBe("Assistant");
  await expect
    .poll(() => page.evaluate(() => document.documentElement.lang))
    .toBe("en");

  // Release /me and let boot finish: the stored preference (ru for the
  // shared test user) drives the dictionary and the shell follows it.
  releaseMe();
  await page
    .locator("#view .calendar, #view .state-error")
    .first()
    .waitFor({ state: "visible", timeout: 20_000 });
  try {
    await expect(page.locator("#app-title")).toHaveText("Ассистент");
    await expect(page.locator("#nav")).toHaveAttribute("aria-label", "Навигация");
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

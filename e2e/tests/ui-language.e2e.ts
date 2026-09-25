import { test, expect, type Page } from "@playwright/test";
import { installTelegramStub } from "../helpers/telegram-stub";
import { installFlatpickrCdn } from "../helpers/flatpickr-cdn";
import { createConsoleGuard } from "../helpers/console-guard";

// V5.2 §9: normalizeUiLanguage maps the client-reported language tag to the
// UI language — primary subtag `en` → "en", anything else → "ru". Asserted
// (a) directly against the module for the full matrix, and (b) end-to-end:
// a client reporting "en-US" paints the app in English before /me resolves,
// then /me's stored preference takes over.

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);

test("normalizeUiLanguage: en, en-US, en-GB → en; ru, ru-RU, unknown → ru", async ({
  page,
}: {
  page: Page;
}) => {
  const { openApp } = await import("../helpers/app");
  const guard = await openApp(page, base);
  try {
    const results = await page.evaluate(async () => {
      const { normalizeUiLanguage } = await import("/miniapp/js/state.js");
      return [
        normalizeUiLanguage("en"),
        normalizeUiLanguage("en-US"),
        normalizeUiLanguage("en-GB"),
        normalizeUiLanguage("ru"),
        normalizeUiLanguage("ru-RU"),
        normalizeUiLanguage("fr-CA"),
        normalizeUiLanguage(undefined),
      ];
    });
    expect(results).toEqual(["en", "en", "en", "ru", "ru", "ru", "ru"]);
  } finally {
    guard.assertClean();
  }
});

test("client language en-US boots in English, stored preference wins after /me", async ({
  page,
}: {
  page: Page;
}) => {
  const guard = createConsoleGuard(page, base);
  await installTelegramStub(page);
  await installFlatpickrCdn(page);
  // Runs on every navigation, after the stub: report en-US as the client
  // language before the app modules execute.
  await page.addInitScript(() => {
    if (window.__tg) window.__tg.setLanguage("en-US");
  });

  // Delay /me so the English first paint (bootstrap fallback dict) is
  // observable before the stored preference is applied.
  await page.route("**/api/v1/me", async (route) => {
    const response = await route.fetch();
    await new Promise((r) => setTimeout(r, 1500));
    await route.fulfill({ response });
  });

  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.waitForURL("**/miniapp", { timeout: 15_000 });
  // The state.js module sets <html lang> from the client language at import
  // time (index.html's static value is "ru") — this flips to "en" before
  // the delayed /me can apply the stored preference.
  await page.waitForFunction(
    () => document.documentElement.lang === "en",
    { timeout: 10_000 },
  );

  // Before /me resolves: the app is in English (normalizeUiLanguage("en-US")).
  expect(await page.evaluate(() => document.documentElement.lang)).toBe("en");
  expect(await page.evaluate(() => document.title)).toBe("Assistant");

  // After /me resolves: the stored preference (ru) takes over.
  await page.waitForFunction(
    () => document.documentElement.lang === "ru",
    { timeout: 15_000 },
  );
  expect(await page.evaluate(() => document.title)).toBe("Ассистент");

  guard.assertClean();
});

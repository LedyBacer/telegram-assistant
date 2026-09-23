import { test, expect } from "@playwright/test";
import { openApp } from "../helpers/app";

// V3 P29: the workout-log form must submit the picked datetime (a naive
// user-TZ wall clock), not silently fall back to "now". Here the browser is
// Europe/Amsterdam (UTC+2) and the user is Europe/Moscow (UTC+3) with a
// frozen clock; the picker shows the user's wall clock, so picking
// "2026-09-20 18:30" must store 2026-09-20T15:30:00Z and display back as
// 18:30 on the 20th — including after a full page reload.

test.use({ timezoneId: "Europe/Amsterdam" });

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);
const FROZEN_NOW = "2026-09-24T22:00:00.000Z";
const NAME = "P29 bench";
const WALL_DISPLAY = "20 сент., 18:30";
const STORED_UTC = "2026-09-20T15:30:00Z";

async function goWorkouts(page: import("@playwright/test").Page) {
  await page.locator('.nav-btn[data-tab="workouts"]').click();
  await page
    .locator("#view .item-title, #view .state-empty")
    .first()
    .waitFor({ timeout: 15_000 });
}

test("workout log stores the picked datetime in the user timezone", async ({ page }) => {
  await page.clock.install({ time: new Date(FROZEN_NOW) });

  const settingsRes = await page.request.patch(`${base}/api/v1/settings`, {
    data: { timezone: "Europe/Moscow" },
  });
  expect(settingsRes.status()).toBe(200);

  const guard = await openApp(page, base);
  await goWorkouts(page);

  // Log form: first .field-input is the name; first .picker-field is "when".
  await page.locator("#view .field-input").first().fill(NAME);
  await page.locator("#view .picker-field").first().click();
  await page.locator(".flatpickr-calendar").waitFor({ state: "visible" });

  // Pick the 20th (a past day) at 18:30 in the displayed (user-TZ) wall clock.
  await page.locator(".flatpickr-calendar .flatpickr-day", { hasText: "20" }).click();
  await page.locator(".flatpickr-time input").nth(0).fill("18");
  await page.locator(".flatpickr-time input").nth(1).fill("30");
  await page.locator(".picker-input").focus();
  await page.keyboard.press("Escape");
  await expect(page.locator(".flatpickr-calendar")).toBeHidden();
  await expect(page.locator("#view .picker-field").first()).toContainText(WALL_DISPLAY);

  // Submit — the form must send the picked wall value as started_at.
  await page.locator("#view .btn-primary", { hasText: "Записать" }).click();
  await expect(page.locator("#toast")).toContainText("Тренировка записана.");

  // The new log is listed with the user-TZ wall time.
  const card = page.locator("#view .card", { hasText: NAME }).first();
  await expect(card.locator(".item-meta")).toContainText(WALL_DISPLAY);

  // Reload: the exact timestamp survives and still renders in the user zone.
  await page.reload({ waitUntil: "domcontentloaded" });
  await page
    .locator("#view .calendar, #view .state-error")
    .first()
    .waitFor({ state: "visible", timeout: 20_000 });
  await goWorkouts(page);
  await expect(page.locator("#view .card", { hasText: NAME }).first().locator(".item-meta"))
    .toContainText(WALL_DISPLAY);

  // Ground truth: stored exactly as Moscow wall 18:30 on the 20th = 15:30 UTC.
  const listRes = await page.request.get(`${base}/api/v1/workouts?limit=10`);
  expect(listRes.status()).toBe(200);
  const logs = (await listRes.json()) as Array<{ name: string; started_at: string }>;
  const saved = logs.find((w) => w.name === NAME);
  expect(saved?.started_at).toBe(STORED_UTC);

  guard.assertClean();

  // Restore the shared test user's default timezone for later tests.
  const restore = await page.request.patch(`${base}/api/v1/settings`, {
    data: { timezone: "UTC" },
  });
  expect(restore.status()).toBe(200);
});

import { test, expect, type Page } from "@playwright/test";
import { openApp } from "../helpers/app";

// V4 §25/§26: flatpickr renders the datetime picker in the BROWSER's local
// time, so seeding it from an aware instant drifts the picker by the
// browser/user offset. Here the browser is pinned to Europe/Amsterdam
// (UTC+2 in September) while the user's timezone is Europe/Moscow (UTC+3,
// no DST). The clock is frozen at:
//
//   now = 2026-09-24T17:00:00Z
//     -> Moscow:    2026-09-24 20:00   (the user's "now")
//     -> Amsterdam: 2026-09-24 19:00   (the browser's "now")
//
// The picker must (1) default an empty field to the USER's wall time, not the
// browser's; (2) not drift when a value is chosen, closed, and reopened
// (still 20:00, never 19:00); (3) save the instant that IS 20:00 Moscow; and
// (4) render 20:00 after a reload.
//
// The midnight spec (below) freezes a clock where the browser and user fall on
// DIFFERENT calendar days, proving the date — not just the time — follows the
// user's zone.

test.use({ timezoneId: "Europe/Amsterdam" });

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);
const FROZEN_NOW = "2026-09-24T17:00:00.000Z";
// 2026-09-24T17:00:00Z = 20:00 Moscow (UTC+3) on the 24th, 19:00 Amsterdam
// (UTC+2) on the 24th. Same calendar day, one hour apart in the time.
const MOSCOW_NOW = "2026-09-24 20:00";
const AMSTERDAM_NOW = "2026-09-24 19:00";
const ITEM_TITLE = "Picker roundtrip";

// The New view builds four picker-field buttons in fixed DOM order:
// kind (0), priority (1), starts (2), due (3).
const startPicker = (page: Page) => page.locator("button.picker-field").nth(2);
const pickerInput = (page: Page) => page.locator("input.picker-input");

/** Close the open flatpickr (its input keydown handler closes on Escape). */
async function closePicker(page: Page) {
  await pickerInput(page).focus();
  await page.keyboard.press("Escape");
  await expect(pickerInput(page)).toHaveCount(0);
}

async function setUserTz(page: Page, tz: string) {
  const res = await page.request.patch(`${base}/api/v1/settings`, {
    data: { timezone: tz },
  });
  expect(res.status()).toBe(200);
}

test("datetime picker defaults to the user timezone and round-trips without drift", async ({ page }) => {
  await page.clock.install({ time: new Date(FROZEN_NOW) });
  await setUserTz(page, "Europe/Moscow");

  const guard = await openApp(page, base);
  await page.locator('.nav-btn[data-tab="new"]').click();

  // 1. An empty datetime picker defaults to the USER's current wall clock
  //    (20:00 Moscow), not the browser's (19:00 Amsterdam).
  await startPicker(page).click();
  const input = pickerInput(page);
  await expect(input).toBeVisible();
  await expect(input).toHaveValue(MOSCOW_NOW);
  expect(await input.inputValue()).not.toBe(AMSTERDAM_NOW);

  // 2. Choose the value, close, and reopen: it must NOT drift to 19:00.
  await closePicker(page);
  const value = await startPicker(page).locator(".picker-value").innerText();
  expect(value).toContain("20:00");

  await startPicker(page).click();
  await expect(pickerInput(page)).toHaveValue(MOSCOW_NOW);
  await closePicker(page);

  // 3. Save: the backend stores the instant that IS 20:00 Moscow on the 24th
  //    = 17:00Z, exactly the frozen instant.
  await page.locator("#view input.field-input").first().fill(ITEM_TITLE);
  const createRes = await page
    .locator("#view button.btn-primary")
    .click()
    .then(() => page.waitForResponse((r) => r.url().includes("/api/v1/items")));
  expect(createRes.status()).toBe(201);
  const created = await createRes.json();
  expect(new Date(created.starts_at).getTime()).toBe(Date.parse(FROZEN_NOW));

  // 4. Reload: the item renders at 20:00 (user TZ), not 19:00 (browser TZ).
  await page.reload();
  await page
    .locator("#view .calendar, #view .state-error")
    .first()
    .waitFor({ state: "visible", timeout: 20_000 });
  const item = page.locator("#view .item-title", { hasText: ITEM_TITLE });
  await expect(item).toBeVisible();
  const meta = (await item.locator("xpath=ancestor::div[contains(@class,'card')]").locator(".item-meta").first().innerText()).replace(/\s+/g, " ");
  expect(meta).toContain("20:00");
  expect(meta).not.toContain("19:00");

  guard.assertClean();
  await setUserTz(page, "UTC");
});

test("datetime picker uses the user timezone's date across midnight", async ({ page }) => {
  // now = 2026-09-24T21:30:00Z
  //   -> Moscow:    2026-09-25 00:30  (already the 25th)
  //   -> Amsterdam: 2026-09-24 23:30  (still the 24th)
  //
  // This is the window where the user and the browser fall on DIFFERENT
  // calendar days. A browser-local default would show the 24th at 23:30; the
  // correct app shows the 25th at 00:30 — proving the date, not just the time,
  // follows the user's timezone.
  await page.clock.install({ time: new Date("2026-09-24T21:30:00.000Z") });
  await setUserTz(page, "Europe/Moscow");

  const guard = await openApp(page, base);
  await page.locator('.nav-btn[data-tab="new"]').click();

  await startPicker(page).click();
  await expect(pickerInput(page)).toHaveValue("2026-09-25 00:30");
  expect(await pickerInput(page).inputValue()).not.toBe("2026-09-24 23:30");
  await closePicker(page);

  guard.assertClean();
  await setUserTz(page, "UTC");
});

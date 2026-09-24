import { test, expect, type Page, type Locator } from "@playwright/test";
import { openApp } from "../helpers/app";

// V4 §27: the New surface must create an event with BOTH a start and an end in
// a single pass (no second edit), reusing the existing date controls, and the
// shared backend invariant (ends_at >= starts_at) must reject an inverted pair.
//
// The browser is pinned to Europe/Amsterdam (UTC+2 in September) while the user
// timezone is Europe/Moscow (UTC+3); the clock is frozen so the wall times are
// deterministic and the created instant is ground-truthable.
//
// Isolation: the whole suite shares one E2E database and a single test user and
// runs sequentially. This spec sorts early, so it must leave "today" empty and
// the user timezone restored to UTC — it deletes every item it creates and
// resets the timezone in a finally block.

test.use({ timezoneId: "Europe/Amsterdam" });

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);
// 2026-09-24T12:00:00.000Z = 15:00 Moscow / 14:00 Amsterdam, both on the 24th.
const FROZEN_NOW = "2026-09-24T12:00:00.000Z";

// New view picker fields, fixed DOM order: kind (0), priority (1),
// starts (2), end (3), due (4).
const startPicker = (page: Page) => page.locator("button.picker-field").nth(2);
const endPicker = (page: Page) => page.locator("button.picker-field").nth(3);
const kindPicker = (page: Page) => page.locator("button.picker-field").nth(0);
const pickerInput = (page: Page) => page.locator("input.picker-input");

async function closePicker(page: Page) {
  await pickerInput(page).focus();
  await page.keyboard.press("Escape");
  await expect(pickerInput(page)).toHaveCount(0);
}

// Pick the 24th at HH:MM in the displayed (user-TZ) wall clock and close.
async function pickAt(
  page: Page,
  picker: (page: Page) => Locator,
  hh: string,
  mm: string,
) {
  await picker(page).click();
  await page.locator(".flatpickr-calendar").waitFor({ state: "visible" });
  await page
    .locator(".flatpickr-calendar .flatpickr-day", { hasText: "24" })
    .click();
  await page.locator(".flatpickr-time input").nth(0).fill(hh);
  await page.locator(".flatpickr-time input").nth(1).fill(mm);
  await closePicker(page);
}

async function setUserTz(page: Page, tz: string) {
  const res = await page.request.patch(`${base}/api/v1/settings`, {
    data: { timezone: tz },
  });
  expect(res.status()).toBe(200);
}

// Run a spec body in Moscow with the clock frozen, guaranteeing the shared
// test user is left in UTC and no created items survive (for later specs that
// assume an empty "today").
async function inMoscow<T>(
  page: Page,
  body: (page: Page) => Promise<{ itemIds?: number[] } | void>,
): Promise<void> {
  await page.clock.install({ time: new Date(FROZEN_NOW) });
  await setUserTz(page, "Europe/Moscow");
  try {
    const result = await body(page);
    for (const id of result?.itemIds ?? []) {
      await page.request.delete(`${base}/api/v1/items/${id}`);
    }
  } finally {
    await page.request.patch(`${base}/api/v1/settings`, { data: { timezone: "UTC" } });
  }
}

test("New surface creates an event with start and end in one pass", async ({ page }) => {
  const TITLE = "Event one-pass";
  await inMoscow(page, async (p) => {
    const guard = await openApp(p, base);
    await p.locator('.nav-btn[data-tab="new"]').click();
    await p.locator("#view input.field-input").first().fill(TITLE);

    // Kind: event (sheet rows: task 0, event 1).
    await kindPicker(p).click();
    const sheet = p.locator("#sheet-root .sheet");
    await expect(sheet).toBeVisible();
    await sheet.locator(".sheet-row").nth(1).click();
    await expect(sheet).toBeHidden();

    // Start = 10:00 and end = 12:00 (Moscow wall, same day) in a single pass.
    await pickAt(p, startPicker, "10", "00");
    await expect(startPicker(p)).toContainText("10:00");
    await pickAt(p, endPicker, "12", "00");
    await expect(endPicker(p)).toContainText("12:00");

    // One save: 201, an event, both timestamps present, end strictly after start.
    const createRes = await p
      .locator("#view button.btn-primary")
      .click()
      .then(() => p.waitForResponse((r) => r.url().includes("/api/v1/items")));
    expect(createRes.status()).toBe(201);
    const created = (await createRes.json()) as {
      id: number;
      kind: string;
      starts_at: string | null;
      ends_at: string | null;
    };
    expect(created.kind).toBe("event");
    expect(created.starts_at).not.toBeNull();
    expect(created.ends_at).not.toBeNull();
    // 10:00 Moscow = 07:00Z; 12:00 Moscow = 09:00Z.
    expect(created.starts_at).toBe("2026-09-24T07:00:00Z");
    expect(created.ends_at).toBe("2026-09-24T09:00:00Z");

    // Reload: the item renders with BOTH the start and end times — a single
    // create pass, no second edit required.
    await p.reload();
    await p
      .locator("#view .calendar, #view .state-error")
      .first()
      .waitFor({ state: "visible", timeout: 20_000 });
    const card = p.locator("#view .card", { hasText: TITLE }).first();
    await expect(card).toBeVisible();
    const meta = (await card.locator(".item-meta").allInnerTexts()).join("  ");
    expect(meta).toContain("10:00");
    expect(meta).toContain("12:00");

    guard.assertClean();
    return { itemIds: [created.id] };
  });
});

test("New surface rejects an inverted end (end before start)", async ({ page }) => {
  const TITLE = "Event inverted";
  await inMoscow(page, async (p) => {
    const guard = await openApp(p, base);
    await p.locator('.nav-btn[data-tab="new"]').click();
    await p.locator("#view input.field-input").first().fill(TITLE);

    // Start = 12:00, end = 10:00 -> end strictly before start.
    await pickAt(p, startPicker, "12", "00");
    await pickAt(p, endPicker, "10", "00");

    // The 400 below is the controlled outcome this spec is asserting.
    guard.allow("/api/v1/items");

    // The shared invariant rejects it at the API: 400, and the form stays.
    const createRes = await p
      .locator("#view button.btn-primary")
      .click()
      .then(() => p.waitForResponse((r) => r.url().includes("/api/v1/items")));
    expect(createRes.status()).toBe(400);
    expect((await createRes.json()).detail).toContain("ends_at");
    await expect(p.locator("#toast")).toBeVisible();

    // Nothing was created.
    await p.reload();
    await p
      .locator("#view .calendar, #view .state-error")
      .first()
      .waitFor({ state: "visible", timeout: 20_000 });
    await expect(p.locator("#view .card", { hasText: TITLE })).toHaveCount(0);

    guard.assertClean();
  });
});

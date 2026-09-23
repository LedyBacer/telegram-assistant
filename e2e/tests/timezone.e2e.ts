import { test, expect } from "@playwright/test";
import { openApp } from "../helpers/app";

// V3 P28: the Mini App must render dates and times in the USER's configured
// timezone (state.me.settings.timezone), not the browser's. Here the browser
// is pinned to Europe/Amsterdam (UTC+2 in September) while the user's
// timezone is Europe/Moscow (UTC+3, no DST). The clock is frozen so the two
// zones fall on DIFFERENT calendar days:
//
//   now = 2026-09-24T22:00:00Z
//     -> Moscow:   2026-09-25 01:00  (Moscow "today"  = 2026-09-25)
//     -> Amsterdam:2026-09-24 00:00  (browser "today" = 2026-09-24)
//
// The seeded item is anchored at 2026-09-25 00:30 Moscow
// (= 2026-09-24T21:30Z = 2026-09-24 23:30 Amsterdam). A browser-local
// implementation would mark the 24th as "today" and show the item at 23:30
// on the 24th; the correct app marks the 25th as "today", groups the item
// there, and shows 00:30.

test.use({ timezoneId: "Europe/Amsterdam" });

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);
const FROZEN_NOW = "2026-09-24T22:00:00.000Z";
const MOSCOW_TODAY = "2026-09-25";
const AMSTERDAM_TODAY = "2026-09-24";
const ITEM_TITLE = "TZ boundary item";

test("dates and times follow the user timezone, not the browser timezone", async ({ page }) => {
  // Deterministic clock: browser (Amsterdam) and user (Moscow) days differ.
  await page.clock.install({ time: new Date(FROZEN_NOW) });

  // User's timezone = Moscow (test auth: deterministic user, no initData).
  const settingsRes = await page.request.patch(`${base}/api/v1/settings`, {
    data: { timezone: "Europe/Moscow" },
  });
  expect(settingsRes.status()).toBe(200);
  expect(await settingsRes.json()).toEqual(
    expect.objectContaining({ timezone: "Europe/Moscow" }),
  );

  // Item at 00:30 Moscow on 2026-09-25 = 21:30 UTC = 23:30 Amsterdam on the 24th.
  const itemRes = await page.request.post(`${base}/api/v1/items`, {
    data: { title: ITEM_TITLE, kind: "task", starts_at: "2026-09-25T00:30:00+03:00" },
  });
  expect(itemRes.status()).toBe(201);

  const guard = await openApp(page, base);

  // "Today" is the user's today (25th), not the browser's (24th).
  const todayCell = page.locator("#view .cal-day.is-today");
  await expect(todayCell).toHaveAttribute("data-date", MOSCOW_TODAY);
  await expect(page.locator(`#view .cal-day[data-date="${AMSTERDAM_TODAY}"].is-today`)).toHaveCount(0);

  // The month range query must cover the Moscow month slice: the 25th cell
  // carries the event dot.
  await expect(page.locator(`#view .cal-day[data-date="${MOSCOW_TODAY}"]`)).toHaveClass(/has-events/);

  // The selected day (user's today) lists the item ...
  const item = page.locator("#view .item-title", { hasText: ITEM_TITLE });
  await expect(item).toBeVisible();

  // ... with the time in Moscow wall clock (00:30), not Amsterdam (23:30).
  const card = item.locator("xpath=ancestor::div[contains(@class,'card')]");
  const meta = (await card.locator(".item-meta").first().innerText()).replace(/\s+/g, " ");
  expect(meta).toContain("00:30");
  expect(meta).not.toContain("23:30");

  // The day header shows the user's date (25), not the browser's (24).
  const header = (await page.locator("#view .view-subtitle").first().innerText()).trim();
  expect(header).toContain("25");
  expect(header).not.toMatch(/^24\b/);

  guard.assertClean();

  // Restore the shared deterministic test user's default timezone so later
  // tests (which assume UTC) don't inherit Moscow day boundaries.
  const restore = await page.request.patch(`${base}/api/v1/settings`, {
    data: { timezone: "UTC" },
  });
  expect(restore.status()).toBe(200);
});

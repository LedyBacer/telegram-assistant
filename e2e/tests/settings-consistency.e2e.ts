import { test, expect } from "@playwright/test";
import { openApp, goTab } from "../helpers/app";

// V5 §18: Settings consistency — switch rollback, proactive load error,
// IANA timezone from backend, tz change resets calendar date.

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);

test("settings consistency: switch rollback, proactive error, IANA list, tz date shift", async ({
  page,
}) => {
  const guard = await openApp(page, base);

  // =====================================================================
  // §18.1 — Switch rollback on failed PATCH
  // =====================================================================
  await goTab(page, "settings");
  const proactiveCard = page.locator("#view .card", {
    has: page.locator('h2.view-title:text-is("Проактивные уведомления")'),
  });
  await expect(proactiveCard).toHaveCount(1);

  const weeklySwitch = proactiveCard.locator(
    'input.switch[aria-label="Еженедельный обзор (понедельник)"]',
  );
  await expect(weeklySwitch).toBeChecked();

  // Intercept PATCH /proactive-settings with a 500 to simulate server failure.
  guard.allow("/proactive-settings");
  await page.route("**/api/v1/proactive-settings", (route) => {
    if (route.request().method() === "PATCH") {
      route.fulfill({ status: 500, json: { detail: "internal error" } });
    } else {
      route.continue();
    }
  });

  // Toggle the switch: PATCH fails → switch rolls back to previous state.
  await weeklySwitch.click();
  // The switch should revert to checked (its previous state).
  await expect(weeklySwitch).toBeChecked();
  // An error toast is shown (not the success toast).
  await expect(page.locator("#toast")).toContainText("Что-то пошло не так");

  // Remove the intercept and verify the switch works normally again.
  await page.unroute("**/api/v1/proactive-settings");
  await weeklySwitch.click();
  await expect(page.locator("#toast")).toContainText("Настройки сохранены.");
  expect(await weeklySwitch.isChecked()).toBe(false);
  // Restore to checked (default).
  await weeklySwitch.click();
  await expect(page.locator("#toast")).toContainText("Настройки сохранены.");
  await expect(weeklySwitch).toBeChecked();

  // =====================================================================
  // §18.3 — Proactive load failure shows error state with Retry
  // =====================================================================
  // Intercept GET /proactive-settings with 500.
  guard.allow("/proactive-settings");
  await page.route("**/api/v1/proactive-settings", (route) => {
    if (route.request().method() === "GET") {
      route.fulfill({ status: 500, json: { detail: "service unavailable" } });
    } else {
      route.continue();
    }
  });

  // Re-render settings: the proactive card should show an error state.
  await goTab(page, "today");
  await goTab(page, "settings");
  const errorState = page.locator("#view .state-error");
  await expect(errorState).toBeVisible();
  await expect(errorState).toContainText("Не удалось загрузить");
  const retryBtn = errorState.locator(".btn-primary", { hasText: "Повторить" });
  await expect(retryBtn).toBeVisible();

  // Remove intercept and retry: the card loads successfully.
  await page.unroute("**/api/v1/proactive-settings");
  await retryBtn.click();
  await expect(proactiveCard).toHaveCount(1);
  await expect(weeklySwitch).toBeChecked();

  // =====================================================================
  // §18.5 — IANA timezone list from backend (>400 entries)
  // =====================================================================
  const tzRow = page.locator("button.settings-row", { hasText: "Часовой пояс" });
  await tzRow.click();
  const panel = page.locator(".sheet", { has: page.locator("input.sheet-search") });
  await expect(panel).toBeVisible();
  // The backend endpoint returns 400+ IANA zones (more than Intl's list).
  expect(await panel.locator(".sheet-row").count()).toBeGreaterThanOrEqual(400);
  // Cancel the sheet.
  await panel.locator(".sheet-cancel-wrap .btn").click();
  await expect(panel).toBeHidden();

  // =====================================================================
  // §18.4 — Timezone change resets calendar date (is-today shifts)
  // =====================================================================
  // Use a fixed clock: 2026-07-20T20:00:00Z.
  // In UTC: today = 2026-07-20.
  // In Pacific/Kiritimati (UTC+14): 20:00+14h = 2026-07-21T10:00 → today = 2026-07-21.
  await page.clock.install({ time: new Date("2026-07-20T20:00:00Z") });

  // Create an item that starts on 2026-07-21 (visible only in Kiritimati tz).
  const createRes = await page.request.post(`${base}/api/v1/items`, {
    data: {
      title: "TZ-shift-item",
      kind: "task",
      starts_at: "2026-07-21T09:00:00+00:00",
    },
  });
  expect(createRes.status()).toBe(201);

  // In UTC (current tz): the July 21 item is NOT today (today is July 20).
  await goTab(page, "today");
  expect(
    await page.locator("#view .item-title", { hasText: "TZ-shift-item" }).count(),
  ).toBe(0);

  // Change timezone to Pacific/Kiritimati: today becomes July 21.
  await goTab(page, "settings");
  await tzRow.click();
  const tzPanel = page.locator(".sheet", { has: page.locator("input.sheet-search") });
  await expect(tzPanel).toBeVisible();
  const search = tzPanel.locator("input.sheet-search");
  await search.fill("Kiritimati");
  await tzPanel.locator(".sheet-row", { hasText: "Pacific/Kiritimati" }).click();
  await expect(page.locator("#toast")).toContainText("Настройки сохранены.");
  await expect(tzRow.locator(".settings-row-value")).toHaveText("Pacific/Kiritimati");

  // Now in Kiritimati: the July 21 item IS today.
  await goTab(page, "today");
  await expect(
    page.locator("#view .item-title", { hasText: "TZ-shift-item" }),
  ).toBeVisible();

  // --- Cleanup ---
  // Delete the test item.
  // Use the API to delete (faster and cleaner than UI interaction).
  const itemsRes = await page.request.get(
    `${base}/api/v1/items?start=2026-07-01T00:00:00Z&end=2026-08-01T00:00:00Z`,
  );
  const items = await itemsRes.json();
  const target = (Array.isArray(items) ? items : []).find(
    (i: { title: string }) => i.title === "TZ-shift-item",
  );
  if (target) {
    const delRes = await page.request.delete(`${base}/api/v1/items/${target.id}`);
    expect(delRes.status()).toBe(204);
  }

  // Restore timezone to UTC so later specs see pristine state.
  const restore = await page.request.patch(`${base}/api/v1/settings`, {
    data: { timezone: "UTC" },
  });
  expect(restore.status()).toBe(200);

  guard.assertClean();
});

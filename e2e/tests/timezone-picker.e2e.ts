import { test, expect } from "@playwright/test";
import { openApp, goTab } from "../helpers/app";

// V3 P35: the Mini App timezone setting is a searchable picker over the full
// canonical IANA list (Intl.supportedValuesOf), not a hardcoded 15-zone array.

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);

test("timezone picker: searchable IANA list, pick persists", async ({ page }) => {
  const guard = await openApp(page, base);
  await goTab(page, "settings");

  const tzRow = page.locator("button.settings-row", { hasText: "Часовой пояс" });
  await expect(tzRow).toHaveCount(1);
  await tzRow.click();

  const panel = page.locator(".sheet", { has: page.locator("input.sheet-search") });
  await expect(panel).toBeVisible();
  const search = panel.locator("input.sheet-search");
  await expect(search).toBeFocused();

  // The browser's IANA list (400+ zones) replaces the hardcoded array.
  expect(await panel.locator(".sheet-row").count()).toBeGreaterThanOrEqual(100);

  // Search narrows the list: Berlin stays visible, Tokyo is filtered out.
  await search.fill("Berlin");
  await expect(panel.locator(".sheet-row", { hasText: "Europe/Berlin" })).toBeVisible();
  await expect(panel.locator(".sheet-row", { hasText: "Asia/Tokyo" })).toBeHidden();
  await expect(panel.locator(".sheet-row:visible")).toHaveCount(1);

  // A query with no match shows the localized empty state.
  await search.fill("zzzz-not-a-zone");
  await expect(panel.locator(".sheet-row:visible")).toHaveCount(0);
  await expect(panel.locator(".sheet-empty")).toBeVisible();

  // Pick the zone: PATCH persists, the row value updates, toast shows.
  await search.fill("Berlin");
  await panel.locator(".sheet-row", { hasText: "Europe/Berlin" }).click();
  await expect(page.locator("#toast")).toContainText("Настройки сохранены.");
  await expect(tzRow.locator(".settings-row-value")).toHaveText("Europe/Berlin");
  guard.assertClean();

  // Restore the deterministic test user's default so later specs see UTC.
  const restore = await page.request.patch(`${base}/api/v1/settings`, {
    data: { timezone: "UTC" },
  });
  expect(restore.status()).toBe(200);
});

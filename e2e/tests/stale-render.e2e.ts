import { test, expect, type Page } from "@playwright/test";
import { openApp } from "../helpers/app";

// V3 P27 regression: an async view that resolves AFTER the user switched
// tabs must not overwrite the newer screen (stale-render race). The Today
// month-range request is delayed; rapid tab switching must keep the newest
// view in the DOM and drop the stale one.

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);
const DELAY_MS = 1500;

async function goTab(page: Page, tab: string): Promise<void> {
  await page.locator(`.nav-btn[data-tab="${tab}"]`).click();
  // Leaving a dirty New/Edit form pops a discard confirmation (V5 §15); these
  // navigation tests don't care about the unsaved form, so confirm it away.
  const dialog = page.locator('[role="alertdialog"]');
  if (await dialog.isVisible().catch(() => false)) {
    await dialog.locator(".btn", { hasText: "Покинуть" }).click();
  }
}

test("rapid tab switching never renders a stale view", async ({ page }) => {
  const guard = await openApp(page, base);

  // Start from a view that renders without network, then make the Today
  // month-range request slow for the rest of the test.
  await goTab(page, "new");
  await expect(page.locator("#view .field-input").first()).toBeVisible();

  await page.route(
    (url) => url.pathname === "/api/v1/items" && url.searchParams.has("start"),
    (route) => {
      setTimeout(() => route.continue(), DELAY_MS);
    },
  );

  // 1) Today: the delayed month-range request goes in flight.
  await goTab(page, "today");
  await expect(page.locator("#view .state-loading")).toBeVisible();

  // 2) Rapidly switch to Actions before the delayed response lands.
  await page.waitForTimeout(400);
  await goTab(page, "actions");

  // The fresh (fast) Actions view commits; after the stale response window
  // has passed, the delayed Today calendar must NOT have overwritten it.
  await expect(page.locator("#view .state-loading")).toHaveCount(0);
  await page.waitForTimeout(DELAY_MS + 800);
  await expect(page.locator("#view .calendar")).toHaveCount(0);
  await expect(page.locator("#view .state-loading")).toHaveCount(0);
  await expect(page.locator('.nav-btn[data-tab="actions"]')).toHaveAttribute(
    "aria-current",
    "page",
  );

  // 3) Same race in the other direction: a second delayed Today request must
  //    not clobber the fast Upcoming view.
  await goTab(page, "today");
  await page.waitForTimeout(400);
  await goTab(page, "upcoming");
  await expect(page.locator("#view .state-loading")).toHaveCount(0);
  await page.waitForTimeout(DELAY_MS + 800);
  await expect(page.locator("#view .calendar")).toHaveCount(0);
  await expect(page.locator('#nav .nav-btn[data-tab="upcoming"]')).toHaveClass(
    /is-active/,
  );

  // 4) With the delay route removed, Today renders normally again — the
  //    generation mechanism did not break the happy path.
  await page.unroute(
    (url) => url.pathname === "/api/v1/items" && url.searchParams.has("start"),
  );
  await goTab(page, "today");
  await expect(page.locator("#view .calendar").first()).toBeVisible();

  guard.assertClean();
});

import { expect, type Page } from "@playwright/test";
import { installTelegramStub } from "./telegram-stub";
import { installFlatpickrCdn } from "./flatpickr-cdn";
import { createConsoleGuard } from "./console-guard";

/**
 * Open the Mini App in a page: install the deterministic Telegram stub and
 * the pinned Flatpickr CDN interception (both before any module runs), guard
 * the console, navigate to the root, and wait until the default (Today) view
 * has rendered. Returns the console guard for the caller to assert on at the
 * end of the test.
 */
export async function openApp(page: Page, base: string) {
  const guard = createConsoleGuard(page, base);
  await installTelegramStub(page);
  await installFlatpickrCdn(page);

  // Root must 307-redirect to /miniapp (the Mini App entry point).
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.waitForURL("**/miniapp", { timeout: 15_000 });

  // Boot: fetch /me, apply theme, render the Today view (a calendar) — or an
  // explicit error state if something went wrong.
  await page
    .locator("#view .calendar, #view .state-error")
    .first()
    .waitFor({ state: "visible", timeout: 20_000 });

  // The loading spinner must be gone by now.
  await expect(page.locator("#view .state-loading")).toHaveCount(0);

  return guard;
}

/** Assert the 390px viewport has no horizontal overflow. */
export async function assertNoHorizontalOverflow(page: Page): Promise<void> {
  const { scrollWidth, innerWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  }));
  expect(
    scrollWidth,
    `horizontal overflow: scrollWidth ${scrollWidth} > innerWidth ${innerWidth}`,
  ).toBeLessThanOrEqual(innerWidth);
}

/** Assert a user-supplied string is rendered as text (not stringified DOM). */
export async function assertNoLeakedDom(page: Page, text: string): Promise<void> {
  const body = await page.locator("#app").innerText();
  expect(body, `expected "${text}" rendered as text`).toContain(text);
  expect(body, "no stringified DOM nodes allowed").not.toContain(
    "[object HTMLDivElement]",
  );
  expect(body).not.toContain("[object Object]");
  expect(body).not.toContain("undefined");
  expect(body).not.toContain("null");
}

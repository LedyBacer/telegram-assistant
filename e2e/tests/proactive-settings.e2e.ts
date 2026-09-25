import { test, expect, type Page } from "@playwright/test";

// V5.2 §4: proactive settings value rows — every row `await`s its PATCH
// inside the withButtonGuard closure, so while a (slow) PATCH is in flight
// the row stays DISABLED: a rapid second tap cannot start a second picker
// and exactly ONE PATCH is sent per interaction. After the response the row
// re-enables and shows the authoritative (server) value.
//
// Cleanup: the row's value is restored to its pre-test value in a finally.

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);
const ROW_LABEL = "Тихие часы с"; // miniapp.proactive_quiet_from (ru)

test("proactive settings row: one PATCH per tap, row disabled in flight, authoritative value", async ({
  page,
}: {
  page: Page;
}) => {
  const { openApp, goTab } = await import("../helpers/app");
  const guard = await openApp(page, base);

  await goTab(page, "settings");
  const view = page.locator("#view");
  const row = view.locator(".settings-row", { hasText: ROW_LABEL });
  await expect(row).toBeVisible();
  const original = (await row.locator(".settings-row-value").innerText()).trim();

  // Count and DELAY PATCHes so the in-flight window is observable.
  let patches = 0;
  await page.route("**/api/v1/proactive-settings", async (route) => {
    if (route.request().method() === "PATCH") {
      patches += 1;
      await new Promise((r) => setTimeout(r, 1200));
    }
    await route.continue();
  });

  // Open the time picker, set 09:30, close — the guarded closure then
  // `await`s the (delayed) PATCH.
  await row.click();
  const picker = page.locator(".picker-input");
  await picker.waitFor({ state: "visible" });
  await picker.evaluate((el: any) =>
    el._flatpickr.setDate(new Date(2026, 0, 1, 9, 30)),
  );
  await picker.focus();
  await page.keyboard.press("Escape");
  await expect(picker).toHaveCount(0);

  // While the PATCH is in flight: the row is disabled (the guard holds it
  // for the whole picker→PATCH sequence).
  await expect(row).toBeDisabled();

  // Rapid second tap: swallowed by the guard — no second picker, no second
  // request.
  await row.click({ force: true });
  await page.waitForTimeout(300);
  expect(patches).toBe(1);
  await expect(page.locator(".picker-input")).toHaveCount(0);

  // After the response: exactly ONE PATCH total, the row re-enabled, and the
  // displayed value is the authoritative one from the server.
  await expect(row).toBeEnabled();
  expect(patches).toBe(1);
  await expect(row.locator(".settings-row-value")).toHaveText("09:30");

  try {
    // Restore the pre-test value.
    await page.evaluate(async (value) => {
      const res = await fetch("/api/v1/proactive-settings", {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "X-Telegram-Init-Data": "e2e-init-data",
        },
        body: JSON.stringify({ quiet_hours_start: `${value}:00` }),
      });
      if (!res.ok) throw new Error(`restore failed: ${res.status}`);
    }, original);
  } finally {
    guard.assertClean();
  }
});

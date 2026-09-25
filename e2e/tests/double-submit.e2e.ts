import { test, expect } from "@playwright/test";
import { openApp, goTab } from "../helpers/app";

// V5 §17: a mutating button must not fire a duplicate request when tapped
// twice in quick succession. `withButtonGuard` disables the source button for
// the duration of the request, so a rapid second tap is dropped and exactly
// one POST reaches the API.

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);
const TITLE = `DS-${Date.now().toString(36)}`;

async function deleteItemsTitled(page: import("@playwright/test").Page, title: string) {
  const listRes = await page.request.get(`${base}/api/v1/items?limit=100`);
  if (!listRes.ok()) return;
  const items = (await listRes.json()) as Array<{ id: number; title: string }>;
  for (const it of items) {
    if (it.title === title) {
      const del = await page.request.delete(`${base}/api/v1/items/${it.id}`);
      expect([200, 204]).toContain(del.status());
    }
  }
}

test("double-tap on save creates exactly one item", async ({ page }) => {
  const guard = await openApp(page, base);
  try {
    await goTab(page, "new");

    let posts = 0;
    page.on("request", (req) => {
      if (req.method() === "POST" && req.url().includes("/api/v1/items")) {
        posts += 1;
      }
    });
    // Slow the create response so the button stays disabled across both taps.
    await page.route("**/api/v1/items", (route) => {
      if (route.request().method() === "POST") {
        return new Promise((res) => setTimeout(() => res(route.continue()), 600));
      }
      return route.continue();
    });

    await page.locator('input[aria-label="Новая задача / событие"]').fill(TITLE);
    const saveBtn = page.locator("#view .btn-primary", { hasText: "Сохранить" });
    await expect(saveBtn).toBeVisible();

    // First tap (a real click) plus a rapid second tap while the guard is
    // active. dispatchEvent bypasses Playwright's actionability wait so the
    // second tap lands even though the button is now disabled.
    await saveBtn.click();
    await saveBtn.dispatchEvent("click");
    // The guard leaves the button disabled/busy while the request is in flight.
    await expect(saveBtn).toBeDisabled();

    // The create lands and the app returns to Today.
    await expect(page.locator("#toast")).toContainText("Сохранено.");
    expect(posts).toBe(1);
  } finally {
    await deleteItemsTitled(page, TITLE);
    guard.assertClean();
  }
});

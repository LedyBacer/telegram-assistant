import { test, expect, type Page } from "@playwright/test";
import { openApp } from "../helpers/app";

// V5 §6.5: a calendar card whose source == "workout" must use the workout
// visual identity (workout icon) without changing the persisted title, and a
// plain task/event keeps its existing icon.
test.use({ timezoneId: "UTC" });

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);
const NAME = "P65 workout identity";

test("scheduled workout item renders the workout icon, title unchanged", async ({
  page,
}: {
  page: Page;
}) => {
  // The shared E2E user's timezone is restored to UTC in a finally, so this
  // test is safe to run in any file order (the app computes "today" in the
  // user's timezone).
  const tzRes = await page.request.patch(`${base}/api/v1/settings`, {
    data: { timezone: "UTC" },
  });
  expect(tzRes.status()).toBe(200);

  // Schedule a workout for today (UTC noon) so it lands on the Today view.
  const startsAt = new Date();
  startsAt.setUTCHours(12, 0, 0, 0);
  const schedRes = await page.request.post(`${base}/api/v1/workouts/schedule`, {
    data: { name: NAME, starts_at: startsAt.toISOString() },
  });
  expect(schedRes.status()).toBe(201);
  const item = (await schedRes.json()) as {
    id: number;
    title: string;
    source: string;
  };
  // Machine-readable marker, not a localized title prefix.
  expect(item.source).toBe("workout");
  expect(item.title).toBe(NAME);

  const guard = await openApp(page, base);
  try {
    // Today view lists the workout card with the workout visual identity.
    const card = page.locator("#view .card", { hasText: NAME }).first();
    await expect(card).toBeVisible();
    await expect(card.locator(".item-icon--workout")).toBeVisible();
    await expect(card.locator(".item-icon--workout")).toHaveText("🏋️");
    // The persisted title is shown verbatim (no "Workout:" prefix).
    await expect(card.locator(".item-title")).toHaveText(NAME);
    // A workout card must not carry the plain-task/event icon.
    expect(await card.locator(".item-icon:not(.item-icon--workout)").count()).toBe(0);
  } finally {
    guard.assertClean();
    // Clean up so the shared E2E database stays pristine for later specs.
    const del = await page.request.delete(`${base}/api/v1/items/${item.id}`);
    expect(del.status()).toBe(204);
    const restore = await page.request.patch(`${base}/api/v1/settings`, {
      data: { timezone: "UTC" },
    });
    expect(restore.status()).toBe(200);
  }
});

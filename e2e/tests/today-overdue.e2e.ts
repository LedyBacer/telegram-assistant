import { test, expect, type Page } from "@playwright/test";

// V5.2 §13: the Today summary's "Просрочено" chip counts only a hard
// deadline (due_at) in the past on SCHEDULED items:
//   A = event, starts yesterday, no due date  → not overdue (bare start is
//       not a deadline; it is not even anchored to today's list)
//   B = task, starts today, due yesterday     → overdue
//   C = task, starts today, due yesterday, completed → not counted
// Summary must read "Просрочено: 1".
//
// Cleanup: all three items are deleted via the API in a finally.

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);

const now = new Date();
const todayUTC = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
const at = (dayOffset: number, hour: number) =>
  new Date(todayUTC + dayOffset * 86_400_000 + hour * 3_600_000).toISOString();

async function api(path: string, method: string, body?: object) {
  const res = await fetch(base + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-Telegram-Init-Data": "e2e-init-data",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${method} ${path} → ${res.status}`);
  return res.json();
}

test("today summary counts a past due date as overdue, not a bare start", async ({
  page,
}: {
  page: Page;
}) => {
  const { openApp } = await import("../helpers/app");
  const guard = await openApp(page, base);

  const a: any = await api(
    "/api/v1/items",
    "POST",
    { title: "Overdue A event", kind: "event", starts_at: at(-1, 10), ends_at: at(0, 10) },
  );
  const b: any = await api(
    "/api/v1/items",
    "POST",
    { title: "Overdue B task", kind: "task", starts_at: at(0, 9), due_at: at(-1, 18) },
  );
  const c: any = await api(
    "/api/v1/items",
    "POST",
    { title: "Overdue C task", kind: "task", starts_at: at(0, 9), due_at: at(-1, 18) },
  );
  await api(`/api/v1/items/${c.id}/complete`, "POST");

  try {
    // Re-render Today (items were created outside the app).
    await page.locator('.nav-btn[data-tab="actions"]').click();
    await page.locator('.nav-btn[data-tab="today"]').click();
    const view = page.locator("#view");
    // A is anchored to yesterday (its start) — not in today's list.
    await expect(view.locator(".item-title", { hasText: "Overdue A event" })).toHaveCount(0);
    // B and C are anchored to today (their start) — both listed.
    await expect(view.locator(".item-title", { hasText: "Overdue B task" })).toBeVisible();
    await expect(view.locator(".item-title", { hasText: "Overdue C task" })).toBeVisible();
    // C is completed: it carries the completed badge.
    await expect(view.locator(".card", { hasText: "Overdue C task" }).first()).toContainText("Готово");

    // Exactly one overdue item (B).
    const chips = view.locator(".summary-chips");
    await expect(chips).toContainText("Просрочено: 1");
    await expect(chips).not.toContainText("Просрочено: 2");
  } finally {
    for (const id of [a.id, b.id, c.id]) {
      await api(`/api/v1/items/${id}`, "DELETE").catch(() => undefined);
    }
    guard.assertClean();
  }
});

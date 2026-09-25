import { test, expect } from "@playwright/test";

// V5.2 §10/§11: action inbox inspection. The list endpoints are mocked with
// EXACT query routes — `?status=actionable` (proposed + confirmed) feeds
// Pending, `?status=history` (executed + rejected + expired) feeds History —
// so the spec proves the client requests the right aggregate per filter and
// never shows terminal rows under Pending. Expired rows render a localized
// reason from the bounded `reason_code` (never raw `last_error` text), and
// a stale confirm (409 `{"detail": "action_stale"}`) surfaces the localized
// stale message, not the server detail.

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);
// Glob note: "?" is a single-character wildcard, so the query part is a "*".
const listRoute = "**/api/v1/actions*";

const ISO = "2026-09-24T12:00:00Z";
const LATER = "2027-09-24T13:00:00Z"; // far-future expiry: rows stay live

function base_action(overrides: Record<string, unknown>) {
  return {
    id: 0,
    kind: "create_item",
    summary: "",
    status: "proposed",
    payload: {},
    last_result: null,
    last_error: null,
    reason_code: null,
    created_at: ISO,
    expires_at: LATER,
    confirmed_at: null,
    rejected_at: null,
    executed_at: null,
    expired_at: null,
    ...overrides,
  };
}

const A_PROPOSED_ITEM = base_action({
  id: 1,
  summary: "Create task 'Ship report'",
  payload: { title: "Ship report", kind: "task" },
});
const A_CONFIRMED_ITEM = base_action({
  id: 2,
  kind: "create_reminder",
  summary: "Remind me 'Call the dentist'",
  status: "confirmed",
  payload: { fire_at: "2026-09-24T15:00:00Z", message: "Call the dentist" },
  confirmed_at: ISO,
});

const A_EXECUTED_WORKOUT = base_action({
  id: 3,
  kind: "log_workout",
  summary: "Log workout 'Morning run' 30 min effort 6",
  status: "executed",
  payload: { name: "Morning run", duration_minutes: 30, perceived_effort: 6 },
  last_result: { workout_id: 7, name: "Morning run" },
  confirmed_at: ISO,
  executed_at: ISO,
});
const A_REJECTED_ITEM = base_action({
  id: 4,
  kind: "update_item",
  summary: "Update item 'Old draft': due_at",
  status: "rejected",
  payload: { item_id: 42, due_at: "2026-09-25T09:00:00Z" },
  confirmed_at: ISO,
  rejected_at: ISO,
});
const A_EXPIRED_ITEM = base_action({
  id: 5,
  kind: "update_item",
  summary: "Update item 'Ghost': due_at",
  status: "expired",
  payload: { item_id: 43, due_at: "2026-09-25T09:00:00Z" },
  last_error: "item_missing",
  reason_code: "item_missing",
  confirmed_at: ISO,
  expired_at: ISO,
});

const ACTIONABLE = [A_PROPOSED_ITEM, A_CONFIRMED_ITEM];
const HISTORY = [A_EXECUTED_WORKOUT, A_REJECTED_ITEM, A_EXPIRED_ITEM];

test("action inbox: actionable vs history routes, localized reason, stale confirm", async ({
  page,
}) => {
  const { openApp } = await import("../helpers/app");
  const guard = await openApp(page, base);
  // The list routes are intentionally mocked; the 409 confirm is deliberate.
  guard.allow("/api/v1/actions");

  // Exact query routes: Pending -> actionable, History -> history.
  await page.route(listRoute, (route) => {
    const url = new URL(route.request().url());
    const status = url.searchParams.get("status");
    const body =
      status === "actionable"
        ? JSON.stringify(ACTIONABLE)
        : status === "history"
          ? JSON.stringify(HISTORY)
          : "[]";
    return route.fulfill({ status: 200, contentType: "application/json", body });
  });

  const view = page.locator("#view");
  await page.locator('.nav-btn[data-tab="actions"]').click();

  // --- Pending: exactly the two actionable rows -------------------------
  const pendingCards = view.locator(".list .card");
  await expect(pendingCards).toHaveCount(2);

  const createCard = view.locator(".card", { hasText: "Create task 'Ship report'" });
  await expect(createCard.locator(".badge")).toContainText("ожидает");
  await expect(createCard.locator(".item-actions .btn")).toHaveCount(2);
  await expect(createCard.locator(".item-error")).toHaveCount(0);

  // A CONFIRMED row lives under Pending too (it is still actionable) —
  // badge shared with proposed, and no confirm buttons (only proposed can
  // be confirmed from the UI).
  const reminderCard = view.locator(".card", { hasText: "Remind me 'Call the dentist'" });
  await expect(reminderCard.locator(".badge")).toContainText("ожидает");
  await expect(reminderCard.locator(".item-actions .btn")).toHaveCount(0);

  // NO history cards in Pending.
  await expect(view.locator(".card", { hasText: "Morning run" })).toHaveCount(0);
  await expect(view.locator(".card", { hasText: "Old draft" })).toHaveCount(0);
  await expect(view.locator(".card", { hasText: "Ghost" })).toHaveCount(0);

  // --- History: executed + rejected + expired ----------------------------
  await view.locator(".btn", { hasText: "История" }).click();
  const historyCards = view.locator(".list .card");
  await expect(historyCards).toHaveCount(3);
  await expect(view.locator(".card", { hasText: "Morning run" }).locator(".badge"))
    .toContainText("выполнено");
  await expect(view.locator(".card", { hasText: "Old draft" }).locator(".badge"))
    .toContainText("отклонено");

  // Expired: localized reason from the bounded code — the localized
  // item_missing string is shown and the raw code is not leaked.
  const expiredCard = view.locator(".card", { hasText: "Ghost" });
  await expect(expiredCard.locator(".badge")).toContainText("истекло");
  await expect(expiredCard.locator(".item-desc")).toContainText("Цель: запись №43");
  await expect(expiredCard.locator(".item-error")).toContainText(
    "Запись больше не существует.",
  );
  await expect(expiredCard.locator(".item-actions .btn")).toHaveCount(0);
  // The raw bounded code never leaks into the UI.
  expect(await page.locator("#app").innerText()).not.toContain("item_missing");

  // --- Stale confirm: 409 action_stale -> localized toast -----------------
  await view.locator(".btn", { hasText: "Ожидают" }).click();
  await page.locator(".list .card", { hasText: "Ship report" }).waitFor();
  await page.route("**/api/v1/actions/1/confirm", (route) =>
    route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ detail: "action_stale" }),
    }),
  );
  await page
    .locator(".card", { hasText: "Ship report" })
    .locator(".item-actions .btn", { hasText: "Подтвердить" })
    .click();
  // The localized stale message; the raw server detail is never rendered.
  await expect(page.locator("#toast")).toContainText(
    "Это действие больше не актуально.",
  );
  expect(await page.locator("#app").innerText()).not.toContain("action_stale");

  guard.assertClean();
});

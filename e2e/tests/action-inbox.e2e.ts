import { test, expect } from "@playwright/test";
import { openApp, assertNoLeakedDom } from "../helpers/app";

// V3 P33: assistant action inbox inspection. GET /api/v1/actions is mocked
// with exact ActionOut shapes (the live inbox flow — propose/confirm/409/
// reject against real PostgreSQL — is covered by v2-features.e2e.ts).
// This spec covers the inspection surface: kind icon, typed preview,
// payload-derived target info, expired reason (last_error), and the
// stale-confirm toast that surfaces the server reason.

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);
// Glob note: "?" is a single-character wildcard, so the query part is a "*".
const listRoute = "**/api/v1/actions?limit=*";

const ISO = "2026-09-24T12:00:00Z";
const LATER = "2026-09-24T13:00:00Z";

const A_PROPOSED_ITEM = {
  id: 1,
  kind: "create_item",
  summary: "Create task 'Ship report' at 2026-09-24 10:00 due 2026-09-24 18:00",
  status: "proposed",
  payload: {
    title: "Ship report",
    kind: "task",
    starts_at: "2026-09-24T10:00:00Z",
    due_at: "2026-09-24T18:00:00Z",
  },
  last_result: null,
  last_error: null,
  created_at: ISO,
  expires_at: LATER,
  confirmed_at: null,
  rejected_at: null,
  executed_at: null,
  expired_at: null,
};

const A_PROPOSED_REMINDER = {
  id: 2,
  kind: "create_reminder",
  summary: "Remind me 'Call the dentist' at 2026-09-24 15:00",
  status: "proposed",
  payload: {
    fire_at: "2026-09-24T15:00:00Z",
    message: "Call the dentist",
  },
  last_result: null,
  last_error: null,
  created_at: ISO,
  expires_at: LATER,
  confirmed_at: null,
  rejected_at: null,
  executed_at: null,
  expired_at: null,
};

const A_EXPIRED_ITEM = {
  id: 3,
  kind: "update_item",
  summary: "Update item 'Ghost': due_at",
  status: "expired",
  payload: { item_id: 42, due_at: "2026-09-25T09:00:00Z" },
  last_result: null,
  last_error: "calendar item no longer exists",
  created_at: ISO,
  expires_at: LATER,
  confirmed_at: ISO,
  rejected_at: null,
  executed_at: null,
  expired_at: ISO,
};

const A_EXECUTED_WORKOUT = {
  id: 4,
  kind: "log_workout",
  summary: "Log workout 'Morning run' 30 min effort 6",
  status: "executed",
  payload: { name: "Morning run", duration_minutes: 30, perceived_effort: 6 },
  last_result: { workout_id: 7, name: "Morning run" },
  last_error: null,
  created_at: ISO,
  expires_at: LATER,
  confirmed_at: ISO,
  rejected_at: null,
  executed_at: ISO,
  expired_at: null,
};

const LIST = [A_PROPOSED_ITEM, A_PROPOSED_REMINDER, A_EXPIRED_ITEM, A_EXECUTED_WORKOUT];

const STALE_DETAIL = "calendar item no longer exists";

test("actions inbox: kind icon, typed preview, target info, expired reason, stale confirm", async ({
  page,
}) => {
  const guard = await openApp(page, base);
  guard.allow("/api/v1/actions");
  // Mock before the tab click: the view fetches the list on render.
  await page.route(listRoute, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(LIST) }),
  );
  await page.locator('.nav-btn[data-tab="actions"]').click();
  const view = page.locator("#view");

  // One card per action, newest order preserved by the mocked list.
  const cards = view.locator(".list .card");
  await expect(cards).toHaveCount(4);

  // 1. Proposed create_item: kind icon, typed preview, and NO target hint
  //    (create payloads have no entity id).
  const createCard = view.locator(".card", { hasText: "Create task 'Ship report'" });
  await expect(createCard.locator(".item-icon")).toContainText("✅");
  await expect(createCard.locator(".badge")).toContainText("ожидает");
  await expect(createCard.locator(".item-desc")).toHaveCount(0);
  await expect(createCard.locator(".item-error")).toHaveCount(0);

  // 2. Proposed create_reminder: reminder icon, no target hint either.
  const reminderCard = view.locator(".card", { hasText: "Remind me 'Call the dentist'" });
  await expect(reminderCard.locator(".item-icon")).toContainText("⏰");

  // 3. Expired update_item: the payload's target entity is shown, and the
  //    stale reason (last_error) is surfaced on the card.
  const expiredCard = view.locator(".card", { hasText: "Update item 'Ghost'" });
  await expect(expiredCard.locator(".badge")).toContainText("истекло");
  await expect(expiredCard.locator(".item-desc")).toContainText("Цель: запись №42");
  await expect(expiredCard.locator(".item-error")).toContainText(STALE_DETAIL);
  await expect(expiredCard.locator(".item-actions .btn")).toHaveCount(0);
  await assertNoLeakedDom(page, "calendar item no longer exists");

  // 4. Executed log_workout: workout icon + done badge, no buttons.
  const workoutCard = view.locator(".card", { hasText: "Log workout 'Morning run'" });
  await expect(workoutCard.locator(".item-icon")).toContainText("🏋️");
  await expect(workoutCard.locator(".badge")).toContainText("выполнено");
  await expect(workoutCard.locator(".item-actions .btn")).toHaveCount(0);

  // Only the two proposed cards carry confirm/reject buttons.
  await expect(createCard.locator(".item-actions .btn")).toHaveCount(2);
  await expect(reminderCard.locator(".item-actions .btn")).toHaveCount(2);

  // 5. Stale confirm: the confirm POST answers 409 (server expired the
  //    action), its detail is shown in the toast, and the view re-renders
  //    from the next (empty) list.
  await page.route("**/api/v1/actions/1/confirm", (route) =>
    route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ detail: STALE_DETAIL }),
    }),
  );
  await page.route(listRoute, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
  const confirmBtn = createCard.locator(".item-actions .btn", { hasText: "Подтвердить" });
  await confirmBtn.click();
  await expect(page.locator("#toast")).toContainText(
    `Действие неактуально: ${STALE_DETAIL}`,
  );
  await expect(view.locator(".state-empty")).toContainText("Нет ожидающих действий");

  guard.assertClean();
});

import { test, expect } from "@playwright/test";

// V3 P30: task/event edit flow. The item card must show the end time and an
// "Изменить" action; the edit view prefills every field from GET /items,
// submits ONLY the changed keys through the tri-state PATCH (explicit null
// clears, omitted leaves as-is), shows the item's pending reminders with an
// inline cancel, and leaves `kind` display-only.
//
// The shared test user's timezone is UTC and the clock is frozen, so all
// naive wall values in this test are exact UTC instants. No user settings
// are modified, so nothing needs restoring.

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);
const FROZEN_NOW = "2026-09-24T12:00:00.000Z";
const run = Date.now().toString(36);
const OLD_TITLE = `P30-${run}`;
const NEW_TITLE = `P30-${run}-edited`;

// Clean up the seeded item (its reminder cascades) so later specs see a
// calendar without this test's 26th-day event.
let itemId: number | null = null;
test.afterEach(async ({ request }) => {
  if (itemId != null) {
    const del = await request.delete(`${base}/api/v1/items/${itemId}`);
    expect([200, 204]).toContain(del.status());
  }
});

interface ItemOut {
  id: number;
  kind: string;
  title: string;
  description: string | null;
  starts_at: string | null;
  ends_at: string | null;
  due_at: string | null;
  status: string;
  priority: string;
}

test("edit view: change + clear via tri-state PATCH, reminder cancel, ends_at on card", async ({
  page,
}) => {
  await page.clock.install({ time: new Date(FROZEN_NOW) });

  // Seed: a scheduled task on 2026-09-26 with start/end/due and one reminder
  // 30 minutes before the start (fires 09:30 UTC).
  const createRes = await page.request.post(`${base}/api/v1/items`, {
    data: {
      title: OLD_TITLE,
      kind: "task",
      description: "seed description",
      starts_at: "2026-09-26T10:00:00",
      ends_at: "2026-09-26T12:00:00",
      due_at: "2026-09-26T18:00:00",
      priority: "normal",
      remind_offsets_minutes: [30],
    },
  });
  expect(createRes.status()).toBe(201);
  const item = (await createRes.json()) as ItemOut;
  itemId = item.id;

  const { openApp } = await import("../helpers/app");
  const guard = await openApp(page, base);
  await page.locator('.nav-btn[data-tab="upcoming"]').click();
  const card = page.locator("#view .card", { hasText: OLD_TITLE }).first();
  await card.waitFor({ timeout: 15_000 });

  // The card meta now shows the end time.
  await expect(card.locator(".item-meta")).toContainText("Конец 26 сент., 12:00");
  await expect(card.locator(".item-meta")).toContainText("Начало 26 сент., 10:00");

  // Open the edit view from the card.
  await card.locator(".item-actions .btn", { hasText: "Изменить" }).click();
  const view = page.locator("#view");
  await expect(view.locator("h2.view-title")).toHaveText(
    "Изменить задачу / событие",
  );
  const titleInput = view.locator('input[aria-label="Новая задача / событие"]');
  await expect(titleInput).toHaveValue(OLD_TITLE);
  // kind is display-only: the value is shown with no picker affordance.
  await expect(view.locator(".field", { hasText: "Тип" })).toContainText("Задача");

  // The pending item reminder is listed with its fire time and offset.
  const reminderRow = view.locator(".reminder-row").first();
  await expect(reminderRow).toContainText("26 сент., 09:30");
  await expect(reminderRow).toContainText("за 30 мин до начала");

  // Change the title and the priority.
  await titleInput.fill(NEW_TITLE);
  const priorityBtn = view.locator(".picker-field", { hasText: "Обычный" }).first();
  await priorityBtn.click();
  await page.locator(".sheet-row", { hasText: "Высокий" }).click();
  // Re-locate: the button's label changed, so the old hasText filter is stale.
  await expect(view.locator(".picker-field", { hasText: "Высокий" }).first())
    .toBeVisible();

  // Clear "Конец" with the inline clear button.
  const endsRow = view.locator(".when-field", { hasText: "Конец" });
  await endsRow.locator(".when-clear").click();
  await expect(endsRow.locator(".picker-field")).toContainText("Не задано");

  // The untouched "Срок" row is pre-filled with its original wall time.
  const dueRow = view.locator(".when-field", { hasText: "Срок" });
  await expect(dueRow.locator(".picker-field")).toContainText("26 сент., 18:00");

  // Save: the card returns with the new title, priority badge, no end time.
  await view.locator(".btn-primary", { hasText: "Сохранить" }).click();
  await expect(page.locator("#toast")).toContainText("Сохранено.");
  const newCard = page.locator("#view .card", { hasText: NEW_TITLE }).first();
  await expect(newCard).toBeVisible();
  await expect(newCard.locator(".item-meta")).toContainText("Начало 26 сент., 10:00");
  await expect(newCard.locator(".item-meta")).not.toContainText("Конец");
  await expect(newCard.locator(".item-head .badge")).toHaveText("Высокий");

  // Ground truth: tri-state PATCH changed only what the form touched.
  const getRes = await page.request.get(`${base}/api/v1/items/${item.id}`);
  expect(getRes.status()).toBe(200);
  const after = (await getRes.json()) as ItemOut;
  expect(after.title).toBe(NEW_TITLE);
  expect(after.priority).toBe("high");
  expect(after.ends_at).toBeNull();
  expect(after.starts_at).toBe("2026-09-26T10:00:00Z");
  expect(after.due_at).toBe("2026-09-26T18:00:00Z");
  expect(after.description).toBe("seed description");
  expect(after.kind).toBe("task");

  // Reload: the edited state survives.
  await page.reload({ waitUntil: "domcontentloaded" });
  await page
    .locator("#view .calendar, #view .state-error")
    .first()
    .waitFor({ state: "visible", timeout: 20_000 });
  await page.locator('.nav-btn[data-tab="upcoming"]').click();
  const reloadedCard = page.locator("#view .card", { hasText: NEW_TITLE }).first();
  await expect(reloadedCard.locator(".item-meta")).not.toContainText("Конец");
  await expect(reloadedCard.locator(".item-head .badge")).toHaveText("Высокий");

  // Cancel the pending reminder from the edit view.
  await reloadedCard.locator(".item-actions .btn", { hasText: "Изменить" }).click();
  const cancelRow = page.locator("#view .reminder-row").first();
  await cancelRow.locator(".btn", { hasText: "Отменить" }).click();
  await expect(page.locator("#toast")).toContainText("Напоминание отменено.");
  await expect(page.locator("#view .reminder-none")).toHaveCount(1);

  const remindRes = await page.request.get(
    `${base}/api/v1/reminders?item_id=${item.id}`,
  );
  expect(remindRes.status()).toBe(200);
  const reminders = (await remindRes.json()) as Array<{ status: string }>;
  expect(reminders).toHaveLength(1);
  expect(reminders[0].status).toBe("cancelled");

  guard.assertClean();
});

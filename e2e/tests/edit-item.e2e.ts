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

// The upcoming view is server-clock driven (anchor >= now), so the seeded item
// must live inside the 7-day window from the SERVER's real clock — a hard-coded
// date rots the moment "now" passes it (V5.4 time-bomb fix). Anchor it to real
// now + 1 day (UTC = the test user's tz) and derive every expected display
// string with the same Intl ru-RU/UTC format the app's fmtDT/fmtWall use.
const SEED = new Date(Date.now() + 24 * 3600 * 1000);
const SEED_DATE = SEED.toISOString().slice(0, 10); // YYYY-MM-DD (UTC)
const SEED_START = new Date(`${SEED_DATE}T10:00:00Z`);
const SEED_END = new Date(`${SEED_DATE}T12:00:00Z`);
const SEED_DUE = new Date(`${SEED_DATE}T18:00:00Z`);
const SEED_FIRE30 = new Date(SEED_START.getTime() - 30 * 60 * 1000);
const fmt = (d: Date) =>
  new Intl.DateTimeFormat("ru-RU", {
    timeZone: "UTC",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(d);

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

  // Seed: a scheduled task on SEED_DATE (real now + 1 day, UTC) with
  // start/end/due and one reminder 30 minutes before the start.
  const createRes = await page.request.post(`${base}/api/v1/items`, {
    data: {
      title: OLD_TITLE,
      kind: "task",
      description: "seed description",
      starts_at: `${SEED_DATE}T10:00:00`,
      ends_at: `${SEED_DATE}T12:00:00`,
      due_at: `${SEED_DATE}T18:00:00`,
      priority: "normal",
      remind_offsets_minutes: [30],
    },
  });
  expect(createRes.status()).toBe(201);
  const item = (await createRes.json()) as ItemOut;
  itemId = item.id;

  const { openApp, goTab } = await import("../helpers/app");
  const guard = await openApp(page, base);
  await goTab(page, "upcoming");
  const card = page.locator("#view .card", { hasText: OLD_TITLE }).first();
  await card.waitFor({ timeout: 15_000 });

  // The card meta now shows the end time.
  await expect(card.locator(".item-meta")).toContainText(`Конец ${fmt(SEED_END)}`);
  await expect(card.locator(".item-meta")).toContainText(`Начало ${fmt(SEED_START)}`);

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
  await expect(reminderRow).toContainText(fmt(SEED_FIRE30));
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
  await expect(dueRow.locator(".picker-field")).toContainText(fmt(SEED_DUE));

  // Save: the card returns with the new title, priority badge, no end time.
  await view.locator(".btn-primary", { hasText: "Сохранить" }).click();
  await expect(page.locator("#toast")).toContainText("Сохранено.");
  const newCard = page.locator("#view .card", { hasText: NEW_TITLE }).first();
  await expect(newCard).toBeVisible();
  await expect(newCard.locator(".item-meta")).toContainText(`Начало ${fmt(SEED_START)}`);
  await expect(newCard.locator(".item-meta")).not.toContainText("Конец");
  await expect(newCard.locator(".item-head .badge")).toHaveText("Высокий");

  // Ground truth: tri-state PATCH changed only what the form touched.
  const getRes = await page.request.get(`${base}/api/v1/items/${item.id}`);
  expect(getRes.status()).toBe(200);
  const after = (await getRes.json()) as ItemOut;
  expect(after.title).toBe(NEW_TITLE);
  expect(after.priority).toBe("high");
  expect(after.ends_at).toBeNull();
  expect(after.starts_at).toBe(`${SEED_DATE}T10:00:00Z`);
  expect(after.due_at).toBe(`${SEED_DATE}T18:00:00Z`);
  expect(after.description).toBe("seed description");
  expect(after.kind).toBe("task");

  // Reload: the edited state survives.
  await page.reload({ waitUntil: "domcontentloaded" });
  await page
    .locator("#view .calendar, #view .state-error")
    .first()
    .waitFor({ state: "visible", timeout: 20_000 });
  await goTab(page, "upcoming");
  const reloadedCard = page.locator("#view .card", { hasText: NEW_TITLE }).first();
  await expect(reloadedCard.locator(".item-meta")).not.toContainText("Конец");
  await expect(reloadedCard.locator(".item-head .badge")).toHaveText("Высокий");

  // Reopen the edit view: add a "10 min" reminder through the shared picker
  // (V4 §28), then cancel both pending reminders.
  await reloadedCard.locator(".item-actions .btn", { hasText: "Изменить" }).click();
  await view.locator(".chip", { hasText: "за 10 мин" }).click();
  await view.locator(".btn", { hasText: "Сохранить напоминания" }).click();
  await expect(page.locator("#toast")).toContainText("Напоминания сохранены.");
  // The new pending reminder (09:50 = 10:00 - 10 min) is listed.
  await expect(view.locator(".reminder-row")).toHaveCount(2);
  await expect(view.locator(".reminder-row", { hasText: "09:50" })).toContainText(
    "за 10 мин",
  );

  // Cancel both pending reminders from the edit view.
  const rows = view.locator(".reminder-row");
  await rows.nth(0).locator(".btn", { hasText: "Отменить" }).click();
  await expect(page.locator("#toast")).toContainText("Напоминание отменено.");
  await rows.nth(0).locator(".btn", { hasText: "Отменить" }).click();
  await expect(view.locator(".reminder-none")).toHaveCount(1);

  const remindRes = await page.request.get(
    `${base}/api/v1/reminders?item_id=${item.id}`,
  );
  expect(remindRes.status()).toBe(200);
  const reminders = (await remindRes.json()) as Array<{ status: string }>;
  expect(reminders).toHaveLength(2);
  expect(reminders.every((r) => r.status === "cancelled")).toBe(true);

  guard.assertClean();
});

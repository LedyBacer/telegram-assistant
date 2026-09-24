import { test, expect } from "@playwright/test";
import { openApp } from "../helpers/app";

// V3 P31: reminder-offset controls. Comma-separated text entry is gone; the
// New screen offers preset chips (multi-select within the backend limit of 5)
// plus a bounded custom input. The item created here (with 5 reminders) is
// deleted in afterEach so other specs see a clean calendar.

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);
const run = Date.now().toString(36);
const title = `P31-${run}: ship with reminders`;

let itemId: number | null = null;
test.afterEach(async ({ request }) => {
  if (itemId != null) {
    const del = await request.delete(`${base}/api/v1/items/${itemId}`);
    expect([200, 204]).toContain(del.status());
  }
});

test("reminder presets: multi-select, limit, custom, save", async ({ page }) => {
  const guard = await openApp(page, base);
  await page.locator('.nav-btn[data-tab="new"]').click();
  const view = page.locator("#view");

  // The five preset chips are present; no comma-separated text entry remains.
  const chip = (label: string) =>
    view.locator(".remind-chips .chip", { hasText: label });
  for (const label of ["в начале", "за 10 мин", "за 30 мин", "за час", "за день"]) {
    await expect(chip(label)).toBeVisible();
  }
  await expect(view.locator(".remind-custom-row .remind-custom")).toBeVisible();

  // Toggle three presets on.
  await chip("в начале").click();
  await chip("за 10 мин").click();
  await chip("за 30 мин").click();
  await expect(chip("в начале")).toHaveClass(/is-on/);
  await expect(chip("за 10 мин")).toHaveClass(/is-on/);
  await expect(chip("за 30 мин")).toHaveClass(/is-on/);

  // Out-of-bounds custom value is rejected with the range message.
  const custom = view.locator(".remind-custom-row .remind-custom");
  await custom.fill("2000");
  await view.locator(".remind-custom-row .btn", { hasText: "Добавить" }).click();
  await expect(page.locator("#toast")).toContainText("Допустимо 0–1440 минут");

  // A valid custom value becomes a removable chip.
  await custom.fill("45");
  await view.locator(".remind-custom-row .btn", { hasText: "Добавить" }).click();
  await expect(chip("45 мин")).toHaveClass(/is-on/);
  await expect(custom).toHaveValue("");

  // Fifth selection fills the item to the backend limit.
  await chip("за час").click();
  // At the limit, unselected presets are disabled; selected chips stay
  // tappable so the user can free a slot.
  for (const label of ["в начале", "за 10 мин", "за 30 мин", "за час"]) {
    await expect(chip(label)).toBeEnabled();
  }
  await expect(chip("за день")).toBeDisabled();

  // A sixth custom value is rejected with the limit message.
  await custom.fill("20");
  await view.locator(".remind-custom-row .btn", { hasText: "Добавить" }).click();
  await expect(page.locator("#toast")).toContainText("Не более 5 напоминаний");

  // Deselecting one frees a slot for another preset.
  await chip("в начале").click();
  await expect(chip("в начале")).not.toHaveClass(/is-on/);
  await expect(chip("за день")).toBeEnabled();
  await chip("за день").click();
  await expect(chip("за день")).toHaveClass(/is-on/);

  // Save: give the item a start date (reminders anchor to it).
  await view.locator(".card input.field-input").first().fill(title);
  await view.locator(".picker-field").nth(2).click(); // starts
  await page.locator(".flatpickr-calendar").waitFor({ state: "visible" });
  await page.locator(".flatpickr-calendar .today").click();
  await page.keyboard.press("Escape");
  await view.locator(".btn-primary", { hasText: "Сохранить" }).click();
  await expect(page.locator("#toast")).toContainText("Сохранено.");

  // Ground truth: exactly the five selected offsets were persisted.
  const listRes = await page.request.get(
    `${base}/api/v1/items?start=2026-01-01T00:00:00Z&end=2027-01-01T00:00:00Z`,
  );
  const items = (await listRes.json()) as Array<{ id: number; title: string }>;
  const mine = items.find((i) => i.title === title);
  expect(mine, "created item is listed").toBeTruthy();
  itemId = mine!.id;
  const remindRes = await page.request.get(
    `${base}/api/v1/reminders?item_id=${mine!.id}&status=pending`,
  );
  expect(remindRes.status()).toBe(200);
  const reminders = (await remindRes.json()) as Array<{ offset_minutes: number }>;
  expect(reminders.map((r) => r.offset_minutes).sort((a, b) => a - b)).toEqual([
    10, 30, 45, 60, 1440,
  ]);

  guard.assertClean();
});

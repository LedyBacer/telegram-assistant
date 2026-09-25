import { test, expect } from "@playwright/test";

// V5 §15: leaving a dirty New/Edit form must ask for a discard confirmation.
// No data is created here (the form is never submitted), so there is nothing
// to clean up.

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);

test("dirty New form: tab switch asks to discard, cancel keeps it, confirm leaves", async ({
  page,
}) => {
  const { openApp } = await import("../helpers/app");
  const guard = await openApp(page, base);

  // Open the New view and type a title — that marks the form dirty.
  await page.locator('.nav-btn[data-tab="new"]').click();
  const view = page.locator("#view");
  await expect(view.locator("h2.view-title")).toHaveText("Новая задача / событие");
  const titleInput = view.locator('input[aria-label="Новая задача / событие"]');
  await titleInput.fill("Черновик без сохранения");

  // Switch to Today: a discard confirmation must appear.
  await page.locator('.nav-btn[data-tab="today"]').click();
  const dialog = page.locator('[role="alertdialog"]');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("Несохранённые изменения");

  // Cancel: stay on the New view, the typed title survives.
  await dialog.locator(".btn", { hasText: "Отменить" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(view.locator("h2.view-title")).toHaveText("Новая задача / событие");
  await expect(titleInput).toHaveValue("Черновик без сохранения");

  // Try to leave again, this time confirm: the app navigates to Today.
  await page.locator('.nav-btn[data-tab="today"]').click();
  await expect(dialog).toBeVisible();
  await dialog.locator(".btn", { hasText: "Покинуть" }).click();
  await expect(page.locator("#view .calendar")).toBeVisible();

  guard.assertClean();
});

// V5.2 §7: the native closing confirmation (Telegram SDK) tracks the form's
// dirty state — asserted through window.__tg.closingConfirmation() (the real
// SDK surface), NOT the browser beforeunload fallback.
test("closing confirmation tracks New form dirty state (type, cancel, discard)", async ({
  page,
}) => {
  const { openApp, goTab } = await import("../helpers/app");
  const guard = await openApp(page, base);
  const cc = () =>
    page.evaluate(() => (window as any).__tg.closingConfirmation().enabled);

  // Clean boot: no confirmation.
  expect(await cc()).toBe(false);

  await goTab(page, "new");
  const view = page.locator("#view");
  const titleInput = view.locator('input[aria-label="Новая задача / событие"]');
  await titleInput.fill("A"); // one character: form is now dirty
  await expect.poll(cc).toBe(true);

  // Navigation attempt + Cancel: stays dirty, confirmation stays enabled.
  await page.locator('.nav-btn[data-tab="today"]').click();
  const dialog = page.locator('[role="alertdialog"]');
  await expect(dialog).toBeVisible();
  await dialog.locator(".btn", { hasText: "Отменить" }).click();
  await expect(dialog).toHaveCount(0);
  await expect.poll(cc).toBe(true);

  // Navigation attempt + discard: form reset, confirmation disabled.
  await page.locator('.nav-btn[data-tab="today"]').click();
  await expect(dialog).toBeVisible();
  await dialog.locator(".btn", { hasText: "Покинуть" }).click();
  await expect(page.locator("#view .calendar")).toBeVisible();
  await expect.poll(cc).toBe(false);

  guard.assertClean();
});

// V5.2 §7: the Edit view's confirmation lifecycle — dirty on change,
// disabled after a SUCCESSFUL save. The created item is deleted in finally.
test("closing confirmation: Edit form dirty → enabled, successful save → disabled", async ({
  page,
}) => {
  const { openApp, goTab } = await import("../helpers/app");
  const guard = await openApp(page, base);
  const cc = () =>
    page.evaluate(() => (window as any).__tg.closingConfirmation().enabled);
  expect(await cc()).toBe(false);

  // Create a scheduled item so it shows up on Today and is editable.
  // Picker field DOM order: kind(0), priority(1), starts(2), end(3), due(4).
  await goTab(page, "new");
  const view = page.locator("#view");
  await view.locator('input[aria-label="Новая задача / событие"]').fill("Dirty edit target");
  const startPicker = view.locator(".picker-field").nth(2);
  await startPicker.click();
  await page.locator(".flatpickr-calendar").waitFor({ state: "visible" });
  await page.locator(".flatpickr-calendar .today").click();
  await page.locator(".picker-input").focus();
  await page.keyboard.press("Escape");
  await view.locator(".btn-primary", { hasText: "Сохранить" }).click();
  await expect(page.locator("#view .item-title", { hasText: "Dirty edit target" })).toBeVisible();

  // Open Edit: the clean form does NOT ask for confirmation.
  await view
    .locator(".card", { hasText: "Dirty edit target" })
    .locator(".item-actions .btn", { hasText: "Изменить" })
    .click();
  // The Edit view reuses the miniapp.new_title aria-label ("Новая задача /
  // событие") for its title input.
  const editTitle = view.locator('input[aria-label="Новая задача / событие"]');
  await expect(editTitle).toHaveValue("Dirty edit target");
  await expect.poll(cc).toBe(false);

  // Change the title: dirty → enabled.
  await editTitle.fill("Dirty edit changed");
  await expect.poll(cc).toBe(true);

  // Successful save → back on Today, confirmation disabled.
  await view.locator(".btn-primary", { hasText: "Сохранить" }).click();
  await expect(page.locator("#toast")).toContainText("Сохранено.");
  await expect(page.locator("#view .item-title", { hasText: "Dirty edit changed" })).toBeVisible();
  await expect.poll(cc).toBe(false);

  // Cleanup: delete the item.
  const card = view.locator(".card", { hasText: "Dirty edit changed" }).first();
  await card.locator(".item-actions .btn").last().click();
  await page.locator("#sheet-root .sheet-confirm .btn-danger").click();
  await expect(page.locator("#view .item-title", { hasText: "Dirty edit changed" })).toHaveCount(0);

  guard.assertClean();
});

// V5.2 §7: a FAILED save (500) must keep the confirmation enabled — the
// unsaved changes are still at risk. No DB rows are created.
test("closing confirmation: failed save (500) keeps it enabled", async ({
  page,
}) => {
  const { openApp, goTab } = await import("../helpers/app");
  const guard = await openApp(page, base);
  const cc = () =>
    page.evaluate(() => (window as any).__tg.closingConfirmation().enabled);
  guard.allow("/api/v1/items");

  await page.route("**/api/v1/items", (route) =>
    route.request().method() === "POST"
      ? route.fulfill({ status: 500, contentType: "application/json", body: "{}" })
      : route.continue(),
  );

  await goTab(page, "new");
  const view = page.locator("#view");
  await view.locator('input[aria-label="Новая задача / событие"]').fill("Unsaved failure");
  await expect.poll(cc).toBe(true);

  // Save fails with 500 — the generic error toast, and the confirmation
  // stays ON (the form is still dirty).
  await view.locator(".btn-primary", { hasText: "Сохранить" }).click();
  await expect(page.locator("#toast")).toContainText("Что-то пошло не так");
  await expect.poll(cc).toBe(true);

  guard.assertClean();
});

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

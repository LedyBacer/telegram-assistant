import { test, expect } from "@playwright/test";
import { openApp, goTab } from "../helpers/app";

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);

/**
 * §22: Shared modal lifecycle — scroll lock, focus trap, Escape, focus
 * return, and no double sheets.
 */
test("modal lifecycle: scroll lock, focus trap, escape, focus return, no double sheets", async ({
  page,
}) => {
  const guard = await openApp(page, base);

  // Navigate to the New view where a picker field triggers a sheet.
  await goTab(page, "new");
  const picker = page.locator("#view .picker-field").nth(1);
  await picker.click();

  // --- 1. Scroll lock ---------------------------------------------------
  // While the sheet is open, body has overflow:hidden + position:fixed.
  await page.locator("#sheet-root .sheet").waitFor();
  const bodyStyles = await page.evaluate(() => ({
    overflow: document.body.style.overflow,
    position: document.body.style.position,
  }));
  expect(bodyStyles.overflow).toBe("hidden");
  expect(bodyStyles.position).toBe("fixed");

  // --- 2. Focus is inside the sheet --------------------------------------
  const focusInSheet = await page.evaluate(() =>
    document.querySelector("#sheet-root .sheet")?.contains(document.activeElement),
  );
  expect(focusInSheet).toBe(true);

  // --- 3. Focus trap: Tab cycles within the sheet ------------------------
  // Press Tab repeatedly; focus must never leave the sheet.
  for (let i = 0; i < 5; i++) {
    await page.keyboard.press("Tab");
    const stillIn = await page.evaluate(() =>
      document.querySelector("#sheet-root .sheet")?.contains(document.activeElement),
    );
    expect(stillIn, `Tab ${i + 1}: focus escaped the sheet`).toBe(true);
  }

  // --- 4. No double sheets ------------------------------------------------
  // While the first sheet is open, a second openSheet call resolves null
  // immediately (no scrim stacking). We simulate by trying to open the
  // "More" sheet while the picker sheet is still visible.
  await page.locator(".bottomnav .nav-btn[data-tab='more']").click({ force: true });
  // Only one scrim should exist in the DOM.
  await expect(page.locator("#sheet-root .sheet-scrim")).toHaveCount(1);

  // --- 5. Escape closes the sheet ----------------------------------------
  await page.keyboard.press("Escape");
  await expect(page.locator("#sheet-root .sheet")).toHaveCount(0);

  // --- 6. Scroll lock released -------------------------------------------
  const bodyAfter = await page.evaluate(() => ({
    overflow: document.body.style.overflow,
    position: document.body.style.position,
  }));
  expect(bodyAfter.overflow).toBe("");
  expect(bodyAfter.position).toBe("");

  // --- 7. Focus return: after closing, focus is back on the trigger ------
  // The picker field should have focus again (it was the last focused element
  // before the sheet opened).
  const focusBack = await page.evaluate(() => {
    const active = document.activeElement;
    return active?.classList.contains("picker-field") || active?.classList.contains("picker-input");
  });
  expect(focusBack, "focus returned to the picker trigger").toBe(true);

  // --- 8. Confirm dialog: focus trap + scroll lock ------------------------
  // Create an item with a start time so it appears in the Today view.
  // The New view picker order: kind(0), priority(1), starts(2), due(3), end(4).
  const startPicker = page.locator("#view .picker-field").nth(2);
  await startPicker.click();
  await page.locator(".flatpickr-calendar").waitFor({ state: "visible" });
  await page.locator(".flatpickr-calendar .today").click();
  await page.locator(".picker-input").focus();
  await page.keyboard.press("Escape");

  const titleInput = page.locator("#view input[aria-label='Новая задача / событие']");
  await titleInput.fill("Modal test item");
  await page.locator("#view .btn-primary", { hasText: "Сохранить" }).click();
  await expect(page.locator("#toast")).toContainText("Сохранено.");
  // After save, we're on the Today view with the new item.
  await expect(page.locator("#view .item-title", { hasText: "Modal test item" })).toBeVisible();

  // Trigger delete → confirm dialog.
  // The item card should have action buttons.
  const itemCard = page.locator("#view .card", { hasText: "Modal test item" }).first();
  const deleteBtn = itemCard.locator(".item-actions .btn").last();
  await deleteBtn.click();

  // Confirm dialog appears with role=alertdialog.
  const dialog = page.locator("#sheet-root .sheet-confirm");
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveAttribute("role", "alertdialog");

  // Scroll lock active on confirm dialog too.
  const confirmBody = await page.evaluate(() => ({
    overflow: document.body.style.overflow,
    position: document.body.style.position,
  }));
  expect(confirmBody.overflow).toBe("hidden");
  expect(confirmBody.position).toBe("fixed");

  // Focus trap in confirm dialog: Tab cycles between the two buttons.
  const btns = dialog.locator("button");
  const btnCount = await btns.count();
  expect(btnCount).toBe(2);
  // Tab from confirm → cancel, and from cancel → confirm (wraps).
  await page.keyboard.press("Tab");
  let activeIsInDialog = await page.evaluate(() =>
    document.querySelector("#sheet-root .sheet-confirm")?.contains(document.activeElement),
  );
  expect(activeIsInDialog, "Tab: focus escaped confirm dialog").toBe(true);
  await page.keyboard.press("Tab");
  activeIsInDialog = await page.evaluate(() =>
    document.querySelector("#sheet-root .sheet-confirm")?.contains(document.activeElement),
  );
  expect(activeIsInDialog, "Tab x2: focus escaped confirm dialog").toBe(true);

  // Cancel the dialog.
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);

  // Scroll lock released after confirm dialog closes.
  const bodyAfterConfirm = await page.evaluate(() => ({
    overflow: document.body.style.overflow,
    position: document.body.style.position,
  }));
  expect(bodyAfterConfirm.overflow).toBe("");
  expect(bodyAfterConfirm.position).toBe("");

  // Cleanup: delete the test item.
  const itemCard2 = page.locator("#view .card", { hasText: "Modal test item" }).first();
  await itemCard2.locator(".item-actions .btn").last().click();
  await page.locator("#sheet-root .sheet-confirm .btn-danger").click();
  await expect(page.locator("#view .item-title", { hasText: "Modal test item" })).toHaveCount(0);

  guard.assertClean();
});

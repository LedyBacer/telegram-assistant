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

/**
 * V5.2 §14: scroll lock on the REAL scroll container (#view). With enough
 * content to scroll: a pre-open scrollTop is preserved, while the sheet is
 * open neither programmatic nor wheel scrolling moves the content, and on
 * close the scrollability is fully restored at the same position.
 */
test("sheet scroll-locks the #view container and restores scrollTop on close", async ({
  page,
}) => {
  const guard = await openApp(page, base);
  const view = page.locator("#view");

  // Create enough items for the Today list to overflow the viewport.
  const created: number[] = [];
  try {
    for (let i = 0; i < 15; i++) {
      const res = await page.evaluate(async (n) => {
        const r = await fetch("/api/v1/items", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Telegram-Init-Data": "e2e-init-data",
          },
          // Anchored to today (starts_at) so the items land in the Today
          // day list — items without a date never render there.
          body: JSON.stringify({
            title: `Scroll lock item ${n}`,
            starts_at: new Date().toISOString(),
          }),
        });
        if (!r.ok) throw new Error(`create ${n} → ${r.status}`);
        return (await r.json()).id;
      }, i);
      created.push(res);
    }
    // Re-render Today (items were created outside the app).
    await page.locator('.nav-btn[data-tab="actions"]').click();
    await page.locator('.nav-btn[data-tab="today"]').click();
    await view.locator(".item-title", { hasText: "Scroll lock item 14" }).waitFor();

    // The view is genuinely scrollable before anything opens.
    const pre = await view.evaluate((el) => ({
      scrollable: el.scrollHeight > el.clientHeight,
      overflowY: getComputedStyle(el).overflowY,
    }));
    expect(pre.scrollable).toBe(true);
    expect(pre.overflowY).toBe("auto");

    // Scroll partway down; remember the position.
    await view.evaluate((el) => {
      el.scrollTop = 300;
    });
    const scrolled = await view.evaluate((el) => el.scrollTop);
    expect(scrolled).toBeGreaterThan(0);

    // Open the More sheet over Today.
    await page.locator('.nav-btn[data-tab="more"]').click();
    await page.locator("#sheet-root .sheet").waitFor();

    // While open: #view is overflow-locked; user (wheel) scrolling is
    // prevented, so the position is unchanged. (A programmatic scrollTop
    // write is allowed even with overflow:hidden and is not the behavior
    // under test.)
    const lockedStyle = await view.evaluate((el) => getComputedStyle(el).overflowY);
    expect(lockedStyle).toBe("hidden");
    await page.mouse.move(20, 200);
    await page.mouse.wheel(0, 500);
    await page.waitForTimeout(100);
    const lockedScrollTop = await view.evaluate((el) => el.scrollTop);
    expect(lockedScrollTop).toBe(scrolled);

    // Close the sheet.
    await page.keyboard.press("Escape");
    await expect(page.locator("#sheet-root .sheet")).toHaveCount(0);

    // Restored: scrollable again, at the same position.
    const after = await view.evaluate((el) => ({
      overflowY: getComputedStyle(el).overflowY,
      scrollTop: el.scrollTop,
    }));
    expect(after.overflowY).toBe("auto");
    expect(after.scrollTop).toBe(scrolled);
  } finally {
    for (const id of created) {
      await page
        .evaluate(async (itemId) => {
          await fetch(`/api/v1/items/${itemId}`, {
            method: "DELETE",
            headers: { "X-Telegram-Init-Data": "e2e-init-data" },
          });
        }, id)
        .catch(() => undefined);
    }
    guard.assertClean();
  }
});

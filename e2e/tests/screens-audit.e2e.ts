import { test, expect, type Page } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { openApp, assertNoHorizontalOverflow } from "../helpers/app";

const here = path.dirname(fileURLToPath(import.meta.url));
const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);
const fixtureFile = path.resolve(here, "../fixtures/upload-fixture.txt");

// Unique per run so re-runs against a warm DB never collide.
const run = Date.now().toString(36);
const factText = `Audit-${run}: coffee at 08:00`;
const taskTitle = `Audit-${run}: review spec`;

/**
 * Programmatic per-screen audit: every data screen must render real content
 * (loading/empty/populated states), user strings must appear as text (never
 * stringified DOM nodes), and no forbidden literals may leak into the UI.
 */

async function goTab(page: Page, tab: string): Promise<void> {
  await page.locator(`.nav-btn[data-tab="${tab}"]`).click();
}

/** No rendering-bug literals in the visible view text. */
async function assertCleanText(page: Page): Promise<void> {
  const text = await page.locator("#view").innerText();
  expect(text).not.toContain("[object");
  expect(text).not.toContain("undefined");
  expect(text).not.toContain("null");
  expect(text).not.toContain("Loading...");
}

test("per-screen audit: states, user content, settings, task lifecycle", async ({
  page,
}) => {
  const guard = await openApp(page, base);

  // --- Today / calendar ---------------------------------------------------
  // Today is marked, the month label is present, every real day is a button.
  await expect(page.locator("#view .cal-day.is-today")).toBeVisible();
  expect((await page.locator("#view .cal-month").innerText()).trim()).not.toBe(
    "",
  );
  const dayCount = await page.locator("#view .cal-day[data-date]").count();
  expect(dayCount).toBeGreaterThanOrEqual(28); // a full month
  // Tap a day that has no events: selection moves and a clean empty-day
  // state is shown (earlier specs may have left items on other days).
  const other = page
    .locator("#view .cal-day[data-date]:not(.is-selected):not(.has-events)")
    .first();
  const otherDate = await other.getAttribute("data-date");
  await other.click();
  await expect(page.locator(`#view .cal-day[data-date="${otherDate}"]`)).toHaveClass(
    /is-selected/,
  );
  await expect(page.locator("#view .state-empty")).toBeVisible();
  // The "today" control returns to the current month/day.
  await page.locator("#view .cal-header .btn").nth(2).click();
  await expect(page.locator("#view .cal-day.is-today.is-selected")).toHaveCount(1);
  await assertCleanText(page);
  await assertNoHorizontalOverflow(page);

  // --- Upcoming: deterministic state -------------------------------------
  // The earlier scenario spec may have left a due-today item, so assert the
  // screen renders one of its real states (list or empty), never an error.
  await goTab(page, "upcoming");
  await expect(page.locator("#view .list, #view .state-empty").first()).toBeVisible();
  await expect(page.locator("#view .state-error")).toHaveCount(0);
  await assertCleanText(page);

  // --- New: form controls -------------------------------------------------
  await goTab(page, "new");
  await expect(page.locator("#view h2.view-title")).toBeVisible();
  const textInputs = page.locator("#view input.field-input");
  // title, description, reminders (3+ labeled text inputs).
  expect(await textInputs.count()).toBeGreaterThanOrEqual(3);
  // 4 picker fields: kind, priority, starts, due.
  await expect(page.locator("#view .picker-field")).toHaveCount(4);
  // The due picker opens Flatpickr in date/time mode and Escape closes it.
  await page.locator("#view .picker-field").nth(3).click();
  await page.locator(".flatpickr-calendar").waitFor({ state: "visible" });
  await expect(page.locator(".flatpickr-calendar .flatpickr-time")).toBeVisible();
  // Flatpickr listens for Escape on its own input: focus it, then close.
  await page.locator(".picker-input").focus();
  await page.keyboard.press("Escape");
  await expect(page.locator(".flatpickr-calendar")).toBeHidden();
  await assertCleanText(page);
  await assertNoHorizontalOverflow(page);

  // --- Workouts: populated stats shell + empty recent list ----------------
  await goTab(page, "workouts");
  await expect(page.locator("#view .stats-grid .stat-cell")).toHaveCount(4);
  await expect(page.locator("#view .state-empty")).toBeVisible();
  await assertCleanText(page);

  // --- Files: upload → status badge + filename ---------------------------
  // (Earlier specs may have left files, so assert by occurrence count.)
  await goTab(page, "files");
  await expect(page.locator("#view .list, #view .state-empty").first()).toBeVisible();
  const namesBefore = await page.locator("#view .file-name").count();
  await page.locator("#file-upload-input").setInputFiles(fixtureFile);
  await expect(async () => {
    expect(await page.locator("#view .file-name").count()).toBe(namesBefore + 1);
  }).toPass();
  const fileCard = page.locator("#view .card .file-name").first();
  await expect(fileCard).toContainText("upload-fixture.txt");
  // A processing/ready status badge with non-empty text is shown.
  const fileCardBox = page.locator("#view .list .card").first();
  const badgeText = (await fileCardBox.locator(".item-head .badge").first().innerText()).trim();
  expect(badgeText).not.toBe("");
  // The internal storage path is not displayed.
  const fileText = await fileCardBox.innerText();
  expect(fileText).not.toContain("storage/");
  await assertCleanText(page);
  await assertNoHorizontalOverflow(page);

  // --- Facts: propose → exact text rendered as a text node ---------------
  // (Earlier specs may have left facts, so do not assume an empty list.)
  await goTab(page, "facts");
  await expect(page.locator("#view .list, #view .state-empty").first()).toBeVisible();
  await page.locator("#view .card input.field-input").first().fill(factText);
  await page.locator("#view .card .btn-primary").first().click();
  await expect(page.locator("#view .fact-value", { hasText: factText })).toHaveCount(1);
  await assertCleanText(page);

  // --- Task lifecycle: create → appears in Today + calendar dot ----------
  // Create a due-today task from the New screen.
  await goTab(page, "new");
  await page.locator("#view .card input.field-input").first().fill(taskTitle);
  await page.locator("#view .picker-field").nth(3).click();
  await page.locator(".flatpickr-calendar").waitFor({ state: "visible" });
  await page.locator(".flatpickr-calendar .today").click();
  await page.keyboard.press("Escape");
  await page.locator("#view .card .btn-primary").first().click();
  // Saved: back on Today with the task listed and its completion actions.
  const myCard = page.locator("#view .list .card", { hasText: taskTitle }).first();
  await expect(myCard.locator(".item-title")).toContainText(taskTitle);
  // The calendar marks the day as having events.
  await expect(page.locator("#view .cal-day.has-events")).toBeVisible();
  // Complete it: the done/cancel actions disappear (a status badge replaces
  // them) and only the delete action remains on the card.
  const actionsBefore = await myCard.locator(".item-actions .btn").count();
  expect(actionsBefore).toBe(4); // edit, done, cancel, delete
  await myCard.locator(".item-actions .btn", { hasText: "✓ Готово" }).click();
  // Completion re-renders after the API round-trip: wait for the done/cancel
  // actions to be replaced by the completed state (delete only).
  await expect(myCard.locator(".item-actions .btn")).toHaveCount(1);
  // Delete it: the confirm dialog (alertdialog) appears; confirming removes it.
  await myCard.locator(".item-actions .btn").last().click();
  const dialog = page.locator("#sheet-root .sheet-confirm");
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveAttribute("role", "alertdialog");
  await dialog.locator(".sheet-confirm-actions .btn").last().click();
  await expect(page.locator("#view .list .card", { hasText: taskTitle })).toHaveCount(0);
  await assertCleanText(page);

  // --- Upcoming: the created task is gone after deletion -----------------
  await goTab(page, "upcoming");
  await expect(page.locator("#view .list, #view .state-empty").first()).toBeVisible();
  await expect(
    page.locator("#view .item-title", { hasText: taskTitle }),
  ).toHaveCount(0);

  // --- Settings: language, timezone, digest time, motivation --------------
  await goTab(page, "settings");
  const rows = page.locator("#view button.settings-row");
  // 7 tappable rows: language, timezone, digest time + the proactive card's
  // quiet-from, quiet-until, max-per-day, min-interval rows.
  await expect(rows).toHaveCount(7);
  // Digest row shows an HH:MM value.
  const digestValue = (await rows.nth(2).locator(".settings-row-value").innerText()).trim();
  expect(digestValue).toMatch(/^\d{2}:\d{2}$/);
  // Timezone row shows a non-empty value.
  expect((await rows.nth(1).locator(".settings-row-value").innerText()).trim()).not.toBe("");
  // Five role=switch checkboxes: motivation (main card) + the four
  // proactive-settings switches (enabled, weekly, workout, overdue).
  const swAll = page.locator('#view input.switch[role="switch"]');
  await expect(swAll).toHaveCount(5);
  // The main settings card renders before the proactive card, so the
  // motivation switch is first in DOM order.
  const sw = swAll.first();
  // Digest row opens the Flatpickr time picker (24h, no date inputs).
  await rows.nth(2).click();
  await page.locator(".flatpickr-calendar").waitFor({ state: "visible" });
  await expect(page.locator(".flatpickr-calendar .flatpickr-time")).toBeVisible();
  await expect(page.locator(".flatpickr-calendar input[type=\"date\"]")).toHaveCount(0);
  await page.locator(".picker-input").focus();
  await page.keyboard.press("Escape");
  await expect(page.locator(".flatpickr-calendar")).toBeHidden();
  // Motivation: toggling persists (toast shown, switch state survives render).
  const before = await sw.isChecked();
  await sw.click();
  await expect(page.locator("#toast")).toBeVisible();
  // The toggle persisted through the re-render (the switch keeps its state).
  expect(await sw.isChecked()).toBe(!before);
  // Language row: switch ru → en and back; the UI updates immediately.
  await rows.nth(0).click();
  await page.locator('#sheet-root .sheet .sheet-row[data-value="en"]').click();
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(page.locator(".nav-btn[data-tab=\"today\"] .nav-label")).toContainText("Today");
  // Re-open settings in English (it re-rendered after the language change).
  await goTab(page, "settings");
  await page.locator("#view button.settings-row").first().click();
  await page.locator('#sheet-root .sheet .sheet-row[data-value="ru"]').click();
  await expect(page.locator("html")).toHaveAttribute("lang", "ru");
  const todayLabel = (
    await page.locator('.nav-btn[data-tab="today"] .nav-label').innerText()
  ).trim();
  expect(todayLabel).toMatch(/^\p{Extended_Pictographic}/u);
  await assertCleanText(page);
  await assertNoHorizontalOverflow(page);

  // --- Touch targets hold on every screen --------------------------------
  for (const tab of ["today", "actions", "upcoming", "new", "workouts", "files", "facts", "settings"]) {
    await goTab(page, tab);
    const small = await page.evaluate(() => {
      const els = Array.from(
        document.querySelectorAll<HTMLElement>(
          "#view button, #view input, .nav-btn, .settings-row, .picker-field",
        ),
      );
      return els
        .filter((e) => e.offsetParent !== null)
        .filter((e) => e.getBoundingClientRect().height < 44)
        .map((e) => `${e.tagName}.${e.className}`);
    });
    expect(small, `tab ${tab}: sub-44px controls: ${small.join(", ")}`).toHaveLength(0);
    await assertNoHorizontalOverflow(page);
    await assertCleanText(page);
  }

  guard.assertClean();
});

import { test, expect, type Page } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  openApp,
  assertNoHorizontalOverflow,
  assertNoLeakedDom,
} from "../helpers/app";
import { THEME_PRESETS } from "../helpers/telegram-stub";

const here = path.dirname(fileURLToPath(import.meta.url));
const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);
const fixtureFile = path.resolve(here, "../fixtures/upload-fixture.txt");

// Unique per run so re-runs against a warm DB never collide.
const run = Date.now().toString(36);
const factText = `Fakt-${run}: height 183cm`;
const taskTitle = `Task-${run}: ship miniapp`;
const fileName = "upload-fixture.txt";

async function goTab(page: Page, tab: string): Promise<void> {
  await page.locator(`.nav-btn[data-tab="${tab}"]`).click();
}

test("Mini App end-to-end scenario", async ({ page }) => {
  // 1-2. Backend up + open app; root redirects to /miniapp and the Today view
  //      renders (calendar). Console guard is active for the whole test.
  const guard = await openApp(page, base);

  // 3. Settings loaded: the identity is shown and the app boots in Russian
  //    (the default persisted language).
  await expect(page.locator("#who")).toContainText("Test User");
  await expect(page.locator("#app-title")).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("lang", "ru");

  // 4. Navigate every section once and assert each renders a real view.
  const sections: Array<[string, string]> = [
    ["today", "#view .calendar"],
    ["upcoming", "#view"],
    ["new", "#view .field-input"],
    ["workouts", "#view .stats-grid"],
    ["files", "#view .upload-btn"],
    ["facts", "#view .fact-form, #view .card"],
    ["settings", "#view .settings-row"],
  ];
  for (const [tab, selector] of sections) {
    await goTab(page, tab);
    await expect(page.locator(selector).first()).toBeVisible();
  }

  // 5. Fresh DB: the Today view shows an empty-day state (no items yet).
  await goTab(page, "today");
  await expect(page.locator("#view .state-empty").first()).toBeVisible();

  // 6-7. Add a fact with a unique exact value, then verify the EXACT text is
  //      rendered as a text node (root-cause regression for
  //      [object HTMLDivElement]).
  await goTab(page, "facts");
  await page.locator("#view .card input.field-input").first().fill(factText);
  await page.locator("#view .card .btn-primary").first().click();
  await expect(page.locator("#view .fact-value").first()).toContainText(factText);
  await assertNoLeakedDom(page, factText);

  // 8-9. Upload the fixture and verify the filename renders (no internal path).
  await goTab(page, "files");
  await page.locator("#file-upload-input").setInputFiles(fixtureFile);
  await expect(page.locator("#view .file-name").first()).toContainText(fileName);
  const fileCardText = await page.locator("#view .card").last().innerText();
  expect(fileCardText, "no internal storage path leaked").not.toContain("/");

  // 10. Create a task (New), giving it a due date via the Flatpickr picker so
  //     it lands on today and becomes visible in the calendar.
  await goTab(page, "new");
  await page.locator("#view .card input.field-input").first().fill(taskTitle);
  // Picker fields in the New form: 0=kind, 1=priority, 2=starts, 3=due.
  // Open the "due" date/time picker so the task anchors to a date.
  await page.locator("#view .picker-field").nth(3).click();
  await page.locator(".flatpickr-calendar").waitFor({ state: "visible" });
  await page.locator(".flatpickr-calendar .today").click();
  await page.keyboard.press("Escape");
  await expect(page.locator("#view .picker-field").nth(3)).toContainText(/\d/);
  await page.locator("#view .card .btn-primary").first().click();

  // 11. Task saved: we are back on Today and the task is listed for today.
  await expect(page.locator("#view .item-title").first()).toContainText(taskTitle);

  // 12. Calendar: tapping a day cell changes the selected day, and the
  //     "today" control returns to the current day (so the task is visible).
  // Out-of-month padding cells have no data-date, so scope to real days.
  const anyDay = page.locator(
    "#view .cal-day[data-date]:not(.is-selected)",
  ).first();
  const tappedDate = await anyDay.getAttribute("data-date");
  await anyDay.click();
  await expect(page.locator(`#view .cal-day[data-date="${tappedDate}"]`))
    .toHaveClass(/is-selected/);
  // cal-header buttons: 0=prev, 1=next, 2=today.
  await page.locator("#view .cal-header .btn").nth(2).click();
  await expect(page.locator("#view .cal-day.is-today")).toBeVisible();
  await expect(page.locator("#view .item-title").first()).toContainText(taskTitle);

  // 13. Bottom sheet: open the priority picker on New, verify selected state,
  //     pick a value, and confirm it closes.
  await goTab(page, "new");
  await page.locator("#view .picker-field").nth(1).click(); // priority
  const sheet = page.locator("#sheet-root .sheet");
  await expect(sheet).toBeVisible();
  await expect(sheet.locator(".sheet-row").first()).toBeVisible();
  const selectedBefore = sheet.locator(".sheet-row.is-selected").first();
  await expect(selectedBefore).toHaveAttribute("aria-selected", "true");
  await sheet.locator(".sheet-row").nth(2).click(); // pick the 3rd option
  await expect(sheet).toBeHidden();
  // Escaping a freshly opened sheet closes it without a selection.
  await page.locator("#view .picker-field").nth(1).click();
  await expect(page.locator("#sheet-root .sheet")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator("#sheet-root .sheet")).toBeHidden();

  // 14. Language ru → en: open the language row on Settings, choose English.
  await goTab(page, "settings");
  await page.locator("#view .settings-row").first().click();
  await expect(page.locator("#sheet-root .sheet")).toBeVisible();
  await page.locator("#sheet-root .sheet .sheet-row[data-value=\"en\"]").click();
  // UI switches immediately, without a reload.
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(page.locator(".nav-btn[data-tab=\"today\"] .nav-label"))
    .toContainText("Today");
  await assertNoLeakedDom(page, "Today");

  // 15. Reload: the language and the created fact persist.
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForURL("**/miniapp");
  await page.locator("#view .calendar, #view .state-error").first().waitFor({
    state: "visible",
  });
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(page.locator(".nav-btn[data-tab=\"today\"] .nav-label"))
    .toContainText("Today");
  await goTab(page, "facts");
  await expect(page.locator("#view .fact-value").first()).toContainText(factText);

  // 16. Controlled API error: force a 500 on the workout stats endpoint and
  //     verify the screen degrades to a localized error state (no crash).
  guard.allow("/api/v1/workouts/stats");
  await page.route("**/api/v1/workouts/stats", (route) =>
    route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"boom"}' }),
  );
  await goTab(page, "workouts");
  await expect(page.locator("#view .state-error")).toBeVisible();

  // 17. Layout + visual invariants on the 390px viewport.
  await goTab(page, "today");
  await assertNoHorizontalOverflow(page);

  // 18. Touch targets: every visible interactive control is >= 44px tall.
  const targets = await page.evaluate(() => {
    const els = Array.from(
      document.querySelectorAll<HTMLElement>(
        "#view button, #view input, .nav-btn, .settings-row, .picker-field",
      ),
    );
    return els
      .filter((e) => e.offsetParent !== null) // visible only
      .map((e) => ({ tag: e.tagName, h: Math.round(e.getBoundingClientRect().height) }));
  });
  const tooSmall = targets.filter((t) => t.h < 44);
  expect(
    tooSmall,
    `touch targets below 44px: ${JSON.stringify(tooSmall)}`,
  ).toHaveLength(0);

  // 19. Telegram WebApp integration: ready/expand were called and the light
  //     theme parameters were applied as CSS variables.
  const tgState = await page.evaluate(() => ({
    ready: (window as any).__tg.counters.ready,
    expand: (window as any).__tg.counters.expand,
    btn: getComputedStyle(document.documentElement)
      .getPropertyValue("--tg-theme-button-color")
      .trim(),
  }));
  expect(tgState.ready).toBeGreaterThanOrEqual(1);
  expect(tgState.expand).toBeGreaterThanOrEqual(1);
  expect(tgState.btn).toBe(THEME_PRESETS.light.params.button_color);

  // 20. No uncaught exceptions, console errors, or unexpected API failures.
  guard.assertClean();
});

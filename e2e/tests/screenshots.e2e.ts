import { test, expect } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { openApp } from "../helpers/app";
import type { ThemeName } from "../helpers/telegram-stub";

const here = path.dirname(fileURLToPath(import.meta.url));
const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);
const fixtureFile = path.resolve(here, "../fixtures/upload-fixture.txt");

// Diagnostic artifacts only — the model cannot visually verify these, so the
// spec asserts the page is in the expected state before capturing.
const outDir = path.resolve(here, "../../test-artifacts/screenshots");

async function setTheme(page: any, name: ThemeName): Promise<void> {
  await page.evaluate((n: ThemeName) => (window as any).__tg.setTheme(n), name);
}

test("capture reference screenshots (390x844)", async ({ page }) => {
  await openApp(page, base);
  const factText = `Fakt-shots: city Vienna`;

  // home-light — Today view, light theme.
  await setTheme(page, "light");
  await expect(page.locator("#view .calendar")).toBeVisible();
  await page.screenshot({ path: path.join(outDir, "home-light.png") });

  // home-dark — Today view, dark theme.
  await setTheme(page, "dark");
  await expect(page.locator("#view .calendar")).toBeVisible();
  await page.screenshot({ path: path.join(outDir, "home-dark.png") });

  // calendar-light — Today/calendar, light theme.
  await setTheme(page, "light");
  await page.locator(".cal-day:not(.is-out)").nth(2).click();
  await expect(page.locator("#view .calendar")).toBeVisible();
  await page.screenshot({ path: path.join(outDir, "calendar-light.png") });

  // settings-dark — Settings, dark theme.
  await page.locator(".nav-btn[data-tab=\"settings\"]").click();
  await setTheme(page, "dark");
  await expect(page.locator("#view .settings-row").first()).toBeVisible();
  await page.screenshot({ path: path.join(outDir, "settings-dark.png") });

  // facts-populated — a confirmed-looking fact list, light theme.
  await setTheme(page, "light");
  await page.locator(".nav-btn[data-tab=\"facts\"]").click();
  await page.locator("#view .card input.field-input").first().fill(factText);
  await page.locator("#view .card .btn-primary").first().click();
  await expect(page.locator("#view .fact-value").first()).toContainText(factText);
  await page.screenshot({ path: path.join(outDir, "facts-populated.png") });

  // files-populated — an uploaded file card, light theme.
  await page.locator(".nav-btn[data-tab=\"files\"]").click();
  await page.locator("#file-upload-input").setInputFiles(fixtureFile);
  await expect(page.locator("#view .file-name").first()).toBeVisible();
  await page.screenshot({ path: path.join(outDir, "files-populated.png") });
});

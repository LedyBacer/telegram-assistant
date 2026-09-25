import { test, expect } from "@playwright/test";

// V5.2 §12: while a secondary tab (opened through the "More" sheet) is
// active, the More launcher carries a secondary active state —
// `is-active-secondary` + `aria-current` — while the primary tabs do not.
// Primary tabs never put the More button into that state.

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);

const SECONDARY = ["upcoming", "workouts", "facts", "settings"];
const PRIMARY = ["today", "actions", "new", "files"];

test("More launcher reflects the secondary active tab", async ({ page }) => {
  const { openApp, goTab } = await import("../helpers/app");
  const guard = await openApp(page, base);
  const more = page.locator('.nav-btn[data-tab="more"]');
  try {
    for (const tab of SECONDARY) {
      await goTab(page, tab);
      await expect(more).toHaveClass(/is-active-secondary/);
      await expect(more).toHaveAttribute("aria-current", "true");
    }
    for (const tab of PRIMARY) {
      await goTab(page, tab);
      await expect(more).not.toHaveClass(/is-active-secondary/);
      await expect(more).not.toHaveAttribute("aria-current");
      // The primary tab itself is the one marked active.
      await expect(page.locator(`.nav-btn[data-tab="${tab}"]`)).toHaveClass(
        /is-active/,
      );
    }
  } finally {
    guard.assertClean();
  }
});

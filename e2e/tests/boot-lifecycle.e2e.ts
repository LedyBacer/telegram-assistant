import { test, expect, type Page } from "@playwright/test";
import { installTelegramStub } from "../helpers/telegram-stub";
import { installFlatpickrCdn } from "../helpers/flatpickr-cdn";
import { createConsoleGuard } from "../helpers/console-guard";
import { openApp } from "../helpers/app";

// V5.2 §8: boot lifecycle — `expand()` exactly once and BEFORE `ready()`
// (recorded in the stub's lifecycle array), and `ready()` exactly once with
// a visible view already on screen. The same order holds on a failed boot
// (/me 500): the explicit startup error IS the visible UI.

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);

test("normal boot: expand once, ready once after the visible view", async ({
  page,
}: {
  page: Page;
}) => {
  const guard = await openApp(page, base);
  try {
    const lifecycle = await page.evaluate(() => (window as any).__tg.lifecycle);
    const expands = lifecycle.filter((e: { event: string }) => e.event === "expand");
    const readys = lifecycle.filter((e: { event: string }) => e.event === "ready");
    expect(expands).toHaveLength(1);
    expect(readys).toHaveLength(1);
    // expand is the FIRST event: before ready, long before /me resolves.
    expect(lifecycle[0].event).toBe("expand");
    expect(lifecycle[1].event).toBe("ready");
    // ready fired only after a visible view was on screen.
    expect(readys[0].hasVisibleView).toBe(true);
  } finally {
    guard.assertClean();
  }
});

test("error boot (/me 500): startup error visible, ready once after expand", async ({
  page,
}: {
  page: Page;
}) => {
  const guard = createConsoleGuard(page, base);
  guard.allow("/api/v1/me");
  await installTelegramStub(page);
  await installFlatpickrCdn(page);
  await page.route("**/api/v1/me", (route) =>
    route.fulfill({ status: 500, contentType: "application/json", body: "{}" }),
  );

  await page.goto("/", { waitUntil: "domcontentloaded" });
  await page.waitForURL("**/miniapp", { timeout: 15_000 });
  // A failed /me replaces the view with the explicit empty/startup state
  // (miniapp.status_open), NOT a generic .state-error.
  await page
    .locator("#view .state-empty")
    .first()
    .waitFor({ state: "visible", timeout: 20_000 });
  try {
    const lifecycle = await page.evaluate(() => (window as any).__tg.lifecycle);
    const expands = lifecycle.filter((e: { event: string }) => e.event === "expand");
    const readys = lifecycle.filter((e: { event: string }) => e.event === "ready");
    expect(expands).toHaveLength(1);
    expect(readys).toHaveLength(1);
    expect(lifecycle[0].event).toBe("expand");
    expect(lifecycle[1].event).toBe("ready");
    // The startup error IS a visible view: ready saw it.
    expect(readys[0].hasVisibleView).toBe(true);
  } finally {
    guard.assertClean();
  }
});

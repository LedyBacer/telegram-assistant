import { test, expect, type Page } from "@playwright/test";
import { openApp, assertNoHorizontalOverflow } from "../helpers/app";
import { THEME_PRESETS } from "../helpers/telegram-stub";

// V5 §11–13 + V5.1 P0 #4/#5/#6/#7: the four safe-area tokens AND the four
// CONTENT safe-area tokens (a distinct group) resolve on :root from
// NON-ZERO client values, the stable viewport height comes from
// viewportStableHeight, runtime changes re-apply the tokens, and the native
// chrome colors follow the active Telegram theme.
test.use({ timezoneId: "UTC" });

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);

interface Vars {
  height: string;
  top: string;
  right: string;
  bottom: string;
  left: string;
  cTop: string;
  cRight: string;
  cBottom: string;
  cLeft: string;
}

/** Read both token groups + the stable height from :root. */
async function readVars(page: Page): Promise<Vars> {
  return page.evaluate(() => {
    const cs = getComputedStyle(document.documentElement);
    return {
      height: cs.getPropertyValue("--tg-viewport-stable-height").trim(),
      top: cs.getPropertyValue("--safe-top").trim(),
      right: cs.getPropertyValue("--safe-right").trim(),
      bottom: cs.getPropertyValue("--safe-bottom").trim(),
      left: cs.getPropertyValue("--safe-left").trim(),
      cTop: cs.getPropertyValue("--tg-content-safe-top").trim(),
      cRight: cs.getPropertyValue("--tg-content-safe-right").trim(),
      cBottom: cs.getPropertyValue("--tg-content-safe-bottom").trim(),
      cLeft: cs.getPropertyValue("--tg-content-safe-left").trim(),
    };
  });
}

test("safe-area + content-safe + stable-height tokens; chrome colors sync", async ({
  page,
}: {
  page: Page;
}) => {
  const guard = await openApp(page, base);
  try {
    // P0 #7: the stub reports NON-ZERO insets (top 59, bottom 34) and
    // viewportStableHeight 844 — all of it must land on :root as px values.
    const vars = await readVars(page);
    expect(vars.height).toBe("844px");
    expect(vars.top).toBe("59px");
    expect(vars.right).toBe("0px");
    expect(vars.bottom).toBe("34px");
    expect(vars.left).toBe("0px");

    // P0 #5: the content group is DISTINCT from the webview group — the
    // stub's contentSafeAreaInset has bottom 16 (vs safeAreaInset 34), and
    // that difference must be visible in the tokens.
    expect(vars.cTop).toBe("59px");
    expect(vars.cRight).toBe("0px");
    expect(vars.cBottom).toBe("16px");
    expect(vars.cLeft).toBe("0px");

    // P0 #6: a runtime safeAreaChanged re-applies only the webview group.
    await page.evaluate(
      () => (window as any).__tg.setSafeAreaInset({ top: 47, right: 0, bottom: 20, left: 0 }),
    );
    const afterSafe = await readVars(page);
    expect(afterSafe.top).toBe("47px");
    expect(afterSafe.bottom).toBe("20px");
    // The content group is untouched by a safeAreaChanged.
    expect(afterSafe.cTop).toBe("59px");
    expect(afterSafe.cBottom).toBe("16px");

    // P0 #6: a runtime contentSafeAreaChanged re-applies only the content
    // group.
    await page.evaluate(
      () => (window as any).__tg.setContentSafeAreaInset({ top: 47, right: 0, bottom: 8, left: 0 }),
    );
    const afterContent = await readVars(page);
    expect(afterContent.cBottom).toBe("8px");
    // The webview group is untouched by a contentSafeAreaChanged.
    expect(afterContent.top).toBe("47px");
    expect(afterContent.bottom).toBe("20px");

    // P0 #4/#6: a runtime viewportChanged updates the stable height from
    // viewportStableHeight.
    await page.evaluate(() => (window as any).__tg.setViewportStableHeight(700));
    const afterHeight = await readVars(page);
    expect(afterHeight.height).toBe("700px");

    // §13: chrome color sync — the header and bottom bar were pushed the
    // active (light) theme colors at boot.
    const light = THEME_PRESETS.light;
    const chrome = await page.evaluate(() => (window as any).__tg.chromeCalls);
    expect(chrome.setHeaderColor).toContain(light.params.header_bg_color);
    expect(chrome.setBottomBarColor).toContain(light.params.section_bg_color);

    // Switch to dark: header + bottom bar + <meta theme-color> re-sync.
    await page.evaluate(() => (window as any).__tg.setTheme("dark"));
    const dark = THEME_PRESETS.dark;
    const chrome2 = await page.evaluate(() => (window as any).__tg.chromeCalls);
    expect(chrome2.setHeaderColor).toContain(dark.params.header_bg_color);
    expect(chrome2.setBottomBarColor).toContain(dark.params.section_bg_color);
    const meta = await page.locator('meta[name="theme-color"]').getAttribute("content");
    expect(meta).toBe(dark.params.bg_color);
  } finally {
    guard.assertClean();
  }
});

// V5.2 §5: when viewportStableHeight is unavailable, the height token falls
// back to the dedicated `viewportHeight` property — never to
// `viewportInfo.contentHeight` (the stub does not even expose viewportInfo,
// so a contentHeight fallback in the app could not resolve at all).
test("stable-height falls back to viewportHeight, not viewportInfo", async ({
  page,
}: {
  page: Page;
}) => {
  const guard = await openApp(page, base);
  try {
    // The stub has no viewportInfo: a contentHeight fallback is impossible.
    expect(await page.evaluate(() => (window as any).Telegram.WebApp.viewportInfo))
      .toBeUndefined();

    // Stable unavailable (0) + viewportHeight 720 -> 720px.
    await page.evaluate(() => {
      const tg = (window as any).__tg;
      tg.setViewportStableHeight(0);
      tg.setViewportHeight(720);
    });
    let vars = await readVars(page);
    expect(vars.height).toBe("720px");

    // Preferred case: stable 700 beats viewportHeight 760 -> 700px.
    await page.evaluate(() => {
      const tg = (window as any).__tg;
      tg.setViewportHeight(760);
      tg.setViewportStableHeight(700);
    });
    vars = await readVars(page);
    expect(vars.height).toBe("700px");
  } finally {
    guard.assertClean();
  }
});

// V5.2 §6: the bottom nav's footprint is a SINGLE token
// (--nav-height + --nav-safe-bottom). With deliberately different insets
// (safe bottom 40 vs content-safe bottom 8, landscape-like left/right 90),
// the nav fills exactly its footprint, the scrollable content ends ABOVE
// the nav (never under it), and the side insets cause no horizontal
// overflow.
test("nav footprint: content never ends under the fixed bottom nav", async ({
  page,
}: {
  page: Page;
}) => {
  const guard = await openApp(page, base);
  try {
    await page.evaluate(() => {
      const tg = (window as any).__tg;
      tg.setSafeAreaInset({ top: 59, right: 90, bottom: 40, left: 90 });
      tg.setContentSafeAreaInset({ top: 59, right: 8, bottom: 8, left: 8 });
    });

    const geo = await page.evaluate(() => {
      const nav = (document.querySelector(".bottomnav") as HTMLElement).getBoundingClientRect();
      const view = (document.querySelector("#view") as HTMLElement).getBoundingClientRect();
      const viewCS = getComputedStyle(document.querySelector("#view") as HTMLElement);
      const navCS = getComputedStyle(document.querySelector(".bottomnav") as HTMLElement);
      return {
        navTop: nav.top,
        navBottom: nav.bottom,
        navPaddingBottom: navCS.paddingBottom,
        viewPaddingBottom: viewCS.paddingBottom,
        contentBottom: view.bottom - Number.parseFloat(viewCS.paddingBottom),
        innerWidth: window.innerWidth,
        innerHeight: window.innerHeight,
      };
    });

    // Nav: min-height 56 + safe bottom 40 + 1px top border = the 97px
    // rendered bar (exactly --nav-footprint),
    // anchored to the viewport bottom.
    expect(geo.navPaddingBottom).toBe("40px");
    expect(geo.navBottom).toBe(geo.innerHeight);
    expect(geo.navBottom - geo.navTop).toBe(97);

    // .view clearance is the footprint + the 16px margin (112px) — the
    // content-safe bottom (8) is NOT double-counted into this token.
    expect(geo.viewPaddingBottom).toBe("113px");

    // The scrollable content ends ABOVE the fixed nav (with margin).
    expect(geo.contentBottom).toBeLessThanOrEqual(geo.navTop);

    // Landscape-like side insets: no horizontal overflow.
    await assertNoHorizontalOverflow(page);
  } finally {
    guard.assertClean();
  }
});

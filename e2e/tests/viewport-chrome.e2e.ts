import { test, expect, type Page } from "@playwright/test";
import { openApp } from "../helpers/app";
import { THEME_PRESETS } from "../helpers/telegram-stub";

// V5 §11–13: the four safe-area tokens resolve on :root, the stable viewport
// height falls back to the client's contentHeight (not a jumping 100vh), and
// the native chrome colors (header + bottom bar + <meta theme-color>) follow
// the active Telegram theme.
test.use({ timezoneId: "UTC" });

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);

test("safe-area + stable-height tokens resolve; chrome colors sync to the theme", async ({
  page,
}: {
  page: Page;
}) => {
  const guard = await openApp(page, base);
  try {
    // §11/§12: tokens resolved on :root. The stub reports viewportInfo
    // contentHeight 844 and safeAreaInset 0, so the stable height is 844px and
    // every safe-area edge is 0px (no env() insets in headless Chromium).
    const vars = await page.evaluate(() => {
      const cs = getComputedStyle(document.documentElement);
      return {
        height: cs.getPropertyValue("--tg-viewport-stable-height").trim(),
        top: cs.getPropertyValue("--safe-top").trim(),
        right: cs.getPropertyValue("--safe-right").trim(),
        bottom: cs.getPropertyValue("--safe-bottom").trim(),
        left: cs.getPropertyValue("--safe-left").trim(),
      };
    });
    expect(vars.height).toBe("844px");
    expect(vars.top).toBe("0px");
    expect(vars.right).toBe("0px");
    expect(vars.bottom).toBe("0px");
    expect(vars.left).toBe("0px");

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

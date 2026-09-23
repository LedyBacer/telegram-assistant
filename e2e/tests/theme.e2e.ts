import { test, expect } from "@playwright/test";
import { openApp } from "../helpers/app";
import { THEME_PRESETS, type ThemeName } from "../helpers/telegram-stub";

const base = "http://127.0.0.1:" + (process.env.E2E_PORT ?? 8123);

/** Convert a #rrggbb hex to the rgb() string getComputedStyle returns. */
function hexToRgb(hex: string): string {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgb(${r}, ${g}, ${b})`;
}

interface Computed {
  scheme: string;
  button: string;
  text: string;
  bg: string;
  btnBg: string; // the primary button's rendered background-color
}

/** Read the applied theme signals the spec cares about. */
async function readTheme(page: any): Promise<Computed> {
  return page.evaluate(() => {
    const root = document.documentElement;
    const cs = getComputedStyle(root);
    const primary = document.querySelector<HTMLElement>(".btn-primary");
    return {
      scheme: root.dataset.colorScheme || "",
      button: cs.getPropertyValue("--tg-theme-button-color").trim(),
      text: cs.getPropertyValue("--tg-theme-text-color").trim(),
      bg: cs.getPropertyValue("--tg-theme-bg-color").trim(),
      btnBg: primary ? getComputedStyle(primary).backgroundColor : "",
    };
  });
}

test("Telegram theme system: light / dark / custom + runtime change", async ({
  page,
}) => {
  await openApp(page, base);

  // Land on a screen that renders a primary button so the spec can verify the
  // theme button color is actually painted (not just set as a CSS variable).
  await page.locator('.nav-btn[data-tab="new"]').click();
  await expect(page.locator("#view .btn-primary").first()).toBeVisible();

  // Light theme (the default) is applied at boot.
  let c = await readTheme(page);
  expect(c.scheme).toBe(THEME_PRESETS.light.colorScheme);
  expect(c.button).toBe(THEME_PRESETS.light.params.button_color);
  expect(c.text).toBe(THEME_PRESETS.light.params.text_color);
  expect(c.bg).toBe(THEME_PRESETS.light.params.bg_color);
  // The primary button actually paints with the theme button color.
  expect(c.btnBg).toBe(hexToRgb(THEME_PRESETS.light.params.button_color));

  // Runtime theme change to dark: the app re-styles via CSS variables without
  // a reload, and the colorScheme flips.
  await page.evaluate((name: ThemeName) => (window as any).__tg.setTheme(name), "dark");
  c = await readTheme(page);
  expect(c.scheme).toBe(THEME_PRESETS.dark.colorScheme);
  expect(c.button).toBe(THEME_PRESETS.dark.params.button_color);
  expect(c.text).toBe(THEME_PRESETS.dark.params.text_color);
  expect(c.bg).toBe(THEME_PRESETS.dark.params.bg_color);
  expect(c.btnBg).toBe(hexToRgb(THEME_PRESETS.dark.params.button_color));

  // A distinctive custom theme proves non-default values flow through.
  await page.evaluate((name: ThemeName) => (window as any).__tg.setTheme(name), "custom");
  c = await readTheme(page);
  expect(c.button).toBe(THEME_PRESETS.custom.params.button_color);
  expect(c.text).toBe(THEME_PRESETS.custom.params.text_color);
  expect(c.bg).toBe(THEME_PRESETS.custom.params.bg_color);
  expect(c.btnBg).toBe(hexToRgb(THEME_PRESETS.custom.params.button_color));

  // Returning to light restores the original palette.
  await page.evaluate((name: ThemeName) => (window as any).__tg.setTheme(name), "light");
  c = await readTheme(page);
  expect(c.button).toBe(THEME_PRESETS.light.params.button_color);
  expect(c.bg).toBe(THEME_PRESETS.light.params.bg_color);
  expect(c.btnBg).toBe(hexToRgb(THEME_PRESETS.light.params.button_color));

  // The page never overflows while the palette swaps.
  const { scrollWidth, innerWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  }));
  expect(scrollWidth).toBeLessThanOrEqual(innerWidth);
});

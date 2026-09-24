import type { Page } from "@playwright/test";

/**
 * Deterministic test-only Telegram WebApp harness.
 *
 * `miniapp/js/telegram.js` captures `window.Telegram.WebApp` at MODULE LOAD,
 * so this stub MUST be installed via `page.addInitScript` before `app.js`
 * (an ES module) executes. The real `https://telegram.org/js/telegram-web-app.js`
 * CDN request is additionally intercepted and aborted so the stub is the only
 * source of the SDK.
 *
 * The stub exposes a `window.__tg` control surface so specs can:
 *   - assert `ready()` / `expand()` were called,
 *   - switch themes (light / dark / custom) and emit `themeChanged`,
 *   - fire the Telegram BackButton,
 *   - read HapticFeedback call counts,
 *   - change the reported client language.
 */

// 13 Telegram themeParams keys. Presets are hand-picked, realistic values so
// computed-style assertions are unambiguous between schemes.
export const THEME_PRESETS = {
  light: {
    colorScheme: "light",
    params: {
      bg_color: "#ffffff",
      secondary_bg_color: "#f1f1f2",
      text_color: "#000000",
      hint_color: "#909399",
      link_color: "#2481cc",
      button_color: "#2481cc",
      button_text_color: "#ffffff",
      header_bg_color: "#ffffff",
      accent_text_color: "#2481cc",
      section_bg_color: "#ffffff",
      section_header_text_color: "#909399",
      subtitle_text_color: "#8e8e93",
      destructive_text_color: "#e53935",
    },
  },
  dark: {
    colorScheme: "dark",
    params: {
      bg_color: "#1c1c1e",
      secondary_bg_color: "#2c2c2e",
      text_color: "#ffffff",
      hint_color: "#98979d",
      link_color: "#6bb2f5",
      button_color: "#6bb2f5",
      button_text_color: "#111111",
      header_bg_color: "#1c1c1e",
      accent_text_color: "#6bb2f5",
      section_bg_color: "#1c1c1e",
      section_header_text_color: "#98979d",
      subtitle_text_color: "#98979d",
      destructive_text_color: "#ff453a",
    },
  },
  // Distinctive non-default accent to prove custom Telegram themes apply.
  custom: {
    colorScheme: "light",
    params: {
      bg_color: "#fff8e7",
      secondary_bg_color: "#fff1c9",
      text_color: "#3b2f00",
      hint_color: "#8a7b45",
      link_color: "#b8860b",
      button_color: "#b8860b",
      button_text_color: "#ffffff",
      header_bg_color: "#fff8e7",
      accent_text_color: "#b8860b",
      section_bg_color: "#fffdf5",
      section_header_text_color: "#8a7b45",
      subtitle_text_color: "#8a7b45",
      destructive_text_color: "#c0392b",
    },
  },
} as const;

export type ThemeName = keyof typeof THEME_PRESETS;

const STUB_SOURCE = `
(() => {
  if (window.__tgInstalled) return;
  window.__tgInstalled = true;

  const PRESETS = ${JSON.stringify(THEME_PRESETS)};
  const CURRENT = { name: "light" };

  const counters = { ready: 0, expand: 0, haptic: 0 };
  const chromeCalls = { setHeaderColor: [], setBottomBarColor: [] };
  const events = {};
  const backButton = { shown: false, handler: null };

  const webApp = {
    initData: "e2e-init-data",
    initDataUnsafe: { user: { id: 999999, first_name: "Test", last_name: "User" } },
    user: {
      id: 999999,
      first_name: "Test",
      last_name: "User",
      username: "e2e",
      language_code: "ru",
    },
    language: "ru",
    platform: "android",
    version: "8.5.0",
    colorScheme: PRESETS.light.colorScheme,
    themeParams: PRESETS.light.params,
    viewportInfo: { contentTop: 0, contentBottom: 0, contentWidth: 390, contentHeight: 844, strokeWidth: 0 },
    safeAreaInset: { top: 0, right: 0, bottom: 0, left: 0 },
    isClosing: "regular",
    isVerticalSwipesEnabled: false,

    ready() { counters.ready += 1; },
    expand() { counters.expand += 1; },
    onEvent(name, cb) { (events[name] = events[name] || []).push(cb); },
    offEvent(name, cb) {
      events[name] = (events[name] || []).filter((f) => f !== cb);
    },
    setHeaderColor(c) { chromeCalls.setHeaderColor.push(c); },
    setBottomBarColor(c) { chromeCalls.setBottomBarColor.push(c); },
    setViewMode() {},
    enableClosing() {},
    disableClosing() {},
    showPopup() { return Promise.resolve(); },
    hidePopup() {},
    showConfirm() { return Promise.resolve({ confirmed: true }); },
    showAlert() { return Promise.resolve(); },
    openLink() {},
    openTelegramLink() {},
    replaceHistory() {},
    setHeaderColorFallback() {},

    BackButton: {
      show() { backButton.shown = true; },
      hide() { backButton.shown = false; },
      onClick(cb) { backButton.handler = cb; },
      onEvent(name, cb) { if (name === "click") backButton.handler = cb; },
      offEvent() { backButton.handler = null; },
    },
    HapticFeedback: {
      impactOccurred() { counters.haptic += 1; },
      notificationOccurred() { counters.haptic += 1; },
      selectionStarted() { counters.haptic += 1; },
    },
    MainButton: {
      show() {}, hide() {}, setText() {},
      onClick(cb) { this._cb = cb; },
      onEvent(name, cb) { if (name === "click") this._cb = cb; },
      offEvent() {},
    },
  };

  window.Telegram = { WebApp: webApp };

  window.__tg = {
    counters,
    chromeCalls,
    presets: PRESETS,
    currentName() { return CURRENT.name; },
    setTheme(name) {
      const preset = PRESETS[name];
      if (!preset) throw new Error("unknown theme " + name);
      CURRENT.name = name;
      webApp.themeParams = preset.params;
      webApp.colorScheme = preset.colorScheme;
      (events.themeChanged || []).forEach((cb) => cb(preset.params));
      return preset;
    },
    fireBackButton() { if (backButton.handler) backButton.handler(); },
    setLanguage(lang) { webApp.language = lang; webApp.user.language_code = lang; },
    backButtonShown() { return backButton.shown; },
  };
})();
`;

/**
 * Install the stub on a page: block the CDN + inject the harness before any
 * app module runs. Call once per page (before navigating to the app).
 */
export async function installTelegramStub(page: Page): Promise<void> {
  await page.route("https://telegram.org/**", (route) => route.abort());
  await page.addInitScript(STUB_SOURCE);
}

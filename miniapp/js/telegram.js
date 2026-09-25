/* Single, defensive access point to the Telegram WebApp SDK.
 *
 * Works in two environments:
 *   - the real Telegram client (window.Telegram.WebApp is present), and
 *   - a plain browser / Playwright Chromium (the CDN script never loaded,
 *     so everything below degrades to no-ops).
 *
 * No other module may read window.Telegram directly.
 */

const tg = (typeof window !== "undefined" && window.Telegram && window.Telegram.WebApp) || null;

/** Raw initData string, sent as X-Telegram-Init-Data. Empty outside Telegram. */
export const INIT_DATA = tg ? tg.initData || "" : "";

/** Map of Telegram themeParams fields -> CSS custom property names. */
const THEME_VAR_MAP = {
  bg_color: "--tg-theme-bg-color",
  secondary_bg_color: "--tg-theme-secondary-bg-color",
  text_color: "--tg-theme-text-color",
  hint_color: "--tg-theme-hint-color",
  link_color: "--tg-theme-link-color",
  button_color: "--tg-theme-button-color",
  button_text_color: "--tg-theme-button-text-color",
  header_bg_color: "--tg-theme-header-bg-color",
  accent_text_color: "--tg-theme-accent-text-color",
  section_bg_color: "--tg-theme-section-bg-color",
  section_header_text_color: "--tg-theme-section-header-text-color",
  subtitle_text_color: "--tg-theme-subtitle-text-color",
  destructive_text_color: "--tg-theme-destructive-text-color",
};

/** Apply the current Telegram themeParams as CSS variables on :root. */
export function applyTheme() {
  const params = tg && tg.themeParams;
  if (!params) return;
  const rootStyle = document.documentElement.style;
  for (const [param, cssVar] of Object.entries(THEME_VAR_MAP)) {
    if (typeof params[param] === "string" && params[param]) {
      rootStyle.setProperty(cssVar, params[param]);
    }
  }
  const scheme =
    tg && tg.colorScheme === "dark" ? "dark" : tg && tg.colorScheme === "light" ? "light" : null;
  document.documentElement.dataset.colorScheme = scheme || "";
  document.documentElement.style.setProperty(
    "--tg-theme-bg-color",
    params.bg_color || rootStyle.getPropertyValue("--tg-theme-bg-color")
  );
  syncChrome();
}

/**
 * Chrome color sync (V5 §13): push the active theme colors to the native
 * Telegram chrome — the header bar (header_bg_color), the bottom bar /
 * bottom-button area (section_bg_color) — and to the browser's
 * `<meta name="theme-color">`. No-op outside a real client.
 */
export function syncChrome() {
  const params = tg && tg.themeParams;
  if (!params) return;
  try {
    if (params.header_bg_color && typeof tg.setHeaderColor === "function")
      tg.setHeaderColor(params.header_bg_color);
  } catch {
    /* no header color support */
  }
  try {
    if (params.section_bg_color && typeof tg.setBottomBarColor === "function")
      tg.setBottomBarColor(params.section_bg_color);
  } catch {
    /* no bottom-bar color support */
  }
  try {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta && params.bg_color) meta.setAttribute("content", params.bg_color);
  } catch {
    /* no theme-color meta (plain HTML) */
  }
}

/**
 * Apply the client viewport metrics (V5 §11, §12; V5.1 P0 #4/#5):
 *  - the four safe-area insets as px custom properties
 *    (--safe-top/right/bottom/left),
 *  - the four CONTENT safe-area insets as a distinct token group
 *    (--tg-content-safe-top/right/bottom/left) from `contentSafeAreaInset`
 *    — the insets the content region must respect, which can differ from
 *    the webview-wide `safeAreaInset`,
 *  - a stable viewport height (--tg-viewport-stable-height) from
 *    `viewportStableHeight` (the dedicated SDK property; older clients
 *    fall back to `viewportInfo.contentHeight`) that does not jump when
 *    the on-screen keyboard appears.
 * In a plain browser the CSS env()/100dvh fallbacks apply.
 */
export function applyViewport() {
  if (!tg) return;
  const rootStyle = document.documentElement.style;
  const inset = tg.safeAreaInset;
  if (inset && typeof inset === "object") {
    for (const [edge, cssVar] of [
      ["top", "--safe-top"],
      ["right", "--safe-right"],
      ["bottom", "--safe-bottom"],
      ["left", "--safe-left"],
    ]) {
      const px = Number(inset[edge]);
      if (Number.isFinite(px)) rootStyle.setProperty(cssVar, `${px}px`);
    }
  }
  const contentInset = tg.contentSafeAreaInset;
  if (contentInset && typeof contentInset === "object") {
    for (const [edge, cssVar] of [
      ["top", "--tg-content-safe-top"],
      ["right", "--tg-content-safe-right"],
      ["bottom", "--tg-content-safe-bottom"],
      ["left", "--tg-content-safe-left"],
    ]) {
      const px = Number(contentInset[edge]);
      if (Number.isFinite(px)) rootStyle.setProperty(cssVar, `${px}px`);
    }
  }
  let h = Number(tg.viewportStableHeight);
  if (!(Number.isFinite(h) && h > 0)) {
    h = tg.viewportInfo ? Number(tg.viewportInfo.contentHeight) : NaN;
  }
  if (Number.isFinite(h) && h > 0) rootStyle.setProperty("--tg-viewport-stable-height", `${h}px`);
}

/**
 * Subscribe to any Telegram WebApp event (V5.1 P0 #6). `handler` receives the
 * event payload; the return value is a disposer that detaches the listener.
 * No-op in a plain browser (returns an empty disposer).
 */
export function onTelegramEvent(name, handler) {
  if (!tg || typeof tg.onEvent !== "function") return () => {};
  try {
    tg.onEvent(name, handler);
  } catch {
    return () => {};
  }
  return () => {
    try {
      tg.offEvent(name, handler);
    } catch {
      /* already detached */
    }
  };
}

/** Register a callback for runtime Telegram viewport changes. No-op in browser. */
export function onViewportChanged(callback) {
  return onTelegramEvent("viewportChanged", () => {
    applyViewport();
    callback();
  });
}

/** Re-apply the safe-area tokens on runtime changes (V5.1 P0 #6). No-op in browser. */
export function onSafeAreaChanged(callback) {
  return onTelegramEvent("safeAreaChanged", () => {
    applyViewport();
    callback();
  });
}

/** Re-apply the content safe-area tokens on runtime changes (V5.1 P0 #6). No-op in browser. */
export function onContentSafeAreaChanged(callback) {
  return onTelegramEvent("contentSafeAreaChanged", () => {
    applyViewport();
    callback();
  });
}

/** Register a callback for runtime Telegram theme changes. No-op in browser. */
export function onThemeChanged(callback) {
  return onTelegramEvent("themeChanged", () => {
    applyTheme();
    callback();
  });
}

/**
 * Boot lifecycle (V5 §13, V5.1 P0 #8): `expand()` is called EARLY (the
 * client immediately gives the full viewport), and `ready()` is called
 * exactly once AFTER visible UI is on screen — so the client never paints
 * an empty shell and knows the app is interactive.
 */
export function webAppReady() {
  if (!tg) return;
  try {
    tg.ready();
  } catch {
    /* SDK not ready yet; harmless */
  }
}

export function webAppExpand() {
  if (!tg) return;
  try {
    tg.expand();
  } catch {
    /* expand unsupported in this client */
  }
}

/** Signal ready AND expand (kept for callers that do not need the split). */
export function initWebApp() {
  webAppReady();
  webAppExpand();
}

/**
 * Closing confirmation (V5.1 P0 #2): while enabled, the client asks the
 * user to confirm before closing the app — used while a form has unsaved
 * changes. Real SDK `enableClosingConfirmation()`; no-op in a plain
 * browser or on clients without the API.
 */
export function enableClosingConfirmation() {
  try {
    tg?.enableClosingConfirmation?.();
  } catch {
    /* unsupported */
  }
}

export function disableClosingConfirmation() {
  try {
    tg?.disableClosingConfirmation?.();
  } catch {
    /* unsupported */
  }
}

/** Current Telegram user object, or null outside Telegram. */
export function tgUser() {
  return tg && tg.user ? tg.user : null;
}

/**
 * Client language reported by Telegram (ru/en/...), or null. Prefers the
 * authoritative `initDataUnsafe.user.language_code` (V5 §13), then the
 * decoded `user` object, then the top-level `language` field.
 */
export function tgLanguage() {
  if (!tg) return null;
  return (
    (tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.language_code) ||
    (tg.user && tg.user.language_code) ||
    tg.language ||
    null
  );
}

/* Telegram BackButton (real client only; no-op everywhere else). */
export function backButtonShow() {
  try {
    tg?.BackButton?.show();
  } catch {
    /* no BackButton support */
  }
}

export function backButtonHide() {
  try {
    tg?.BackButton?.hide();
  } catch {
    /* no BackButton support */
  }
}

export function backButtonOn(onClick) {
  if (!tg || !tg.BackButton || typeof tg.BackButton.onClick !== "function") return;
  try {
    tg.BackButton.onClick(onClick);
  } catch {
    /* ignore */
  }
}

/* Haptic feedback (real client only). */
export function haptic(kind = "light") {
  try {
    const h = tg && tg.HapticFeedback;
    if (!h) return;
    if (kind === "notification" && typeof h.notificationOccurred === "function") {
      h.notificationOccurred("success");
    } else if (typeof h.impactOccurred === "function") {
      h.impactOccurred(kind);
    }
  } catch {
    /* haptics unavailable */
  }
}

/** Current client platform string ("ios" | "android" | ...), or null. */
export function tgPlatform() {
  return tg && tg.platform ? tg.platform : null;
}

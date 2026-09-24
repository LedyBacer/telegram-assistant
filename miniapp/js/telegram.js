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
 * Apply the client viewport metrics (V5 §11, §12): the four safe-area insets
 * as px custom properties (--safe-top/right/bottom/left) and a stable
 * viewport height (--tg-viewport-stable-height, from viewportInfo
 * contentHeight) that does not jump when the on-screen keyboard appears.
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
  const info = tg.viewportInfo;
  const h = info && Number(info.contentHeight);
  if (Number.isFinite(h) && h > 0) rootStyle.setProperty("--tg-viewport-stable-height", `${h}px`);
}

/** Register a callback for runtime Telegram viewport changes. No-op in browser. */
export function onViewportChanged(callback) {
  if (!tg || typeof tg.onEvent !== "function") return () => {};
  const handler = () => {
    applyViewport();
    callback();
  };
  try {
    tg.onEvent("viewportChanged", handler);
  } catch {
    return () => {};
  }
  return () => {
    try {
      tg.offEvent("viewportChanged", handler);
    } catch {
      /* already detached */
    }
  };
}

/** Register a callback for runtime Telegram theme changes. No-op in browser. */
export function onThemeChanged(callback) {
  if (!tg || typeof tg.onEvent !== "function") return () => {};
  const handler = () => {
    applyTheme();
    callback();
  };
  try {
    tg.onEvent("themeChanged", handler);
  } catch {
    return () => {};
  }
  return () => {
    try {
      tg.offEvent("themeChanged", handler);
    } catch {
      /* already detached */
    }
  };
}

/**
 * ready()/expand() split (V5 §13): the app signals `ready()` at boot, and
 * requests `expand()` only once real content is on screen — so the client
 * grows the viewport to the content, not to an empty shell (avoids the
 * initial half-height flash).
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

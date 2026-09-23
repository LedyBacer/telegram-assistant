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

/** Signal that the Mini App is ready and expand the viewport (if supported). */
export function initWebApp() {
  if (!tg) return;
  try {
    tg.ready();
  } catch {
    /* SDK not ready yet; harmless */
  }
  try {
    tg.expand();
  } catch {
    /* expand unsupported in this client */
  }
}

/** Current Telegram user object, or null outside Telegram. */
export function tgUser() {
  return tg && tg.user ? tg.user : null;
}

/** Client language reported by Telegram (ru/en/...), or null. */
export function tgLanguage() {
  return tg && tg.language ? tg.language : null;
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

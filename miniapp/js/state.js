/* Shared application state: identity, settings, language, locale dictionary,
 * and the active section. Kept deliberately flat and tiny so a future reader
 * (or a small LLM) can reason about the whole app from one file.
 */

import { api } from "./api.js";

export const state = {
  me: null, // { user, settings }
  lang: "ru", // active UI language (ru | en)
  tab: "today",
  month: null, // { y, m } cursor for the calendar (m is 0-based)
  selectedDate: null, // "YYYY-MM-DD" local date for the calendar
};

/** Flat locale dictionary for the active language, loaded from the API. */
export let STR = {};

/**
 * Translate a key with {param} interpolation. Falls back to the key itself so
 * a missing key is obvious in tests and never renders "undefined".
 */
export function S(key, params) {
  let text = STR[key];
  if (text === undefined) text = key;
  return String(text).replace(/\{(\w+)\}/g, (m, k) =>
    params && params[k] !== undefined ? String(params[k]) : m
  );
}

/**
 * Load the locale dictionary for a language and activate it.
 * @param {string} lang
 */
export async function setLanguage(lang) {
  STR = await api(`/api/v1/i18n/${lang}`);
  state.lang = lang;
  document.documentElement.lang = lang;
}

/** Bootstrap: fetch identity+settings, then the matching locale. */
export async function loadMe() {
  const me = await api("/api/v1/me");
  state.me = me;
  const lang = me.settings.language || "ru";
  await setLanguage(lang);
  return me;
}

/** Locale code for Intl/Flatpickr (ru / en). */
export function localeCode() {
  return state.lang === "en" ? "en" : "ru";
}

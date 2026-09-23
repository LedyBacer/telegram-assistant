/* Small UI kit: safe DOM builder, toasts, bottom sheet / action sheet,
 * confirm dialog, Flatpickr wrappers, and standard screen states.
 *
 * Rendering rule (regression guard for the [object HTMLDivElement] bug):
 *   - el() appends Node children AS nodes; everything else is converted to a
 *     text node. innerHTML is never used, so a DOM node can never be
 *     accidentally stringified into "[object HTMLDivElement]".
 */

import { localeCode, S } from "./state.js";
import { haptic } from "./telegram.js";
import { fmtDT, userWallAsBrowserDate } from "./time.js";

export { fmtDT };

/* ------------------------------------------------------------------ */
/* Safe DOM builder                                                    */
/* ------------------------------------------------------------------ */

function appendChildren(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined) continue;
    if (child instanceof Node) node.appendChild(child);
    else node.appendChild(document.createTextNode(String(child)));
  }
}

/**
 * Create an element.
 * @param {string} tag
 * @param {object} [attrs]  class, dataset, on* listeners, other attributes
 * @param {...any} children nodes, arrays, strings (rendered as text)
 */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined) continue;
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function")
      node.addEventListener(key.slice(2).toLowerCase(), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  appendChildren(node, children);
  return node;
}

export const card = (...children) => el("div", { class: "card" }, ...children);
export const badge = (text, tone = "normal") => el("span", { class: `badge badge-${tone}` }, text);

export function btn(label, onClick, { variant = "ghost", disabled = false, ariaLabel } = {}) {
  return el(
    "button",
    {
      type: "button",
      class: `btn btn-${variant}`,
      disabled: disabled || null,
      "aria-label": ariaLabel || null,
      onclick: onClick,
    },
    label
  );
}

export function field(labelText, control) {
  return el("label", { class: "field" }, el("span", { class: "field-label" }, labelText), control);
}

export function input(attrs = {}) {
  return el("input", Object.assign({ type: "text", class: "field-input" }, attrs));
}

/* ------------------------------------------------------------------ */
/* Standard screen states                                              */
/* ------------------------------------------------------------------ */

export const loading = () =>
  el("div", { class: "state state-loading", role: "status", "aria-busy": "true" },
    el("span", { class: "spinner", "aria-hidden": "true" }));

export const empty = (message) =>
  el("div", { class: "state state-empty" }, el("p", {}, message));

export const errorState = (message, onRetry) =>
  el("div", { class: "state state-error" },
    el("p", { class: "state-error-text" }, message),
    onRetry ? btn(S("miniapp.retry"), () => {
      haptic();
      onRetry();
    }, { variant: "primary" }) : null);

/* ------------------------------------------------------------------ */
/* Toast                                                               */
/* ------------------------------------------------------------------ */

let toastTimer = 0;
export function toast(message, kind = "default") {
  const node = document.getElementById("toast");
  if (!node) return;
  node.textContent = message;
  node.className = `toast toast-${kind}`;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    node.hidden = true;
  }, 2500);
}

/* ------------------------------------------------------------------ */
/* Bottom sheet / action sheet                                         */
/* ------------------------------------------------------------------ */

/**
 * Open a Telegram-like bottom sheet with a short option list.
 * @param {object} opts
 * @param {string} [opts.title]
 * @param {Array<{value:string,label:string}>} opts.options
 * @param {string} [opts.value] currently selected value (shows the check)
 * @returns {Promise<string|null>} chosen value, or null when closed/cancelled
 */
export function openSheet({ title, options, value = null }) {
  return new Promise((resolve) => {
    const root = document.getElementById("sheet-root");
    if (!root) return resolve(null);
    const previouslyFocused = document.activeElement;

    let resolved = false;
    const close = (result) => {
      if (resolved) return;
      resolved = true;
      document.removeEventListener("keydown", onKey, true);
      scrim.remove();
      if (previouslyFocused && previouslyFocused.focus) previouslyFocused.focus();
      resolve(result);
    };

    const rows = options.map((opt) =>
      el(
        "button",
        {
          type: "button",
          role: "option",
          class: `sheet-row${opt.value === value ? " is-selected" : ""}`,
          "aria-selected": opt.value === value ? "true" : "false",
          "data-value": opt.value,
          onclick: () => {
            haptic();
            close(opt.value);
          },
        },
        el("span", { class: "sheet-row-label" }, opt.label),
        el("span", { class: "sheet-row-check", "aria-hidden": "true" }, opt.value === value ? "✓" : "")
      )
    );

    const panel = el(
      "div",
      { class: "sheet", role: "dialog", "aria-modal": "true", "aria-label": title || "sheet", tabindex: "-1" },
      title ? el("div", { class: "sheet-title" }, title) : null,
      el("div", { class: "sheet-options", role: "listbox" }, rows),
      el("div", { class: "sheet-cancel-wrap" },
        btn(S("miniapp.sheet_cancel"), () => close(null), { variant: "ghost" })
      )
    );
    const scrim = el("div", { class: "sheet-scrim" }, panel);
    scrim.addEventListener("mousedown", (e) => {
      if (e.target === scrim) close(null);
    });

    const onKey = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        close(null);
      } else if (e.key === "Tab") {
        // Keep focus inside the sheet.
        const focusables = [...panel.querySelectorAll("button:not([disabled])")];
        if (!focusables.length) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey, true);

    root.appendChild(scrim);
    const selected = panel.querySelector(".sheet-row.is-selected") || panel.querySelector(".sheet-row");
    (selected || panel).focus();
  });
}

/**
 * Confirmation dialog (delete and similar destructive actions).
 * @param {string} message
 * @param {string} [confirmLabel]
 * @param {string} [cancelLabel]
 * @returns {Promise<boolean>}
 */
export function confirmDialog(message, confirmLabel, cancelLabel) {
  return new Promise((resolve) => {
    const root = document.getElementById("sheet-root");
    if (!root) return resolve(false);
    const previouslyFocused = document.activeElement;
    let resolved = false;
    const close = (result) => {
      if (resolved) return;
      resolved = true;
      document.removeEventListener("keydown", onKey, true);
      scrim.remove();
      if (previouslyFocused && previouslyFocused.focus) previouslyFocused.focus();
      resolve(result);
    };
    const onKey = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        close(false);
      }
    };
    document.addEventListener("keydown", onKey, true);

    const confirmBtn = btn(confirmLabel || S("miniapp.btn_delete"), () => close(true), {
      variant: "danger",
    });
    const cancelBtn = btn(cancelLabel || S("miniapp.btn_cancel"), () => close(false), {
      variant: "ghost",
    });
    const panel = el(
      "div",
      { class: "sheet sheet-confirm", role: "alertdialog", "aria-modal": "true", "aria-label": message, tabindex: "-1" },
      el("div", { class: "sheet-confirm-text" }, message),
      el("div", { class: "sheet-confirm-actions" }, cancelBtn, confirmBtn)
    );
    const scrim = el("div", { class: "sheet-scrim" }, panel);
    scrim.addEventListener("mousedown", (e) => {
      if (e.target === scrim) close(false);
    });
    root.appendChild(scrim);
    confirmBtn.focus();
  });
}

/* ------------------------------------------------------------------ */
/* Flatpickr wrappers (date / datetime / time)                         */
/* ------------------------------------------------------------------ */

function flatpickrLocale() {
  return localeCode() === "ru" ? "ru" : "default";
}

function hasFlatpickr() {
  return typeof window !== "undefined" && typeof window.flatpickr === "function";
}

/**
 * Pick a date and time (24h, localized). Resolves ISO string or null.
 * @param {string|null} [initialISO]
 */
export function pickDateTime(initialISO = null) {
  return new Promise((resolve) => {
    const holder = el("div", { class: "picker-holder" });
    const inputNode = el("input", {
      class: "field-input picker-input",
      readonly: "readonly",
      placeholder: S("miniapp.pick_datetime"),
      "aria-label": S("miniapp.pick_datetime"),
    });
    holder.appendChild(inputNode);
    document.body.appendChild(holder);

    const fallback = () => {
      holder.remove();
      resolve(null);
    };
    if (!hasFlatpickr()) return fallback();

    let fp;
    try {
      fp = window.flatpickr(inputNode, {
        enableTime: true,
        time24hour: true,
        allowInput: false,
        dateFormat: "Y-m-d H:i",
        locale: flatpickrLocale(),
        // Seed the (browser-local) picker with the USER-TZ wall clock of
        // the instant, so a user in a foreign browser zone still sees and
        // edits their own wall time. The onClose "Y-m-d H:i" string is then
        // exactly the naive user-TZ wall clock the backend expects.
        defaultDate: initialISO ? (userWallAsBrowserDate(initialISO) || new Date()) : new Date(),
        onClose: (dates, str) => {
          // Defer destroy(): onClose fires from inside flatpickr's own
          // close(), which keeps referencing calendarContainer after we
          // return. Destroying synchronously there throws on the null node.
          setTimeout(() => {
            try {
              fp.destroy();
            } catch {
              /* already destroyed */
            }
          }, 0);
          holder.remove();
          // Naive "YYYY-MM-DD HH:MM" in the user's timezone (no offset).
          resolve(str ? str.replace(" ", "T") : null);
        },
      });
    } catch {
      fallback();
      return;
    }
    fp.open();
  });
}

/**
 * Pick a time of day (24h). Resolves "HH:MM" or null.
 * @param {string} [initial] e.g. "08:00"
 */
export function pickTime(initial = null) {
  return new Promise((resolve) => {
    const holder = el("div", { class: "picker-holder" });
    const inputNode = el("input", {
      class: "field-input picker-input",
      readonly: "readonly",
      placeholder: S("miniapp.pick_time"),
      "aria-label": S("miniapp.pick_time"),
    });
    holder.appendChild(inputNode);
    document.body.appendChild(holder);

    const fallback = () => {
      holder.remove();
      resolve(null);
    };
    if (!hasFlatpickr()) return fallback();

    let fp;
    try {
      fp = window.flatpickr(inputNode, {
        enableTime: true,
        noCalendar: true,
        time24hour: true,
        allowInput: false,
        dateFormat: "H:i",
        locale: flatpickrLocale(),
        defaultDate: initial ? new Date(`1970-01-01T${initial}:00`) : new Date(),
        onClose: (dates, str) => {
          // Defer destroy(): see pickDateTime — destroying synchronously from
          // inside flatpickr's close() throws on the nulled calendarContainer.
          setTimeout(() => {
            try {
              fp.destroy();
            } catch {
              /* already destroyed */
            }
          }, 0);
          holder.remove();
          resolve(str || null);
        },
      });
    } catch {
      fallback();
      return;
    }
    fp.open();
  });
}

/* ------------------------------------------------------------------ */
/* Formatting helpers                                                  */
/* ------------------------------------------------------------------ */

export function fmtBytes(n) {
  if (!Number.isFinite(n)) return "";
  const nf = new Intl.NumberFormat(localeCode(), { maximumFractionDigits: 1 });
  if (n < 1024) return S("miniapp.size_bytes", { n: nf.format(n) });
  if (n < 1024 * 1024) return S("miniapp.size_kb", { n: nf.format(n / 1024) });
  return S("miniapp.size_mb", { n: nf.format(n / 1024 / 1024) });
}

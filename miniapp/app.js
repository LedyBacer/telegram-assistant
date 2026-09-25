/* Smart Assistant Mini App — entry point (vanilla ES modules, no build).
 *
 * Structure: js/telegram.js (only window.Telegram access) · js/api.js (API
 * client) · js/state.js (state + i18n) · js/ui.js (DOM/UI kit). This file
 * owns navigation and the section views. Every screen renders through the
 * safe el() builder; user content is always appended as text.
 */

import { api, apiUpload } from "./js/api.js";
import {
  applyTheme,
  applyViewport,
  onThemeChanged,
  onViewportChanged,
  webAppReady,
  webAppExpand,
  backButtonShow,
  backButtonHide,
  backButtonOn,
  haptic,
} from "./js/telegram.js";
import { state, S, loadMe, setLanguage, localeCode } from "./js/state.js";
import {
  el,
  card,
  badge,
  btn,
  field,
  input,
  loading,
  empty,
  errorState,
  toast,
  openSheet,
  confirmDialog,
  pickDateTime,
  pickTime,
  fmtBytes,
} from "./js/ui.js";
import {
  wallParts,
  dateKeyTZ,
  todayKey,
  monthStartUTC,
  isoToWall,
  fmtDT,
  fmtWall,
} from "./js/time.js";

const viewEl = document.getElementById("view");
const navEl = document.getElementById("nav");
const whoEl = document.getElementById("who");

// Primary bottom-nav tabs (V5 §16). The rest live in the "More" sheet so the
// bar stays at five touch targets instead of eight.
const TABS = [
  ["today", "miniapp.tab_today"],
  ["actions", "miniapp.tab_actions"],
  ["new", "miniapp.tab_new"],
  ["files", "miniapp.tab_files"],
];
const MORE_TABS = [
  ["upcoming", "miniapp.tab_upcoming"],
  ["workouts", "miniapp.tab_workouts"],
  ["facts", "miniapp.tab_facts"],
  ["settings", "miniapp.tab_settings"],
];

// Fallback for browsers without Intl.supportedValuesOf (Chrome 93+,
// Safari 15.4+, Firefox 93+).
const FALLBACK_TIMEZONES = [
  "UTC",
  "Europe/Berlin",
  "Europe/Madrid",
  "Europe/Paris",
  "Europe/London",
  "Europe/Kyiv",
  "Europe/Moscow",
  "America/New_York",
  "America/Chicago",
  "America/Los_Angeles",
  "Asia/Tokyo",
  "Asia/Shanghai",
  "Asia/Karachi",
  "Asia/Dubai",
  "Australia/Sydney",
];

/**
 * Canonical IANA timezone list for the picker (V3 P35): the browser's own
 * list (400+ zones, no network, always valid IANA names), plus the user's
 * currently configured zone when the browser does not know it.
 */
function timezoneOptions() {
  let zones;
  try {
    zones = Intl.supportedValuesOf("timeZone");
  } catch {
    zones = FALLBACK_TIMEZONES;
  }
  const current = state.me && state.me.settings && state.me.settings.timezone;
  return current && !zones.includes(current) ? [current, ...zones] : zones;
}

const PRIORITY_TONES = { high: "high", normal: "normal", low: "low" };
const PRIORITY_LABELS = {
  high: "miniapp.new_high",
  normal: "miniapp.new_normal",
  low: "miniapp.new_low",
};

/* ------------------------------------------------------------------ */
/* Navigation                                                          */
/* ------------------------------------------------------------------ */

function buildNav() {
  const buttons = TABS.map(([key, keyStr]) =>
    el(
      "button",
      {
        type: "button",
        class: `nav-btn${key === state.tab ? " is-active" : ""}`,
        "aria-current": key === state.tab ? "page" : null,
        "data-tab": key,
        onclick: () => {
          if (state.tab === key) return;
          navigate(key);
        },
      },
      el("span", { class: "nav-icon", "aria-hidden": "true" }, iconFor(key)),
      el("span", { class: "nav-label" }, S(keyStr))
    )
  );
  // The "More" launcher opens a sheet of the secondary tabs (V5 §16). It is a
  // launcher, not a view: it never becomes state.tab, so it is never active.
  buttons.push(
    el(
      "button",
      {
        type: "button",
        class: "nav-btn",
        "data-tab": "more",
        "aria-haspopup": "true",
        "aria-label": S("miniapp.tab_more"),
        onclick: () => openMoreSheet(),
      },
      el("span", { class: "nav-icon", "aria-hidden": "true" }, iconFor("more")),
      el("span", { class: "nav-label" }, S("miniapp.tab_more"))
    )
  );
  navEl.replaceChildren(...buttons);
  updateBackButton();
}

/** Emojis are part of the localized tab strings; keep an iconless fallback. */
function iconFor(key) {
  if (key === "more") return "⋮";
  const label = S(TABS.find(([k]) => k === key)?.[1] ?? key);
  const match = label.match(/^(\p{Extended_Pictographic}+)/u);
  return match ? match[1] : "•";
}

/** Open the "More" sheet of secondary tabs; navigate when one is picked. */
async function openMoreSheet() {
  const chosen = await openSheet({
    title: S("miniapp.tab_more"),
    options: MORE_TABS.map(([key, keyStr]) => ({ value: key, label: S(keyStr) })),
    value: state.tab,
  });
  if (chosen) navigate(chosen);
}

function updateBackButton() {
  // V5 §15: the Telegram BackButton is a nested-nav affordance — shown only on
  // the Edit view (pushed from a list), not on the top-level tabs.
  if (state.tab === "edit") backButtonShow();
  else backButtonHide();
}

/* ------------------------------------------------------------------ */
/* View dispatch                                                       */
/* ------------------------------------------------------------------ */

const VIEWS = {
  today: viewToday,
  actions: viewActions,
  upcoming: viewUpcoming,
  new: viewNew,
  edit: viewEdit,
  workouts: viewWorkouts,
  files: viewFiles,
  facts: viewFacts,
  settings: viewSettings,
};

// Render generation (V3 P27): every render() owns a monotonic generation
// token and an AbortController. An async view may commit its DOM only if its
// generation is still the active one — a response that resolves after the
// user switched tabs must not overwrite the newer screen. Stale in-flight
// requests are aborted.
let renderGeneration = 0;
let renderAbort = null;

const isStale = (gen) => gen !== renderGeneration;

async function render() {
  renderGeneration += 1;
  renderAbort?.abort();
  renderAbort = new AbortController();
  const gen = renderGeneration;
  const signal = renderAbort.signal;
  buildNav();
  viewEl.replaceChildren(loading());
  try {
    await VIEWS[state.tab](viewEl, gen, signal);
  } catch (e) {
    if (isStale(gen)) return;
    const key = e && e.status === 401 ? "miniapp.status_auth" : "miniapp.error_load";
    viewEl.replaceChildren(errorState(S(key), () => render()));
  }
}

/* ------------------------------------------------------------------ */
/* Dirty-form protection (V5 §15)                                      */
/* ------------------------------------------------------------------ */

// True while the user has edited the New/Edit form since it last rendered.
let formDirty = false;
function markDirty() {
  formDirty = true;
}
const isFormView = (tab) => tab === "new" || tab === "edit";

// The single navigation path (V5 §15): the bottom nav, an item's "Edit"
// button, the Telegram BackButton and a successful save all route through
// here. Leaving a dirty form asks for a discard confirmation; `force`
// bypasses it (used right after a successful save, when nothing is unsaved).
async function navigate(tab, { force = false } = {}) {
  if (tab === state.tab) return;
  if (!force && isFormView(state.tab) && formDirty) {
    if (!(await confirmDialog(S("miniapp.discard_confirm"), S("miniapp.discard"), S("miniapp.btn_cancel")))) return;
  }
  if (state.tab === "edit") state.editId = null;
  formDirty = false;
  haptic();
  state.tab = tab;
  render();
}

/* ------------------------------------------------------------------ */
/* Items (shared card)                                                 */
/* ------------------------------------------------------------------ */

function itemCard(item) {
  const actions = [];
  if (item.status === "scheduled") {
    actions.push(
      btn(S("miniapp.btn_edit"), () => {
        state.editReturn = state.tab === "edit" ? "today" : state.tab;
        state.editId = item.id;
        navigate("edit");
      })
    );
    actions.push(
      btn(S("miniapp.btn_done"), async () => {
        try {
          await api(`/api/v1/items/${item.id}/complete`, "POST");
          toast(S("miniapp.saved"));
          render();
        } catch {
          toast(S("miniapp.error_generic"), "error");
        }
      }, { variant: "primary" })
    );
    actions.push(
      btn(S("miniapp.btn_cancel"), async () => {
        try {
          await api(`/api/v1/items/${item.id}/cancel`, "POST");
          toast(S("miniapp.saved"));
          render();
        } catch {
          toast(S("miniapp.error_generic"), "error");
        }
      })
    );
  }
  actions.push(
    btn(S("miniapp.btn_delete"), async () => {
      const ok = await confirmDialog(S("miniapp.confirm_delete", { title: item.title }));
      if (!ok) return;
      try {
        await api(`/api/v1/items/${item.id}`, "DELETE");
        toast(S("miniapp.deleted"));
        render();
      } catch {
        toast(S("miniapp.error_generic"), "error");
      }
    }, { variant: "danger" })
  );

  const meta = [];
  if (item.starts_at) meta.push(el("span", {}, S("miniapp.item_starts", { when: fmtDT(item.starts_at) })));
  if (item.ends_at) meta.push(el("span", {}, S("miniapp.item_ends", { when: fmtDT(item.ends_at) })));
  if (item.due_at) meta.push(el("span", {}, S("miniapp.item_due", { when: fmtDT(item.due_at) })));
  if (item.status !== "scheduled") {
    meta.push(badge(S(`miniapp.status_${item.status}`), item.status === "completed" ? "ok" : "muted"));
  }

  const icon =
    item.source === "workout" ? "🏋️" : item.kind === "event" ? "📌" : "✅";
  const iconClass = item.source === "workout" ? "item-icon item-icon--workout" : "item-icon";

  return card(
    el("div", { class: "item-head" },
      el("span", { class: iconClass, "aria-hidden": "true" }, icon),
      el("span", { class: "item-title" }, item.title),
      badge(S(PRIORITY_LABELS[item.priority] || PRIORITY_LABELS.normal), PRIORITY_TONES[item.priority] || "normal")
    ),
    meta.length ? el("div", { class: "item-meta" }, ...meta) : null,
    item.description ? el("p", { class: "item-desc" }, item.description) : null,
    el("div", { class: "item-actions" }, ...actions)
  );
}

/* ------------------------------------------------------------------ */
/* Calendar (Today)                                                    */
/* ------------------------------------------------------------------ */

const pad2 = (n) => String(n).padStart(2, "0");
// Pure calendar-day key (no timezone involved): the month grid already
// works in the user's local calendar, so keys are "YYYY-MM-DD" strings.
const dayKey = (y, m, d) => `${y}-${pad2(m + 1)}-${pad2(d)}`;

// V3 P37: compact at-a-glance summary of the selected day, computed from
// the month items already fetched by viewToday (no extra request).
function daySummary(dayItems) {
  if (!dayItems.length) return null;
  const now = Date.now();
  let scheduled = 0;
  let completed = 0;
  let overdue = 0;
  for (const item of dayItems) {
    if (item.status === "scheduled") {
      scheduled += 1;
      const anchor = item.due_at || item.starts_at;
      if (anchor && new Date(anchor).getTime() < now) overdue += 1;
    } else if (item.status === "completed") {
      completed += 1;
    }
  }
  return card(
    el("h2", { class: "view-subtitle" }, S("miniapp.today_summary")),
    el("div", { class: "summary-chips" },
      badge(S("miniapp.summary_total", { n: dayItems.length })),
      badge(S("miniapp.summary_left", { n: scheduled }), scheduled ? "high" : "muted"),
      badge(S("miniapp.summary_done", { n: completed }), "ok"),
      overdue ? badge(S("miniapp.summary_overdue", { n: overdue }), "error") : null
    )
  );
}

function ensureCalendarState() {
  if (!state.month) {
    const now = wallParts(new Date());
    state.month = { y: now.y, m: now.m - 1 };
  }
  if (!state.selectedDate) state.selectedDate = todayKey();
}

async function viewToday(view, gen, signal) {
  ensureCalendarState();
  const { y, m } = state.month;
  // Month range in the USER's timezone: [midnight of day 1, midnight of
  // the first day of the next month) — exactly the API's [start, end)
  // semantics, so no item on the boundary is lost or duplicated.
  const start = monthStartUTC(y, m);
  const end = monthStartUTC(y, m + 1);
  const items = await api(
    `/api/v1/items?start=${start.toISOString()}&end=${end.toISOString()}`,
    "GET",
    undefined,
    signal
  );
  if (isStale(gen)) return;

  const byDay = new Map();
  for (const item of items) {
    const anchor = item.starts_at || item.due_at;
    if (!anchor) continue;
    const d = new Date(anchor);
    if (Number.isNaN(d.getTime())) continue;
    const key = dateKeyTZ(d);
    if (!byDay.has(key)) byDay.set(key, []);
    byDay.get(key).push(item);
  }

  const grid = buildMonthGrid(y, m, byDay);
  const dayItems = byDay.get(state.selectedDate) || [];
  const [hy, hm, hd] = state.selectedDate.split("-").map(Number);
  const dayHeader = el("h2", { class: "view-subtitle" },
    new Intl.DateTimeFormat(localeCode(), {
      weekday: "long", day: "numeric", month: "long",
    }).format(new Date(hy, hm - 1, hd))
  );

  view.replaceChildren(
    daySummary(dayItems),
    grid,
    dayHeader,
    dayItems.length
      ? el("div", { class: "list" }, ...dayItems.map(itemCard))
      : empty(S("miniapp.calendar_empty_day"))
  );
}

function buildMonthGrid(y, m, byDay) {
  const first = new Date(y, m, 1);
  const firstWeekday = (first.getDay() + 6) % 7; // Monday-based
  const daysInMonth = new Date(y, m + 1, 0).getDate();
  // "Today" in the USER's timezone (the browser may be in another zone).
  const nowKey = todayKey();

  const weekdays = [...Array(7).keys()].map((i) =>
    el("span", { class: "cal-weekday", "aria-hidden": "true" },
      new Intl.DateTimeFormat(localeCode(), { weekday: "narrow" }).format(new Date(2021, 0, 4 + i)))
  );

  const cells = [];
  for (let i = 0; i < firstWeekday; i++) {
    cells.push(el("span", { class: "cal-day is-out", "aria-hidden": "true" }));
  }
  for (let day = 1; day <= daysInMonth; day++) {
    const key = dayKey(y, m, day);
    cells.push(
      el(
        "button",
        {
          type: "button",
          class: [
            "cal-day",
            key === nowKey ? "is-today" : "",
            key === state.selectedDate ? "is-selected" : "",
            byDay.has(key) ? "has-events" : "",
          ].filter(Boolean).join(" "),
          "data-date": key,
          "aria-label": new Intl.DateTimeFormat(localeCode(), { day: "numeric", month: "long", year: "numeric" }).format(new Date(y, m, day)),
          "aria-pressed": key === state.selectedDate ? "true" : "false",
          onclick: () => {
            state.selectedDate = key;
            haptic();
            render();
          },
        },
        el("span", { class: "cal-day-num" }, String(day)),
        el("span", { class: "cal-day-dot", "aria-hidden": "true" })
      )
    );
  }

  const shift = (delta) => {
    const d = new Date(y, m + delta, 1);
    state.month = { y: d.getFullYear(), m: d.getMonth() };
    render();
  };
  const monthLabel = new Intl.DateTimeFormat(localeCode(), { month: "long", year: "numeric" }).format(first);

  return el("div", { class: "calendar" },
    el("div", { class: "cal-header" },
      btn("‹", () => shift(-1), { variant: "ghost", ariaLabel: S("miniapp.cal_prev") }),
      el("span", { class: "cal-month" }, monthLabel),
      btn("›", () => shift(1), { variant: "ghost", ariaLabel: S("miniapp.cal_next") }),
      btn(S("miniapp.cal_today"), () => {
        const now = wallParts(new Date());
        state.month = { y: now.y, m: now.m - 1 };
        state.selectedDate = todayKey();
        haptic();
        render();
      }, { variant: "ghost" })
    ),
    el("div", { class: "cal-weekdays" }, ...weekdays),
    el("div", { class: "cal-grid" }, ...cells)
  );
}

/* ------------------------------------------------------------------ */
/* Upcoming                                                            */
/* ------------------------------------------------------------------ */

async function viewUpcoming(view, gen, signal) {
  const items = await api(
    "/api/v1/calendar/upcoming?days=7",
    "GET",
    undefined,
    signal
  );
  if (isStale(gen)) return;
  view.replaceChildren(
    items.length
      ? el("div", { class: "list" }, ...items.map(itemCard))
      : empty(S("miniapp.empty_upcoming"))
  );
}

/* ------------------------------------------------------------------ */
/* Actions (pending proposals inbox)                                   */
/* ------------------------------------------------------------------ */

const ACTION_KIND_ICONS = {
  create_item: "✅",
  update_item: "✏️",
  complete_item: "✔️",
  cancel_item: "⏸",
  delete_item: "🗑",
  create_reminder: "⏰",
  cancel_reminder: "🔕",
  log_workout: "🏋️",
  schedule_workout: "🏋️",
};

/** Target-entity hint derived from the validated payload (SPEC: inbox must
 * show which item/reminder a proposal acts on). */
function actionTarget(a) {
  const p = a.payload;
  if (!p) return null;
  if (p.item_id !== undefined) return S("miniapp.action_target_item", { id: p.item_id });
  if (p.reminder_id !== undefined) return S("miniapp.action_target_reminder", { id: p.reminder_id });
  return null;
}
const ACTION_STATUS_KEYS = {
  proposed: "miniapp.action_proposed",
  confirmed: "miniapp.action_proposed",
  executed: "miniapp.action_executed",
  rejected: "miniapp.action_rejected",
  expired: "miniapp.action_expired",
};
const ACTION_STATUS_TONES = {
  proposed: "normal",
  confirmed: "normal",
  executed: "ok",
  rejected: "muted",
  expired: "muted",
};

async function viewActions(view, gen, signal) {
  const actions = await api("/api/v1/actions?limit=20", "GET", undefined, signal);
  if (isStale(gen)) return;
  view.replaceChildren(
    actions.length
      ? el("div", { class: "list" }, ...actions.map(actionCard))
      : empty(S("miniapp.actions_empty"))
  );
}

function actionCard(a) {
  const meta = [];
  if (a.expires_at) meta.push(S("miniapp.action_expires", { when: fmtDT(a.expires_at) }));
  const status = el("span", { class: "item-meta" },
    ...meta,
    badge(S(ACTION_STATUS_KEYS[a.status] || a.status), ACTION_STATUS_TONES[a.status] || "muted"));

  const target = actionTarget(a);
  const reason = a.status === "expired" && a.last_error ? a.last_error : null;

  const actions = [];
  if (a.status === "proposed") {
    actions.push(
      btn(S("miniapp.confirm"), async () => {
        try {
          await api(`/api/v1/actions/${a.id}/confirm`, "POST");
          toast(S("miniapp.saved"));
          render();
        } catch (e) {
          // 409 = stale target (server already expired the action): surface
          // the server reason and refresh so the card shows "expired".
          // 400 = payload no longer valid (same outcome).
          if (e && (e.status === 409 || e.status === 400)) {
            toast(S("miniapp.action_stale_detail", { reason: e.message }), "error");
          } else {
            toast(S("miniapp.error_generic"), "error");
          }
          render();
        }
      }, { variant: "primary" }),
      btn(S("miniapp.reject"), async () => {
        try {
          await api(`/api/v1/actions/${a.id}/reject`, "POST");
          toast(S("miniapp.saved"));
          render();
        } catch {
          toast(S("miniapp.error_generic"), "error");
        }
      })
    );
  }

  return card(
    el("div", { class: "item-head" },
      el("span", { class: "item-icon", "aria-hidden": "true" }, ACTION_KIND_ICONS[a.kind] || "⚡"),
      el("span", { class: "item-title" }, a.summary),
    ),
    status,
    target ? el("p", { class: "item-desc" }, target) : null,
    reason ? el("p", { class: "item-error" }, reason) : null,
    el("div", { class: "item-actions" }, ...actions)
  );
}

/* ------------------------------------------------------------------ */
/* Reminder-offset picker (shared by New and Edit, V4 §28)             */
/* ------------------------------------------------------------------ */

/**
 * Presets plus a custom value, multi-select within the shared backend
 * bounds (SPEC §14.2: max 5, 0..1440 minutes). Offsets are minutes before
 * the start; 0 fires at the start. The local bounds are UX-only sugar —
 * the server re-validates everything and also caps the item's TOTAL
 * reminders, so no validation logic is duplicated beyond these constants.
 * Returns { node, get, clear }.
 */
function reminderPicker() {
  const REMINDER_MAX = 5;
  const REMINDER_MAX_MIN = 1440;
  const offsets = new Set();
  const presetDefs = [
    { value: 0, label: () => S("miniapp.reminder_at_start") },
    { value: 10, label: () => S("miniapp.reminder_preset_10") },
    { value: 30, label: () => S("miniapp.reminder_preset_30") },
    { value: 60, label: () => S("miniapp.reminder_preset_60") },
    { value: 1440, label: () => S("miniapp.reminder_preset_1440") },
  ];
  const isPreset = (o) => presetDefs.some((p) => p.value === o);
  const chips = el("div", { class: "remind-chips" });
  const refreshChips = () => {
    const full = offsets.size >= REMINDER_MAX;
    const presetChips = presetDefs.map((p) => {
      const on = offsets.has(p.value);
      return el("button", {
        type: "button",
        class: `chip${on ? " is-on" : ""}`,
        disabled: full && !on ? true : null,
        "aria-pressed": on ? "true" : "false",
        "data-offset": String(p.value),
        onclick: () => {
          haptic();
          if (on) offsets.delete(p.value);
          else if (offsets.size < REMINDER_MAX) offsets.add(p.value);
          refreshChips();
        },
      }, p.label());
    });
    const customChips = [...offsets]
      .filter((o) => !isPreset(o))
      .sort((a, b) => a - b)
      .map((o) =>
        el("button", {
          type: "button",
          class: "chip is-on chip-custom",
          "aria-pressed": "true",
          "data-offset": String(o),
          "aria-label": S("miniapp.reminder_offset", { n: o }),
          onclick: () => {
            haptic();
            offsets.delete(o);
            refreshChips();
          },
        }, S("miniapp.reminder_min", { n: o }))
      );
    chips.replaceChildren(...presetChips, ...customChips);
  };
  const customInput = input({
    placeholder: S("miniapp.reminder_custom_ph"),
    "aria-label": S("miniapp.reminder_custom_ph"),
    inputmode: "numeric",
    class: "field-input remind-custom",
  });
  const customAdd = btn(S("miniapp.reminder_add"), () => {
    const n = Number(customInput.value.trim());
    if (!Number.isInteger(n) || n < 0 || n > REMINDER_MAX_MIN) {
      toast(S("miniapp.reminder_range"), "error");
      return;
    }
    if (offsets.has(n)) {
      customInput.value = "";
      return;
    }
    if (offsets.size >= REMINDER_MAX) {
      toast(S("miniapp.reminder_limit", { n: REMINDER_MAX }), "error");
      return;
    }
    haptic();
    offsets.add(n);
    customInput.value = "";
    refreshChips();
  });
  refreshChips();
  const node = el("div", { class: "remind-picker" },
    chips,
    el("div", { class: "remind-custom-row" }, customInput, customAdd)
  );
  return {
    node,
    get: () => [...offsets],
    clear: () => {
      offsets.clear();
      customInput.value = "";
      refreshChips();
    },
  };
}

/* ------------------------------------------------------------------ */
/* New task / event                                                    */
/* ------------------------------------------------------------------ */

async function viewNew(view) {
  const formState = {
    title: "",
    description: "",
    kind: "task",
    priority: "normal",
    startsAt: null,
    endsAt: null,
    dueAt: null,
  };

  const titleInput = input({
    placeholder: S("miniapp.new_title_ph"),
    "aria-label": S("miniapp.new_title"),
  });
  const descInput = input({
    placeholder: S("miniapp.new_description_ph"),
    "aria-label": S("miniapp.new_description"),
  });
  // Reminder-offset picker (V3 P31, shared with the Edit view, V4 §28).
  const remindPicker = reminderPicker();

  // V5 §15: a freshly rendered form is clean; typing marks it dirty.
  formDirty = false;
  titleInput.addEventListener("input", markDirty);
  descInput.addEventListener("input", markDirty);

  const kindBtn = pickerBtn(S("miniapp.new_kind"), S("miniapp.new_task"), async () => {
    const chosen = await openSheet({
      title: S("miniapp.new_kind"),
      value: formState.kind,
      options: [
        { value: "task", label: S("miniapp.new_task") },
        { value: "event", label: S("miniapp.new_event") },
      ],
    });
    if (chosen) {
      formState.kind = chosen;
      markDirty();
      kindBtn.querySelector(".picker-value").textContent =
        chosen === "task" ? S("miniapp.new_task") : S("miniapp.new_event");
    }
  });
  const priorityBtn = pickerBtn(S("miniapp.new_priority"), S("miniapp.new_normal"), async () => {
    const chosen = await openSheet({
      title: S("miniapp.new_priority"),
      value: formState.priority,
      options: [
        { value: "low", label: S("miniapp.new_low") },
        { value: "normal", label: S("miniapp.new_normal") },
        { value: "high", label: S("miniapp.new_high") },
      ],
    });
    if (chosen) {
      formState.priority = chosen;
      markDirty();
      priorityBtn.querySelector(".picker-value").textContent = S(PRIORITY_LABELS[chosen]);
    }
  });
  // pickDateTime resolves a NAIVE user-TZ wall clock ("YYYY-MM-DDTHH:MM"),
  // which is exactly what the backend expects (naive == user's timezone).
  const startsBtn = pickerBtn(S("miniapp.new_starts"), S("miniapp.not_set"), async () => {
    const wall = await pickDateTime(formState.startsAt);
    if (wall) {
      formState.startsAt = wall;
      markDirty();
      startsBtn.querySelector(".picker-value").textContent = fmtWall(wall);
    }
  });
  const dueBtn = pickerBtn(S("miniapp.new_due"), S("miniapp.not_set"), async () => {
    const wall = await pickDateTime(formState.dueAt);
    if (wall) {
      formState.dueAt = wall;
      markDirty();
      dueBtn.querySelector(".picker-value").textContent = fmtWall(wall);
    }
  });
  // V4 §27: an event can be created with start AND end in one pass; the
  // backend's shared `ends_at >= starts_at` invariant rejects an inverted pair.
  const endBtn = pickerBtn(S("miniapp.new_end"), S("miniapp.not_set"), async () => {
    const wall = await pickDateTime(formState.endsAt);
    if (wall) {
      formState.endsAt = wall;
      markDirty();
      endBtn.querySelector(".picker-value").textContent = fmtWall(wall);
    }
  });

  const saveBtn = btn(S("miniapp.save"), async () => {
    formState.title = titleInput.value.trim();
    formState.description = descInput.value.trim();
    if (!formState.title) {
      toast(S("miniapp.new_title_required"), "error");
      titleInput.focus();
      return;
    }
    saveBtn.disabled = true;
    try {
      await api("/api/v1/items", "POST", {
        title: formState.title,
        kind: formState.kind,
        description: formState.description || null,
        starts_at: formState.startsAt,
        ends_at: formState.endsAt,
        due_at: formState.dueAt,
        priority: formState.priority,
        remind_offsets_minutes: remindPicker.get(),
      });
      toast(S("miniapp.saved"));
      formDirty = false; // just persisted — nothing left to discard
      navigate("today");
    } catch (e) {
      toast(e && e.status === 422 ? S("miniapp.new_title_required") : S("miniapp.error_generic"), "error");
      saveBtn.disabled = false;
    }
  }, { variant: "primary" });

  view.replaceChildren(
    card(
      el("h2", { class: "view-title" }, S("miniapp.new_title")),
      field(S("miniapp.new_title"), titleInput),
      field(S("miniapp.new_description"), descInput),
      el("div", { class: "form-row" }, field(S("miniapp.new_kind"), kindBtn), field(S("miniapp.new_priority"), priorityBtn)),
      el("div", { class: "form-row" }, field(S("miniapp.new_starts"), startsBtn), field(S("miniapp.new_end"), endBtn)),
      el("div", { class: "form-row" }, field(S("miniapp.new_due"), dueBtn)),
      el("div", { class: "field" }, el("span", { class: "field-label" }, S("miniapp.new_reminders")), remindPicker.node),
      saveBtn
    )
  );
}

/* ------------------------------------------------------------------ */
/* Edit item (V3 P30)                                                  */
/* ------------------------------------------------------------------ */

/**
 * Datetime form row: tap-to-pick plus an inline clear button (V3 P30).
 * Values are naive user-TZ wall clocks (the backend reads naive == user TZ).
 */
function whenField(labelKey, get, set) {
  const rowEl = el("div", { class: "field when-field" });
  const refresh = () => {
    const value = get();
    rowEl.replaceChildren(
      el("span", { class: "field-label" }, S(labelKey)),
      el("div", { class: "when-row" },
        pickerBtn(S(labelKey), value ? fmtWall(value) : S("miniapp.not_set"), async () => {
          const wall = await pickDateTime(value);
          if (wall) {
            set(wall);
            markDirty();
            refresh();
          }
        }),
        value
          ? el("button", {
              type: "button",
              class: "btn btn-ghost when-clear",
              "aria-label": S("miniapp.clear"),
              onclick: () => {
                haptic();
                set(null);
                markDirty();
                refresh();
              },
            }, "✕")
          : null
      )
    );
  };
  refresh();
  return rowEl;
}

async function viewEdit(view, gen, signal) {
  const id = state.editId;
  if (id == null) {
    state.tab = state.editReturn || "today";
    render();
    return;
  }
  let item;
  let reminders;
  try {
    [item, reminders] = await Promise.all([
      api(`/api/v1/items/${id}`, "GET", undefined, signal),
      api(`/api/v1/reminders?item_id=${id}&status=pending`, "GET", undefined, signal),
    ]);
  } catch (e) {
    if (isStale(gen)) return;
    if (e && e.status === 404) {
      state.editId = null;
      view.replaceChildren(empty(S("miniapp.item_not_found")));
      return;
    }
    throw e;
  }
  if (isStale(gen)) return;

  const formState = {
    priority: item.priority,
    startsAt: item.starts_at ? isoToWall(item.starts_at) : null,
    endsAt: item.ends_at ? isoToWall(item.ends_at) : null,
    dueAt: item.due_at ? isoToWall(item.due_at) : null,
  };
  const original = { ...formState, description: item.description || "" };

  const titleInput = input({
    value: item.title,
    "aria-label": S("miniapp.new_title"),
    placeholder: S("miniapp.new_title_ph"),
  });
  const descInput = input({
    value: original.description,
    "aria-label": S("miniapp.new_description"),
    placeholder: S("miniapp.new_description_ph"),
  });

  // V5 §15: a freshly rendered form is clean; typing marks it dirty.
  formDirty = false;
  titleInput.addEventListener("input", markDirty);
  descInput.addEventListener("input", markDirty);

  const priorityBtn = pickerBtn(
    S("miniapp.new_priority"),
    S(PRIORITY_LABELS[item.priority] || PRIORITY_LABELS.normal),
    async () => {
      const chosen = await openSheet({
        title: S("miniapp.new_priority"),
        value: formState.priority,
        options: [
          { value: "low", label: S("miniapp.new_low") },
          { value: "normal", label: S("miniapp.new_normal") },
          { value: "high", label: S("miniapp.new_high") },
        ],
      });
      if (chosen) {
        formState.priority = chosen;
        markDirty();
        priorityBtn.querySelector(".picker-value").textContent = S(PRIORITY_LABELS[chosen]);
      }
    }
  );

  // kind is display-only: PATCH /items has no kind field (SPEC §4.3).
  const kindValue = el("span", { class: "picker-value" },
    item.kind === "event" ? S("miniapp.new_event") : S("miniapp.new_task"));

  const reminderList = el("div", { class: "reminder-list" });
  const renderReminders = () => {
    reminderList.replaceChildren(
      ...(reminders.length
        ? reminders.map((r) =>
            el("div", { class: "reminder-row" },
              el("span", { class: "reminder-when" }, fmtDT(r.fire_at)),
              el("span", { class: "reminder-offset" },
                r.offset_minutes
                  ? S("miniapp.reminder_offset", { n: r.offset_minutes })
                  : S("miniapp.reminder_at_start")),
              btn(S("miniapp.btn_cancel"), async () => {
                try {
                  await api(`/api/v1/reminders/${r.id}/cancel`, "POST");
                  toast(S("miniapp.reminder_cancelled"));
                  reminders = reminders.filter((x) => x.id !== r.id);
                  renderReminders();
                } catch {
                  toast(S("miniapp.error_generic"), "error");
                }
              })
            ))
        : [el("p", { class: "reminder-none" }, S("miniapp.reminders_empty"))])
    );
  };
  renderReminders();

  // Add reminders to the existing item (V4 §28): the same shared picker as
  // the New view; the server re-validates and caps the item's total.
  const remindPicker = reminderPicker();
  const addReminderBtn = btn(S("miniapp.reminder_save"), async () => {
    const selected = remindPicker.get();
    if (!selected.length) return;
    if (!item.starts_at) {
      toast(S("miniapp.reminder_needs_start"), "error");
      return;
    }
    addReminderBtn.disabled = true;
    try {
      const created = await api(
        `/api/v1/items/${id}/reminders`,
        "POST",
        { offsets_minutes: selected },
        signal,
      );
      toast(S("miniapp.reminder_added"));
      reminders.push(...created);
      renderReminders();
      remindPicker.clear();
    } catch {
      toast(S("miniapp.error_generic"), "error");
    } finally {
      addReminderBtn.disabled = false;
    }
  });

  const saveBtn = btn(S("miniapp.save"), async () => {
    const title = titleInput.value.trim();
    const description = descInput.value.trim();
    if (!title) {
      toast(S("miniapp.new_title_required"), "error");
      titleInput.focus();
      return;
    }
    // PATCH tri-state (SPEC §4.3): send only the changed keys; an explicit
    // null clears, an omitted key is left as-is.
    const body = {};
    if (title !== item.title) body.title = title;
    if (description !== original.description) body.description = description || null;
    if (formState.startsAt !== original.startsAt) body.starts_at = formState.startsAt;
    if (formState.endsAt !== original.endsAt) body.ends_at = formState.endsAt;
    if (formState.dueAt !== original.dueAt) body.due_at = formState.dueAt;
    if (formState.priority !== original.priority) body.priority = formState.priority;
    saveBtn.disabled = true;
    try {
      await api(`/api/v1/items/${id}`, "PATCH", body, signal);
      toast(S("miniapp.saved"));
      formDirty = false;
      navigate(state.editReturn || "today");
    } catch (e) {
      if (isStale(gen)) return;
      toast(e && e.status === 422 ? S("miniapp.new_title_required") : S("miniapp.error_generic"), "error");
      saveBtn.disabled = false;
    }
  }, { variant: "primary" });

  view.replaceChildren(
    card(
      el("h2", { class: "view-title" }, S("miniapp.edit_title")),
      field(S("miniapp.new_title"), titleInput),
      field(S("miniapp.new_description"), descInput),
      el("div", { class: "form-row" }, field(S("miniapp.new_kind"), kindValue), field(S("miniapp.new_priority"), priorityBtn)),
      whenField("miniapp.new_starts", () => formState.startsAt, (v) => { formState.startsAt = v; }),
      whenField("miniapp.new_ends", () => formState.endsAt, (v) => { formState.endsAt = v; }),
      whenField("miniapp.new_due", () => formState.dueAt, (v) => { formState.dueAt = v; }),
      field(S("miniapp.new_reminders"), reminderList, remindPicker.node, addReminderBtn),
      saveBtn
    )
  );
}

/** A tap-to-open picker row (sheet or date picker), Telegram-like. */
function pickerBtn(label, valueText, onTap) {
  return el(
    "button",
    {
      type: "button",
      class: "picker-field",
      "aria-haspopup": "dialog",
      "aria-label": label,
      onclick: () => {
        haptic();
        onTap();
      },
    },
    el("span", { class: "picker-value" }, valueText),
    el("span", { class: "picker-icon", "aria-hidden": "true" }, "›")
  );
}

/* ------------------------------------------------------------------ */
/* Workouts                                                            */
/* ------------------------------------------------------------------ */

async function viewWorkouts(view, gen, signal) {
  const [stats, logs] = await Promise.all([
    api("/api/v1/workouts/stats", "GET", undefined, signal),
    api("/api/v1/workouts?limit=10", "GET", undefined, signal),
  ]);
  if (isStale(gen)) return;

  const statsCard = card(
    el("h2", { class: "view-title" }, S("miniapp.workouts_stats")),
    el("div", { class: "stats-grid" },
      statCell(String(stats.total), S("miniapp.stats_total")),
      statCell(String(stats.total_minutes), S("miniapp.stats_minutes")),
      statCell(String(stats.this_week), S("miniapp.stats_week")),
      statCell(String(stats.current_streak), S("miniapp.stats_streak"))
    )
  );

  const nameInput = input({
    placeholder: S("miniapp.workout_name_ph"),
    "aria-label": S("miniapp.workout_log"),
  });
  const minutesInput = input({
    type: "number",
    min: "1",
    placeholder: S("miniapp.minutes_ph"),
    "aria-label": S("miniapp.minutes"),
    inputmode: "numeric",
  });
  let logStart = null;
  const whenBtn = pickerBtn(S("miniapp.when"), S("miniapp.now"), async () => {
    const wall = await pickDateTime(logStart);
    if (wall) {
      logStart = wall;
      whenBtn.querySelector(".picker-value").textContent = fmtWall(wall);
    }
  });
  const effortBtn = pickerBtn(S("miniapp.effort"), S("miniapp.not_set"), async () => {
    const chosen = await openSheet({
      title: S("miniapp.effort"),
      options: [
        { value: "", label: S("miniapp.not_set") },
        ...[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((n) => ({
          value: String(n),
          label: `${n} / 10`,
        })),
      ],
    });
    if (chosen !== null)
      effortBtn.querySelector(".picker-value").textContent =
        chosen === "" ? S("miniapp.not_set") : `${chosen} / 10`;
  });

  const logBtn = btn(S("miniapp.log"), async () => {
    const name = nameInput.value.trim();
    if (!name) {
      toast(S("miniapp.workout_name_required"), "error");
      nameInput.focus();
      return;
    }
    logBtn.disabled = true;
    try {
      await api("/api/v1/workouts", "POST", {
        name,
        started_at: logStart,
        duration_minutes: minutesInput.value ? Number(minutesInput.value) : null,
        perceived_effort: effortValue(),
      });
      toast(S("miniapp.workout_logged"));
      render();
    } catch {
      toast(S("miniapp.error_generic"), "error");
      logBtn.disabled = false;
    }
    function effortValue() {
      const v = effortBtn.querySelector(".picker-value").textContent;
      const match = v.match(/^(\d+)/);
      return match ? Number(match[1]) : null;
    }
  }, { variant: "primary" });

  const logCard = card(
    el("h2", { class: "view-title" }, S("miniapp.workout_log")),
    field(S("miniapp.workout_name_ph"), nameInput),
    el("div", { class: "form-row" }, field(S("miniapp.when"), whenBtn), field(S("miniapp.minutes"), minutesInput)),
    field(S("miniapp.effort"), effortBtn),
    logBtn
  );

  // Schedule: a future calendar item + a reminder at the start time.
  const schedNameInput = input({
    placeholder: S("miniapp.workout_name_ph"),
    "aria-label": S("miniapp.workout_schedule"),
  });
  let schedStart = null;
  const schedStartBtn = pickerBtn(S("miniapp.when"), S("miniapp.not_set"), async () => {
    const wall = await pickDateTime(schedStart);
    if (wall) {
      schedStart = wall;
      schedStartBtn.querySelector(".picker-value").textContent = fmtWall(wall);
    }
  });
  const schedMinutesInput = input({
    type: "number",
    min: "1",
    placeholder: S("miniapp.minutes_ph"),
    "aria-label": S("miniapp.minutes"),
    inputmode: "numeric",
  });
  const schedBtn = btn(S("miniapp.schedule"), async () => {
    const name = schedNameInput.value.trim();
    if (!name) {
      toast(S("miniapp.workout_name_required"), "error");
      schedNameInput.focus();
      return;
    }
    if (!schedStart) {
      toast(S("miniapp.when_required"), "error");
      return;
    }
    schedBtn.disabled = true;
    try {
      await api("/api/v1/workouts/schedule", "POST", {
        name,
        starts_at: schedStart,
        duration_minutes: schedMinutesInput.value ? Number(schedMinutesInput.value) : null,
      });
      toast(S("miniapp.workout_scheduled"));
      render();
    } catch {
      toast(S("miniapp.error_generic"), "error");
      schedBtn.disabled = false;
    }
  }, { variant: "primary" });

  const scheduleCard = card(
    el("h2", { class: "view-title" }, S("miniapp.workout_schedule")),
    field(S("miniapp.workout_name_ph"), schedNameInput),
    el("div", { class: "form-row" }, field(S("miniapp.when"), schedStartBtn), field(S("miniapp.minutes"), schedMinutesInput)),
    schedBtn
  );

  view.replaceChildren(
    statsCard,
    logCard,
    scheduleCard,
    el("h2", { class: "view-subtitle" }, S("miniapp.recent")),
    logs.length
      ? el("div", { class: "list" }, ...logs.map((w) => workoutCard(w)))
      : empty(S("miniapp.workouts_empty"))
  );
}

function statCell(value, label) {
  return el("div", { class: "stat-cell" },
    el("div", { class: "stat-value" }, value),
    el("div", { class: "stat-label" }, label));
}

function workoutCard(w) {
  const meta = [];
  if (w.duration_minutes) meta.push(S("miniapp.minutes_value", { minutes: w.duration_minutes }));
  if (w.perceived_effort) meta.push(S("miniapp.effort_value", { value: w.perceived_effort }));
  if (w.started_at) meta.unshift(fmtDT(w.started_at));
  return card(
    el("div", { class: "item-head" },
      el("span", { class: "item-icon", "aria-hidden": "true" }, "💪"),
      el("span", { class: "item-title" }, w.name)
    ),
    meta.length ? el("div", { class: "item-meta" }, ...meta) : null
  );
}

/* ------------------------------------------------------------------ */
/* Files                                                               */
/* ------------------------------------------------------------------ */

const FILE_STATE_KEYS = {
  queued: "miniapp.state_queued",
  downloading: "miniapp.state_downloading",
  extracting: "miniapp.state_extracting",
  chunking: "miniapp.state_chunking",
  embedding: "miniapp.state_embedding",
  indexed: "miniapp.state_indexed",
  failed: "miniapp.state_failed",
  rejected: "miniapp.state_rejected",
};
const FILE_STATE_TONES = {
  queued: "muted",
  downloading: "muted",
  extracting: "muted",
  chunking: "muted",
  embedding: "muted",
  indexed: "ok",
  failed: "error",
  rejected: "error",
};

async function viewFiles(view, gen, signal) {
  const files = await api("/api/v1/files?limit=20", "GET", undefined, signal);
  if (isStale(gen)) return;

  const fileInput = el("input", {
    type: "file",
    class: "file-input",
    id: "file-upload-input",
    "aria-label": S("miniapp.upload"),
  });
  fileInput.addEventListener("change", async () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    uploadBtn.disabled = true;
    uploadLabel.textContent = S("miniapp.uploading");
    try {
      await apiUpload("/api/v1/files", file);
      toast(S("miniapp.uploaded", { name: file.name }));
      render();
    } catch {
      toast(S("miniapp.upload_failed"), "error");
      uploadBtn.disabled = false;
      uploadLabel.textContent = S("miniapp.upload");
      fileInput.value = "";
    }
  });
  const uploadLabel = el("span", { class: "btn-label" }, S("miniapp.upload"));
  const uploadBtn = el(
    "button",
    { type: "button", class: "btn btn-primary upload-btn", "aria-label": S("miniapp.upload") },
    el("span", { class: "upload-icon", "aria-hidden": "true" }, "⬆"),
    uploadLabel
  );
  uploadBtn.addEventListener("click", () => fileInput.click());

  // Search (V3 P32): full-text + vector search over indexed files.
  const searchInput = input({
    placeholder: S("miniapp.search_ph"),
    "aria-label": S("miniapp.search"),
  });
  const searchBtn = btn(S("miniapp.search"), null, { variant: "primary" });
  const searchResults = el("div", { class: "search-results" });
  let searchBusy = false;
  const doSearch = async () => {
    const q = searchInput.value.trim();
    if (!q || searchBusy) return;
    searchBusy = true;
    searchBtn.disabled = true;
    searchResults.replaceChildren(loading());
    try {
      const results = await api(
        `/api/v1/files/search?q=${encodeURIComponent(q)}&top_k=10`,
        "GET",
        undefined,
        signal,
      );
      if (isStale(gen)) return;
      searchResults.replaceChildren(
        results.length
          ? el("div", { class: "list" }, ...results.map(searchResultCard))
          : empty(S("miniapp.search_empty"))
      );
    } catch {
      if (isStale(gen)) return;
      searchResults.replaceChildren(errorState(S("miniapp.error_load"), doSearch));
    } finally {
      if (!isStale(gen)) {
        searchBusy = false;
        searchBtn.disabled = false;
      }
    }
  };
  searchBtn.onclick = doSearch;
  searchInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSearch();
  });

  view.replaceChildren(
    card(
      el("h2", { class: "view-title" }, S("miniapp.my_files")),
      el("div", { class: "search-row" }, searchInput, searchBtn),
      searchResults,
      el("div", { class: "upload-row" }, uploadBtn, fileInput),
      files.length
        ? el("div", { class: "list" }, ...files.map(fileCard))
        : empty(S("miniapp.files_empty"))
    )
  );
}

function searchResultCard(r) {
  return card(
    el("div", { class: "item-head" },
      el("span", { class: "item-icon", "aria-hidden": "true" }, "📄"),
      el("span", { class: "item-title file-name" }, r.file_name),
      badge(S("miniapp.search_chunk", { file: r.file_name, position: r.position + 1 }))
    ),
    el("p", { class: "search-excerpt" }, r.text)
  );
}

function fileCard(f) {
  const meta = [fmtBytes(f.size_bytes || 0), fmtDT(f.created_at)].filter(Boolean);
  return card(
    el("div", { class: "item-head" },
      el("span", { class: "item-icon", "aria-hidden": "true" }, "📄"),
      el("span", { class: "item-title file-name" }, f.original_filename),
      badge(S(FILE_STATE_KEYS[f.state] || f.state), FILE_STATE_TONES[f.state] || "muted")
    ),
    el("div", { class: "item-meta" }, ...meta),
    f.error ? el("p", { class: "item-error" }, f.error) : null,
    el("div", { class: "item-actions" },
      f.state === "failed"
        ? btn(S("miniapp.file_retry"), async () => {
            try {
              await api(`/api/v1/files/${f.id}/retry`, "POST");
              toast(S("miniapp.saved"));
              render();
            } catch {
              toast(S("miniapp.error_generic"), "error");
            }
          }, { variant: "primary" })
        : null,
      btn(S("miniapp.btn_delete"), async () => {
        const ok = await confirmDialog(S("miniapp.confirm_delete", { title: f.original_filename }));
        if (!ok) return;
        try {
          await api(`/api/v1/files/${f.id}`, "DELETE");
          toast(S("miniapp.deleted"));
          render();
        } catch {
          toast(S("miniapp.error_generic"), "error");
        }
      }, { variant: "danger" })
    )
  );
}

/* ------------------------------------------------------------------ */
/* Facts                                                               */
/* ------------------------------------------------------------------ */

const FACT_STATE_KEYS = {
  proposed: "miniapp.fact_proposed",
  confirmed: "miniapp.fact_confirmed",
  rejected: "miniapp.fact_rejected",
  superseded: "miniapp.fact_superseded",
};
const FACT_STATE_TONES = {
  proposed: "muted",
  confirmed: "ok",
  rejected: "error",
  superseded: "muted",
};

async function viewFacts(view, gen, signal) {
  const facts = await api("/api/v1/facts?limit=50", "GET", undefined, signal);
  if (isStale(gen)) return;

  const valueInput = input({
    placeholder: S("miniapp.facts_value_ph"),
    "aria-label": S("miniapp.facts_add"),
  });
  const categoryInput = input({
    placeholder: S("miniapp.facts_category_ph"),
    "aria-label": S("miniapp.facts_category"),
  });
  const proposeBtn = btn(S("miniapp.propose"), async () => {
    const value = valueInput.value.trim();
    if (!value) {
      toast(S("miniapp.fact_value_required"), "error");
      valueInput.focus();
      return;
    }
    proposeBtn.disabled = true;
    try {
      await api("/api/v1/facts", "POST", {
        value,
        category: categoryInput.value.trim() || null,
      });
      toast(S("miniapp.proposed"));
      render();
    } catch {
      toast(S("miniapp.error_generic"), "error");
      proposeBtn.disabled = false;
    }
  }, { variant: "primary" });

  view.replaceChildren(
    card(
      el("h2", { class: "view-title" }, S("miniapp.facts_add")),
      field(S("miniapp.facts_value_ph"), valueInput),
      field(S("miniapp.facts_category"), categoryInput),
      proposeBtn
    ),
    el("h2", { class: "view-subtitle" }, S("miniapp.my_facts")),
    facts.length
      ? el(
          "div",
          { class: "list" },
          ...facts.map((f) => factCard(f, factsById(facts))),
        )
      : empty(S("miniapp.facts_empty"))
  );
}

/* Inline "replace" form state: one fact at a time, survives re-renders. */
let supersedingFactId = null;

function factsById(facts) {
  const map = new Map();
  for (const f of facts) map.set(f.id, f);
  return map;
}

function factCard(f, byId) {
  // A replacement fact (proposed, linked via replaces_fact_id) is not itself
  // replaceable; a plain proposed/confirmed fact is.
  const replaceable =
    f.status === "confirmed" ||
    (f.status === "proposed" && !f.replaces_fact_id);
  const actions = [];
  if (f.status === "proposed") {
    actions.push(
      btn(S("miniapp.confirm"), async () => {
        try {
          await api(`/api/v1/facts/${f.id}/confirm`, "POST");
          toast(S("miniapp.saved"));
          render();
        } catch {
          toast(S("miniapp.error_generic"), "error");
        }
      }, { variant: "primary" }),
      btn(S("miniapp.reject"), async () => {
        try {
          await api(`/api/v1/facts/${f.id}/reject`, "POST");
          toast(S("miniapp.saved"));
          render();
        } catch {
          toast(S("miniapp.error_generic"), "error");
        }
      })
    );
  }
  if (replaceable) {
    actions.push(
      btn(S("miniapp.fact_replace"), () => {
        supersedingFactId = supersedingFactId === f.id ? null : f.id;
        render();
      })
    );
  }
  actions.push(
    btn(S("miniapp.btn_delete"), async () => {
      const ok = await confirmDialog(S("miniapp.confirm_delete", { title: f.value }));
      if (!ok) return;
      try {
        await api(`/api/v1/facts/${f.id}`, "DELETE");
        toast(S("miniapp.deleted"));
        render();
      } catch {
        toast(S("miniapp.error_generic"), "error");
      }
    }, { variant: "danger" })
  );

  // A pending replacement is a distinct state from a plain proposal:
  // give it its own badge (SPEC §14: distinguish replacement).
  const isReplacement = f.status === "proposed" && f.replaces_fact_id != null;
  // The other side of a completed replacement: what this fact became.
  const replacedBy =
    f.status === "superseded" && f.superseded_by != null && byId.has(f.superseded_by)
      ? byId.get(f.superseded_by).value
      : null;

  return card(
    el("div", { class: "item-head" },
      el("span", { class: "item-icon", "aria-hidden": "true" }, "🧠"),
      el("span", { class: "item-title fact-category" }, f.category),
      badge(S(FACT_STATE_KEYS[f.status] || f.status), FACT_STATE_TONES[f.status] || "muted"),
      isReplacement ? badge(S("miniapp.fact_replacement")) : null
    ),
    // The fact value is user content: always rendered as a text node.
    el("p", { class: "fact-value" }, f.value),
    // Replacement link: show the value this proposed fact supersedes.
    f.replaces_fact_id && byId.has(f.replaces_fact_id)
      ? el(
          "p",
          { class: "fact-replaces" },
          S("miniapp.fact_replaces") + ": " + byId.get(f.replaces_fact_id).value,
        )
      : null,
    // Superseded link: show the value this fact was replaced by.
    replacedBy
      ? el("p", { class: "fact-replaces" }, S("miniapp.fact_replaced_by") + ": " + replacedBy)
      : null,
    supersedingFactId === f.id
      ? replaceForm(f)
      : null,
    el("div", { class: "item-actions" }, ...actions)
  );
}

/** Inline form proposing a replacement value (POST /facts/{id}/supersede). */
function replaceForm(f) {
  const inputNode = input({
    placeholder: S("miniapp.fact_replace_ph"),
    "aria-label": S("miniapp.fact_replace_ph"),
  });
  const save = btn(S("miniapp.save"), async () => {
    const value = inputNode.value.trim();
    if (!value) {
      toast(S("miniapp.fact_value_required"), "error");
      inputNode.focus();
      return;
    }
    save.disabled = true;
    try {
      await api(`/api/v1/facts/${f.id}/supersede`, "POST", { value });
      supersedingFactId = null;
      toast(S("miniapp.proposed"));
      render();
    } catch {
      toast(S("miniapp.error_generic"), "error");
      save.disabled = false;
    }
  }, { variant: "primary" });
  return el("div", { class: "replace-form" },
    inputNode,
    el("div", { class: "item-actions" },
      save,
      btn(S("miniapp.btn_cancel"), () => {
        supersedingFactId = null;
        render();
      })
    )
  );
}

/* ------------------------------------------------------------------ */
/* Settings                                                            */
/* ------------------------------------------------------------------ */

async function viewSettings(view, gen, signal) {
  const settings = state.me.settings;

  // Proactive settings are a separate card; a failure there must not break
  // the core settings screen.
  let proactiveCard = null;
  try {
    proactiveCard = await buildProactiveCard(signal);
  } catch {
    /* card omitted */
  }
  if (isStale(gen)) return;

  view.replaceChildren(
    card(
      el("h2", { class: "view-title" }, S("miniapp.settings")),
      settingsRow(S("miniapp.settings_language"), S(settings.language === "en" ? "miniapp.lang_en" : "miniapp.lang_ru"), async () => {
        const languages = await api("/api/v1/i18n/languages");
        const chosen = await openSheet({
          title: S("miniapp.settings_language"),
          value: settings.language,
          options: languages.map((l) => ({ value: l.code, label: l.label })),
        });
        if (!chosen) return;
        const saved = await api("/api/v1/settings", "PATCH", { language: chosen });
        state.me.settings = saved;
        // Switch the whole UI immediately, without a reload.
        await setLanguage(saved.language);
        toast(S("miniapp.settings_saved"));
        render();
      }),
      settingsRow(S("miniapp.settings_timezone"), settings.timezone, async () => {
        const chosen = await openSheet({
          title: S("miniapp.settings_timezone"),
          value: settings.timezone,
          options: timezoneOptions().map((z) => ({ value: z, label: z })),
          searchable: true,
          searchPlaceholder: S("miniapp.tz_search_ph"),
        });
        if (!chosen) return;
        const saved = await api("/api/v1/settings", "PATCH", { timezone: chosen });
        state.me.settings = saved;
        toast(S("miniapp.settings_saved"));
        render();
      }),
      settingsRow(S("miniapp.settings_digest"), (settings.digest_time || "08:00").slice(0, 5), async () => {
        const chosen = await pickTime((settings.digest_time || "08:00:00").slice(0, 5));
        if (!chosen) return;
        const saved = await api("/api/v1/settings", "PATCH", { digest_time: `${chosen}:00` });
        state.me.settings = saved;
        toast(S("miniapp.settings_saved"));
        render();
      }),
      switchRow(S("miniapp.settings_motivation"), settings.motivation_enabled, async (next) => {
        const saved = await api("/api/v1/settings", "PATCH", { motivation_enabled: next });
        state.me.settings = saved;
        toast(S("miniapp.settings_saved"));
        render();
      })
    ),
    proactiveCard
  );
}

/** Proactive-notifications card backed by /api/v1/proactive-settings. */
async function buildProactiveCard(signal) {
  const ps = await api(
    "/api/v1/proactive-settings",
    "GET",
    undefined,
    signal
  );
  const patch = async (body) => {
    try {
      await api("/api/v1/proactive-settings", "PATCH", body);
      toast(S("miniapp.settings_saved"));
    } catch {
      toast(S("miniapp.error_generic"), "error");
    }
  };
  return card(
    el("h2", { class: "view-title" }, S("miniapp.proactive_title")),
    switchRow(S("miniapp.proactive_enabled"), ps.enabled, (v) => patch({ enabled: v })),
    switchRow(S("miniapp.proactive_weekly_review"), ps.weekly_review_enabled, (v) => patch({ weekly_review_enabled: v })),
    switchRow(S("miniapp.proactive_workout_nudge"), ps.workout_nudge_enabled, (v) => patch({ workout_nudge_enabled: v })),
    switchRow(S("miniapp.proactive_overdue_nudge"), ps.overdue_nudge_enabled, (v) => patch({ overdue_nudge_enabled: v })),
    settingsRow(S("miniapp.proactive_quiet_from"), ps.quiet_hours_start.slice(0, 5), async () => {
      const t = await pickTime(ps.quiet_hours_start.slice(0, 5));
      if (t) patch({ quiet_hours_start: `${t}:00` });
    }),
    settingsRow(S("miniapp.proactive_quiet_until"), ps.quiet_hours_end.slice(0, 5), async () => {
      const t = await pickTime(ps.quiet_hours_end.slice(0, 5));
      if (t) patch({ quiet_hours_end: `${t}:00` });
    }),
    settingsRow(S("miniapp.proactive_max_per_day"), String(ps.max_nudges_per_day), async () => {
      const chosen = await openSheet({
        title: S("miniapp.proactive_max_per_day"),
        value: String(ps.max_nudges_per_day),
        options: [...Array(20).keys()].map((i) => ({ value: String(i + 1), label: String(i + 1) })),
      });
      if (chosen) patch({ max_nudges_per_day: Number(chosen) });
    }),
    settingsRow(S("miniapp.proactive_min_interval"), S("miniapp.minutes_value", { minutes: ps.min_interval_minutes }), async () => {
      const chosen = await openSheet({
        title: S("miniapp.proactive_min_interval"),
        value: String(ps.min_interval_minutes),
        options: [0, 15, 30, 60, 120, 240, 720].map((n) => ({
          value: String(n),
          label: n === 0 ? "0" : S("miniapp.minutes_value", { minutes: n }),
        })),
      });
      if (chosen) patch({ min_interval_minutes: Number(chosen) });
    })
  );
}

/** A settings row that opens a sheet/picker on tap (Telegram-like). */
function settingsRow(label, valueText, onTap) {
  return el(
    "button",
    {
      type: "button",
      class: "settings-row",
      "aria-haspopup": "dialog",
      onclick: () => {
        haptic();
        onTap();
      },
    },
    el("span", { class: "settings-row-label" }, label),
    el("span", { class: "settings-row-value" }, valueText),
    el("span", { class: "settings-row-icon", "aria-hidden": "true" }, "›")
  );
}

/** A settings row with an on/off switch (a real checkbox input). */
function switchRow(label, checked, onChange) {
  const box = el("input", {
    type: "checkbox",
    class: "switch",
    role: "switch",
    "aria-label": label,
    checked: checked ? "checked" : null,
  });
  box.addEventListener("change", () => onChange(box.checked));
  return el("div", { class: "settings-row settings-row-static" },
    el("span", { class: "settings-row-label" }, label),
    el("label", { class: "switch-wrap" }, box));
}

/* ------------------------------------------------------------------ */
/* Boot                                                                */
/* ------------------------------------------------------------------ */

async function boot() {
  // ready() now; expand() is deferred until the first real render (V5 §13).
  webAppReady();
  applyTheme();
  applyViewport();
  onThemeChanged(() => {
    // Theme changes re-style the app through CSS variables automatically.
  });
  onViewportChanged(() => {
    // Safe-area / stable-height tokens are re-applied inside the handler.
  });
  backButtonOn(() => {
    navigate(state.tab === "edit" ? (state.editReturn || "today") : "today");
  });
  // Closing the whole app (not just navigating) — native confirmation when a
  // form has unsaved changes (V5 §15).
  window.addEventListener("beforeunload", (e) => {
    if (isFormView(state.tab) && formDirty) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
  try {
    const me = await loadMe();
    whoEl.textContent = [me.user.first_name, me.user.last_name].filter(Boolean).join(" ");
    await render();
    webAppExpand();
  } catch (e) {
    const key = e && e.status === 401 ? "miniapp.status_auth" : "miniapp.status_open";
    viewEl.replaceChildren(empty(S(key)));
    webAppExpand();
  }
}

boot();

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
  initWebApp,
  onThemeChanged,
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
  fmtDT,
  fmtWall,
} from "./js/time.js";

const viewEl = document.getElementById("view");
const navEl = document.getElementById("nav");
const whoEl = document.getElementById("who");

const TABS = [
  ["today", "miniapp.tab_today"],
  ["actions", "miniapp.tab_actions"],
  ["upcoming", "miniapp.tab_upcoming"],
  ["new", "miniapp.tab_new"],
  ["workouts", "miniapp.tab_workouts"],
  ["files", "miniapp.tab_files"],
  ["facts", "miniapp.tab_facts"],
  ["settings", "miniapp.tab_settings"],
];

const TIMEZONES = [
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
  navEl.replaceChildren(
    ...TABS.map(([key, keyStr]) =>
      el(
        "button",
        {
          type: "button",
          class: `nav-btn${key === state.tab ? " is-active" : ""}`,
          "aria-current": key === state.tab ? "page" : null,
          "data-tab": key,
          onclick: () => {
            if (state.tab === key) return;
            state.tab = key;
            haptic();
            render();
          },
        },
        el("span", { class: "nav-icon", "aria-hidden": "true" }, iconFor(key)),
        el("span", { class: "nav-label" }, S(keyStr))
      )
    )
  );
  updateBackButton();
}

/** Emojis are part of the localized tab strings; keep an iconless fallback. */
function iconFor(key) {
  const label = S(TABS.find(([k]) => k === key)?.[1] ?? key);
  const match = label.match(/^(\p{Extended_Pictographic}+)/u);
  return match ? match[1] : "•";
}

function updateBackButton() {
  if (state.tab === "today") backButtonHide();
  else backButtonShow();
}

/* ------------------------------------------------------------------ */
/* View dispatch                                                       */
/* ------------------------------------------------------------------ */

const VIEWS = {
  today: viewToday,
  actions: viewActions,
  upcoming: viewUpcoming,
  new: viewNew,
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
/* Items (shared card)                                                 */
/* ------------------------------------------------------------------ */

function itemCard(item) {
  const actions = [];
  if (item.status === "scheduled") {
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
  if (item.due_at) meta.push(el("span", {}, S("miniapp.item_due", { when: fmtDT(item.due_at) })));
  if (item.status !== "scheduled") {
    meta.push(badge(S(`miniapp.status_${item.status}`), item.status === "completed" ? "ok" : "muted"));
  }

  return card(
    el("div", { class: "item-head" },
      el("span", { class: "item-icon", "aria-hidden": "true" }, item.kind === "event" ? "📌" : "✅"),
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
  replace_fact: "🧠",
};
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

  const actions = [];
  if (a.status === "proposed") {
    actions.push(
      btn(S("miniapp.confirm"), async () => {
        try {
          await api(`/api/v1/actions/${a.id}/confirm`, "POST");
          toast(S("miniapp.saved"));
          render();
        } catch (e) {
          toast(e && e.status === 409 ? S("miniapp.action_stale") : S("miniapp.error_generic"), "error");
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
    el("div", { class: "item-actions" }, ...actions)
  );
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
    dueAt: null,
    reminders: "",
  };

  const titleInput = input({
    placeholder: S("miniapp.new_title_ph"),
    "aria-label": S("miniapp.new_title"),
  });
  const descInput = input({
    placeholder: S("miniapp.new_description_ph"),
    "aria-label": S("miniapp.new_description"),
  });
  const remindInput = input({
    placeholder: S("miniapp.new_reminders_ph"),
    "aria-label": S("miniapp.new_reminders"),
    inputmode: "numeric",
  });

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
      priorityBtn.querySelector(".picker-value").textContent = S(PRIORITY_LABELS[chosen]);
    }
  });
  // pickDateTime resolves a NAIVE user-TZ wall clock ("YYYY-MM-DDTHH:MM"),
  // which is exactly what the backend expects (naive == user's timezone).
  const startsBtn = pickerBtn(S("miniapp.new_starts"), S("miniapp.not_set"), async () => {
    const wall = await pickDateTime(formState.startsAt);
    if (wall) {
      formState.startsAt = wall;
      startsBtn.querySelector(".picker-value").textContent = fmtWall(wall);
    }
  });
  const dueBtn = pickerBtn(S("miniapp.new_due"), S("miniapp.not_set"), async () => {
    const wall = await pickDateTime(formState.dueAt);
    if (wall) {
      formState.dueAt = wall;
      dueBtn.querySelector(".picker-value").textContent = fmtWall(wall);
    }
  });

  const saveBtn = btn(S("miniapp.save"), async () => {
    formState.title = titleInput.value.trim();
    formState.description = descInput.value.trim();
    formState.reminders = remindInput.value;
    if (!formState.title) {
      toast(S("miniapp.new_title_required"), "error");
      titleInput.focus();
      return;
    }
    const offsets = formState.reminders
      .split(",")
      .map((s) => s.trim())
      .filter((s) => s !== "")
      .map((s) => Number(s))
      .filter((n) => Number.isInteger(n));
    saveBtn.disabled = true;
    try {
      await api("/api/v1/items", "POST", {
        title: formState.title,
        kind: formState.kind,
        description: formState.description || null,
        starts_at: formState.startsAt,
        due_at: formState.dueAt,
        priority: formState.priority,
        remind_offsets_minutes: offsets,
      });
      toast(S("miniapp.saved"));
      state.tab = "today";
      render();
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
      el("div", { class: "form-row" }, field(S("miniapp.new_starts"), startsBtn), field(S("miniapp.new_due"), dueBtn)),
      field(S("miniapp.new_reminders"), remindInput),
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
  const whenBtn = pickerBtn(S("miniapp.when"), S("miniapp.now"), async () => {
    const wall = await pickDateTime(null);
    if (wall) whenBtn.querySelector(".picker-value").textContent = fmtWall(wall);
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

  view.replaceChildren(
    card(
      el("h2", { class: "view-title" }, S("miniapp.my_files")),
      el("div", { class: "upload-row" }, uploadBtn, fileInput),
      files.length
        ? el("div", { class: "list" }, ...files.map(fileCard))
        : empty(S("miniapp.files_empty"))
    )
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

  return card(
    el("div", { class: "item-head" },
      el("span", { class: "item-icon", "aria-hidden": "true" }, "🧠"),
      el("span", { class: "item-title fact-category" }, f.category),
      badge(S(FACT_STATE_KEYS[f.status] || f.status), FACT_STATE_TONES[f.status] || "muted")
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
        const zones = settings.timezone && !TIMEZONES.includes(settings.timezone)
          ? [settings.timezone, ...TIMEZONES]
          : TIMEZONES;
        const chosen = await openSheet({
          title: S("miniapp.settings_timezone"),
          value: settings.timezone,
          options: zones.map((z) => ({ value: z, label: z })),
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
  initWebApp();
  applyTheme();
  onThemeChanged(() => {
    // Theme changes re-style the app through CSS variables automatically.
  });
  backButtonOn(() => {
    state.tab = "today";
    render();
  });
  try {
    const me = await loadMe();
    whoEl.textContent = [me.user.first_name, me.user.last_name].filter(Boolean).join(" ");
    await render();
  } catch (e) {
    const key = e && e.status === 401 ? "miniapp.status_auth" : "miniapp.status_open";
    viewEl.replaceChildren(empty(S(key)));
  }
}

boot();

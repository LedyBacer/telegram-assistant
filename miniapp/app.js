/* Smart Assistant Mini App: vanilla JS + Tailwind (SPEC §18).
 * Every request sends the raw Telegram.WebApp.initData; the backend verifies
 * its HMAC signature before trusting the identity. All user-supplied strings
 * are rendered through textContent, never innerHTML.
 * All UI strings come from the backend locale dictionary (single source of
 * truth: /api/v1/i18n/{locale}); nothing is hard-coded here. */
(() => {
  "use strict";

  const tg = window.Telegram?.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
  }
  const INIT_DATA = tg?.initData || "";

  const statusEl = document.getElementById("status");
  const viewEl = document.getElementById("view");
  const tabsEl = document.getElementById("tabs");
  const whoEl = document.getElementById("who");

  /* Locale dictionary, loaded from the API for the user's language. */
  let STR = {};
  const S = (key, params) =>
    String(STR[key] !== undefined ? STR[key] : key).replace(
      /\{(\w+)\}/g,
      (m, k) => (params && params[k] !== undefined ? String(params[k]) : m)
    );

  const TAB_KEYS = [
    ["today", "miniapp.tab_today"],
    ["upcoming", "miniapp.tab_upcoming"],
    ["new", "miniapp.tab_new"],
    ["workouts", "miniapp.tab_workouts"],
    ["files", "miniapp.tab_files"],
    ["facts", "miniapp.tab_facts"],
    ["settings", "miniapp.tab_settings"],
  ];
  let tab = "today";
  let me = null;

  /* ------------------------------------------------------------------ */
  /* API helpers                                                         */
  /* ------------------------------------------------------------------ */

  async function api(path, method = "GET", body) {
    const res = await fetch(path, {
      method,
      headers: {
        "Content-Type": "application/json",
        "X-Telegram-Init-Data": INIT_DATA,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (res.status === 401) {
      throw new Error(S("miniapp.status_auth"));
    }
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const data = await res.json();
        if (data.detail) detail = String(data.detail);
      } catch {
        /* keep default detail */
      }
      throw new Error(detail);
    }
    if (res.status === 204) return null;
    return res.json();
  }

  function setStatus(text, isError = false) {
    statusEl.textContent = text;
    statusEl.className = isError ? "text-red-400" : "text-slate-400";
    if (!isError) {
      setTimeout(() => {
        if (statusEl.textContent === text) {
          statusEl.textContent = "";
        }
      }, 4000);
    }
  }

  /* ------------------------------------------------------------------ */
  /* DOM helpers                                                         */
  /* ------------------------------------------------------------------ */

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "class") node.className = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (value !== null && value !== undefined) node.setAttribute(key, value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined) continue;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  const card = (children) =>
    el("div", { class: "bg-slate-900 rounded-xl p-3 space-y-2 border border-slate-800" }, children);

  const badge = (text, color) =>
    el("span", { class: `text-xs px-2 py-0.5 rounded-full ${color}` }, text);

  const btn = (label, onClick, cls = "bg-slate-800 hover:bg-slate-700") =>
    el(
      "button",
      { class: `text-sm px-3 py-1.5 rounded-lg ${cls}`, onclick: onClick },
      label
    );

  const input = (attrs) =>
    el("input", Object.assign({ class: "w-full bg-slate-800 rounded-lg px-3 py-2 text-sm" }, attrs));

  const select = (options, value) => {
    const node = el(
      "select",
      { class: "w-full bg-slate-800 rounded-lg px-3 py-2 text-sm" },
      options.map(([optValue, label]) =>
        el("option", { value: optValue, ...(optValue === value ? { selected: "selected" } : {}) }, label)
      )
    );
    node.value = value;
    return node;
  };

  const fmtDT = (iso) =>
    iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "";

  const PRIORITY_COLORS = {
    high: "bg-red-500/20 text-red-300",
    normal: "bg-slate-600/40 text-slate-300",
    low: "bg-slate-700/40 text-slate-400",
  };

  /* ------------------------------------------------------------------ */
  /* Item rendering + actions                                            */
  /* ------------------------------------------------------------------ */

  function itemCard(item) {
    const actions = [];
    if (item.status === "scheduled") {
      actions.push(
        btn(S("miniapp.btn_done"), async () => {
          try {
            await api(`/api/v1/items/${item.id}/complete`, "POST");
            await render();
          } catch (e) {
            setStatus(e.message, true);
          }
        }, "bg-emerald-600 hover:bg-emerald-500"),
        btn(S("miniapp.btn_cancel"), async () => {
          try {
            await api(`/api/v1/items/${item.id}/cancel`, "POST");
            await render();
          } catch (e) {
            setStatus(e.message, true);
          }
        }),
        btn(S("miniapp.btn_delete"), async () => {
          try {
            await api(`/api/v1/items/${item.id}`, "DELETE");
            await render();
          } catch (e) {
            setStatus(e.message, true);
          }
        }, "bg-red-900/60 hover:bg-red-800")
      );
    }
    return card([
      el(
        "div",
        { class: "flex items-center justify-between gap-2" },
        el("div", { class: "flex items-center gap-2 min-w-0" },
          el("span", {}, item.kind === "event" ? "📌" : "✅"),
          el("span", { class: "font-medium truncate" }, item.title)
        ),
        badge(item.priority, PRIORITY_COLORS[item.priority] || PRIORITY_COLORS.normal)
      ),
      el("div", { class: "text-xs text-slate-400 space-x-2" },
        item.starts_at ? el("span", {}, S("miniapp.item_starts", { when: fmtDT(item.starts_at) })) : null,
        item.due_at ? el("span", {}, S("miniapp.item_due", { when: fmtDT(item.due_at) })) : null,
        item.status !== "scheduled" ? badge(item.status, "bg-slate-700 text-slate-300") : null
      ),
      actions.length ? el("div", { class: "flex gap-2" }, actions) : null
    ]);
  }

  const empty = (text) => el("p", { class: "text-slate-500 text-sm" }, text);

  /* ------------------------------------------------------------------ */
  /* Views                                                               */
  /* ------------------------------------------------------------------ */

  async function viewToday() {
    const items = await api("/api/v1/calendar/today");
    viewEl.append(items.length ? items.map(itemCard) : empty(S("miniapp.empty_today")));
  }

  async function viewUpcoming() {
    const items = await api("/api/v1/calendar/upcoming?days=7");
    viewEl.append(items.length ? items.map(itemCard) : empty(S("miniapp.empty_upcoming")));
  }

  function viewNew() {
    const title = input({ type: "text", placeholder: S("miniapp.new_title_ph") });
    const kind = select([["task", S("miniapp.new_task")], ["event", S("miniapp.new_event")]], "task");
    const starts = input({ type: "datetime-local" });
    const due = input({ type: "datetime-local" });
    const priority = select(
      [["low", S("miniapp.new_low")], ["normal", S("miniapp.new_normal")], ["high", S("miniapp.new_high")]],
      "normal"
    );
    const remind = input({
      type: "text",
      placeholder: S("miniapp.new_reminders_ph")
    });

    viewEl.append(
      card([
        el("h2", { class: "font-semibold" }, S("miniapp.new_title")),
        title,
        el("div", { class: "grid grid-cols-2 gap-2" }, kind, priority),
        el("div", { class: "grid grid-cols-1 gap-2" },
          el("label", { class: "text-xs text-slate-400 space-y-1" }, S("miniapp.new_starts"), starts),
          el("label", { class: "text-xs text-slate-400 space-y-1" }, S("miniapp.new_due"), due)
        ),
        el("label", { class: "text-xs text-slate-400 space-y-1" }, S("miniapp.new_reminders"), remind),
        btn(S("miniapp.save"), async () => {
          const offsets = remind.value
            .split(",")
            .map((s) => s.trim())
            .filter((s) => s !== "")
            .map((s) => Number(s))
            .filter((n) => Number.isInteger(n));
          try {
            await api("/api/v1/items", "POST", {
              title: title.value,
              kind: kind.value,
              starts_at: starts.value ? new Date(starts.value).toISOString() : null,
              due_at: due.value ? new Date(due.value).toISOString() : null,
              priority: priority.value,
              remind_offsets_minutes: offsets,
            });
            setStatus(S("miniapp.saved"));
            tab = "today";
            await render();
          } catch (e) {
            setStatus(e.message, true);
          }
        }, "bg-indigo-600 hover:bg-indigo-500")
      ])
    );
  }

  async function viewWorkouts() {
    const [stats, logs] = await Promise.all([
      api("/api/v1/workouts/stats"),
      api("/api/v1/workouts?limit=10"),
    ]);
    viewEl.append(
      card([
        el("h2", { class: "font-semibold" }, S("miniapp.workouts_stats")),
        el("div", { class: "grid grid-cols-2 gap-2 text-sm" },
          el("div", {}, el("div", { class: "text-2xl font-bold" }, String(stats.total)), el("div", { class: "text-xs text-slate-400" }, S("miniapp.stats_total"))),
          el("div", {}, el("div", { class: "text-2xl font-bold" }, String(stats.total_minutes)), el("div", { class: "text-xs text-slate-400" }, S("miniapp.stats_minutes"))),
          el("div", {}, el("div", { class: "text-2xl font-bold" }, String(stats.this_week)), el("div", { class: "text-xs text-slate-400" }, S("miniapp.stats_week"))),
          el("div", {}, el("div", { class: "text-2xl font-bold" }, String(stats.current_streak)), el("div", { class: "text-xs text-slate-400" }, S("miniapp.stats_streak")))
        )
      ])
    );

    const name = input({ type: "text", placeholder: S("miniapp.workout_name_ph") });
    const when = input({ type: "datetime-local" });
    const minutes = input({ type: "number", placeholder: S("miniapp.minutes_ph"), min: "1" });
    const effort = select(
      [
        ["", S("miniapp.effort")],
        ...Array.from({ length: 10 }, (_, i) => [String(i + 1), `${i + 1} / 10`]),
      ],
      ""
    );
    viewEl.append(
      card([
        el("h2", { class: "font-semibold" }, S("miniapp.workout_log")),
        name,
        el("div", { class: "grid grid-cols-2 gap-2" },
          el("label", { class: "text-xs text-slate-400 space-y-1" }, S("miniapp.when"), when),
          el("label", { class: "text-xs text-slate-400 space-y-1" }, S("miniapp.minutes"), minutes)
        ),
        effort,
        btn(S("miniapp.log"), async () => {
          try {
            await api("/api/v1/workouts", "POST", {
              name: name.value,
              started_at: when.value ? new Date(when.value).toISOString() : null,
              duration_minutes: minutes.value ? Number(minutes.value) : null,
              perceived_effort: effort.value ? Number(effort.value) : null,
            });
            setStatus(S("miniapp.workout_logged"));
            await render();
          } catch (e) {
            setStatus(e.message, true);
          }
        }, "bg-indigo-600 hover:bg-indigo-500")
      ])
    );

    viewEl.append(el("h2", { class: "font-semibold" }, S("miniapp.recent")));
    viewEl.append(
      logs.length
        ? logs.map((w) =>
            card([
              el("div", { class: "flex justify-between" },
                el("span", { class: "font-medium" }, w.name),
                el("span", { class: "text-xs text-slate-400" }, fmtDT(w.started_at))
              ),
              el("div", { class: "text-xs text-slate-400" },
                [w.duration_minutes ? S("miniapp.minutes_value", { minutes: w.duration_minutes }) : null,
                 w.perceived_effort ? S("miniapp.effort_value", { value: w.perceived_effort }) : null]
                  .filter(Boolean).join(" · ") || w.status
              )
            ])
          )
        : empty(S("miniapp.workouts_empty"))
    );
  }

  async function viewFiles() {
    const query = input({ type: "text", placeholder: S("miniapp.search_ph") });
    const results = el("div", { class: "space-y-2" });
    const doSearch = async () => {
      const q = query.value.trim();
      if (!q) return;
      results.replaceChildren(empty(S("miniapp.searching")));
      try {
        const hits = await api(`/api/v1/files/search?q=${encodeURIComponent(q)}&top_k=5`);
        results.replaceChildren(
          hits.length
            ? hits.map((c) =>
                card([
                  el("div", { class: "text-xs text-indigo-300" }, S("miniapp.search_chunk", { file: c.file_name, position: c.position + 1 })),
                  el("p", { class: "text-sm text-slate-300" }, c.text.length > 300 ? c.text.slice(0, 300) + "…" : c.text)
                ])
              )
            : empty(S("miniapp.search_empty"))
        );
      } catch (e) {
        results.replaceChildren(empty(e.message));
      }
    };
    viewEl.append(
      card([
        el("h2", { class: "font-semibold" }, S("miniapp.search")),
        el("div", { class: "flex gap-2" }, query, btn(S("miniapp.search"), doSearch, "bg-indigo-600 hover:bg-indigo-500"))
      ]),
      results
    );

    const files = await api("/api/v1/files?limit=20");
    viewEl.append(el("h2", { class: "font-semibold" }, S("miniapp.my_files")));
    viewEl.append(
      files.length
        ? files.map((f) =>
            card([
              el("div", { class: "flex justify-between items-center gap-2" },
                el("span", { class: "font-medium truncate" }, f.original_filename),
                badge(f.state, f.state === "indexed" ? "bg-emerald-600/40 text-emerald-300" : f.state === "failed" || f.state === "rejected" ? "bg-red-600/40 text-red-300" : "bg-amber-600/40 text-amber-300")
              ),
              el("div", { class: "text-xs text-slate-400" },
                fmtDT(f.created_at) + (f.error ? ` — ${f.error}` : "")),
              el("div", {}, btn(S("miniapp.btn_delete"), async () => {
                try {
                  await api(`/api/v1/files/${f.id}`, "DELETE");
                  await render();
                } catch (e) {
                  setStatus(e.message, true);
                }
              }, "bg-red-900/60 hover:bg-red-800"))
            ])
          )
        : empty(S("miniapp.files_empty"))
    );
  }

  async function viewFacts() {
    const value = input({ type: "text", placeholder: S("miniapp.facts_value_ph") });
    const category = input({ type: "text", placeholder: S("miniapp.facts_category_ph") });
    viewEl.append(
      card([
        el("h2", { class: "font-semibold" }, S("miniapp.facts_add")),
        value,
        category,
        btn(S("miniapp.propose"), async () => {
          try {
            await api("/api/v1/facts", "POST", {
              value: value.value,
              category: category.value || null,
            });
            setStatus(S("miniapp.proposed"));
            await render();
          } catch (e) {
            setStatus(e.message, true);
          }
        }, "bg-indigo-600 hover:bg-indigo-500")
      ])
    );

    const facts = await api("/api/v1/facts?limit=50");
    viewEl.append(el("h2", { class: "font-semibold" }, S("miniapp.my_facts")));
    viewEl.append(
      facts.length
        ? facts.map((f) =>
            card([
              el("div", { class: "flex justify-between items-center gap-2" },
                el("span", { class: "text-xs text-slate-400" }, f.category),
                badge(f.status, f.status === "confirmed" ? "bg-emerald-600/40 text-emerald-300" : f.status === "proposed" ? "bg-amber-600/40 text-amber-300" : "bg-slate-700 text-slate-400")
              ),
              el("p", { class: "text-sm" }, f.value),
              el("div", { class: "flex gap-2" },
                f.status === "proposed" ? btn(S("miniapp.confirm"), async () => {
                  try {
                    await api(`/api/v1/facts/${f.id}/confirm`, "POST");
                    await render();
                  } catch (e) {
                    setStatus(e.message, true);
                  }
                }, "bg-emerald-600 hover:bg-emerald-500") : null,
                f.status === "proposed" ? btn(S("miniapp.reject"), async () => {
                  try {
                    await api(`/api/v1/facts/${f.id}/reject`, "POST");
                    await render();
                  } catch (e) {
                    setStatus(e.message, true);
                  }
                }) : null,
                btn(S("miniapp.btn_delete"), async () => {
                  try {
                    await api(`/api/v1/facts/${f.id}`, "DELETE");
                    await render();
                  } catch (e) {
                    setStatus(e.message, true);
                  }
                }, "bg-red-900/60 hover:bg-red-800")
              )
            ])
          )
        : empty(S("miniapp.facts_empty"))
    );
  }

  async function viewSettings() {
    const settings = me.settings;
    const timezones = [
      "UTC", "Europe/Berlin", "Europe/Madrid", "Europe/Paris", "Europe/London",
      "Europe/Kyiv", "Europe/Moscow", "America/New_York", "America/Chicago",
      "America/Los_Angeles", "Asia/Tokyo", "Asia/Shanghai", "Asia/Karachi",
      "Australia/Sydney",
    ];
    const tz = select(timezones.map((z) => [z, z]), settings.timezone);
    if (!timezones.includes(settings.timezone)) {
      tz.append(el("option", { value: settings.timezone }, settings.timezone));
      tz.value = settings.timezone;
    }
    const digest = input({ type: "time", value: settings.digest_time.slice(0, 5) });
    const motivation = el("input", { type: "checkbox" });
    motivation.checked = settings.motivation_enabled;
    const languages = await api("/api/v1/i18n/languages");
    const language = select(
      languages.map((l) => [l.code, l.label]),
      settings.language
    );

    viewEl.append(
      card([
        el("h2", { class: "font-semibold" }, S("miniapp.settings")),
        el("label", { class: "text-xs text-slate-400 space-y-1 block" }, S("miniapp.settings_timezone"), tz),
        el("label", { class: "text-xs text-slate-400 space-y-1 block" }, S("miniapp.settings_digest"), digest),
        el("label", { class: "text-xs text-slate-400 space-y-1 block" }, S("miniapp.settings_language"), language),
        el("label", { class: "flex items-center gap-2 text-sm" }, motivation, S("miniapp.settings_motivation")),
        btn(S("miniapp.save"), async () => {
          try {
            const saved = await api("/api/v1/settings", "PATCH", {
              timezone: tz.value,
              digest_time: digest.value,
              motivation_enabled: motivation.checked,
              language: language.value,
            });
            me.settings = saved;
            // Reload the locale dictionary if the language changed so the
            // whole UI switches immediately, without a divergent local copy.
            if (saved.language && saved.language !== me.language) {
              me.language = saved.language;
              STR = await api(`/api/v1/i18n/${saved.language}`);
            }
            setStatus(S("miniapp.settings_saved"));
            await render();
          } catch (e) {
            setStatus(e.message, true);
          }
        }, "bg-indigo-600 hover:bg-indigo-500")
      ])
    );
  }

  /* ------------------------------------------------------------------ */
  /* Boot                                                                */
  /* ------------------------------------------------------------------ */

  const VIEWS = {
    today: viewToday,
    upcoming: viewUpcoming,
    new: viewNew,
    workouts: viewWorkouts,
    files: viewFiles,
    facts: viewFacts,
    settings: viewSettings,
  };

  async function render() {
    viewEl.replaceChildren();
    tabsEl.replaceChildren();
    for (const [key, keyStr] of TAB_KEYS) {
      const active = key === tab;
      tabsEl.append(
        btn(S(keyStr), () => {
          tab = key;
          render();
        }, active ? "bg-indigo-600 hover:bg-indigo-500" : "bg-slate-800 hover:bg-slate-700")
      );
    }
    await VIEWS[tab]();
  }

  async function boot() {
    if (!INIT_DATA) {
      setStatus(S("miniapp.status_open"), true);
      return;
    }
    try {
      me = await api("/api/v1/me");
      me.language = me.settings.language;
      STR = await api(`/api/v1/i18n/${me.language}`);
      whoEl.textContent = [me.user.first_name, me.user.last_name].filter(Boolean).join(" ");
      setStatus("");
      await render();
    } catch (e) {
      setStatus(e.message, true);
    }
  }

  boot();
})();

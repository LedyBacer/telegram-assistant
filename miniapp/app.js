/* Smart Assistant Mini App: vanilla JS + Tailwind (SPEC §18).
 * Every request sends the raw Telegram.WebApp.initData; the backend verifies
 * its HMAC signature before trusting the identity. All user-supplied strings
 * are rendered through textContent, never innerHTML. */
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

  const TABS = [
    ["today", "📅 Today"],
    ["upcoming", "🗓️ Upcoming"],
    ["new", "➕ New"],
    ["workouts", "💪 Workouts"],
    ["files", "📁 Files"],
    ["facts", "🧠 Facts"],
    ["settings", "⚙️ Settings"],
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
      throw new Error("Authentication failed — reopen the app from the bot.");
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
        btn("✓ Done", async () => {
          try {
            await api(`/api/v1/items/${item.id}/complete`, "POST");
            await render();
          } catch (e) {
            setStatus(e.message, true);
          }
        }, "bg-emerald-600 hover:bg-emerald-500"),
        btn("Cancel", async () => {
          try {
            await api(`/api/v1/items/${item.id}/cancel`, "POST");
            await render();
          } catch (e) {
            setStatus(e.message, true);
          }
        }),
        btn("Delete", async () => {
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
        item.starts_at ? el("span", {}, `Starts ${fmtDT(item.starts_at)}`) : null,
        item.due_at ? el("span", {}, `Due ${fmtDT(item.due_at)}`) : null,
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
    viewEl.append(items.length ? items.map(itemCard) : empty("Nothing scheduled for today."));
  }

  async function viewUpcoming() {
    const items = await api("/api/v1/calendar/upcoming?days=7");
    viewEl.append(items.length ? items.map(itemCard) : empty("Nothing in the next 7 days."));
  }

  function viewNew() {
    const title = input({ type: "text", placeholder: "Title" });
    const kind = select([["task", "Task"], ["event", "Event"]], "task");
    const starts = input({ type: "datetime-local" });
    const due = input({ type: "datetime-local" });
    const priority = select([["low", "Low"], ["normal", "Normal"], ["high", "High"]], "normal");
    const remind = input({
      type: "text",
      placeholder: "Remind (minutes before, e.g. 30,0 — optional)"
    });

    viewEl.append(
      card([
        el("h2", { class: "font-semibold" }, "New task / event"),
        title,
        el("div", { class: "grid grid-cols-2 gap-2" }, kind, priority),
        el("div", { class: "grid grid-cols-1 gap-2" },
          el("label", { class: "text-xs text-slate-400 space-y-1" }, "Starts", starts),
          el("label", { class: "text-xs text-slate-400 space-y-1" }, "Due", due)
        ),
        el("label", { class: "text-xs text-slate-400 space-y-1" }, "Reminders", remind),
        btn("Save", async () => {
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
            setStatus("Saved.");
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
        el("h2", { class: "font-semibold" }, "Stats"),
        el("div", { class: "grid grid-cols-2 gap-2 text-sm" },
          el("div", {}, el("div", { class: "text-2xl font-bold" }, String(stats.total)), el("div", { class: "text-xs text-slate-400" }, "total workouts")),
          el("div", {}, el("div", { class: "text-2xl font-bold" }, String(stats.total_minutes)), el("div", { class: "text-xs text-slate-400" }, "total minutes")),
          el("div", {}, el("div", { class: "text-2xl font-bold" }, String(stats.this_week)), el("div", { class: "text-xs text-slate-400" }, "this week")),
          el("div", {}, el("div", { class: "text-2xl font-bold" }, String(stats.current_streak)), el("div", { class: "text-xs text-slate-400" }, "day streak"))
        )
      ])
    );

    const name = input({ type: "text", placeholder: "Workout name (e.g. Push day)" });
    const when = input({ type: "datetime-local" });
    const minutes = input({ type: "number", placeholder: "Minutes", min: "1" });
    const effort = select(
      [["", "Effort (optional)"].concat(
        Array.from({ length: 10 }, (_, i) => [String(i + 1), `${i + 1} / 10`])
      )),
      ""
    );
    viewEl.append(
      card([
        el("h2", { class: "font-semibold" }, "Log a workout"),
        name,
        el("div", { class: "grid grid-cols-2 gap-2" },
          el("label", { class: "text-xs text-slate-400 space-y-1" }, "When", when),
          el("label", { class: "text-xs text-slate-400 space-y-1" }, "Minutes", minutes)
        ),
        effort,
        btn("Log", async () => {
          try {
            await api("/api/v1/workouts", "POST", {
              name: name.value,
              started_at: when.value ? new Date(when.value).toISOString() : null,
              duration_minutes: minutes.value ? Number(minutes.value) : null,
              perceived_effort: effort.value ? Number(effort.value) : null,
            });
            setStatus("Workout logged.");
            await render();
          } catch (e) {
            setStatus(e.message, true);
          }
        }, "bg-indigo-600 hover:bg-indigo-500")
      ])
    );

    viewEl.append(el("h2", { class: "font-semibold" }, "Recent"));
    viewEl.append(
      logs.length
        ? logs.map((w) =>
            card([
              el("div", { class: "flex justify-between" },
                el("span", { class: "font-medium" }, w.name),
                el("span", { class: "text-xs text-slate-400" }, fmtDT(w.started_at))
              ),
              el("div", { class: "text-xs text-slate-400" },
                [w.duration_minutes ? `${w.duration_minutes} min` : null,
                 w.perceived_effort ? `effort ${w.perceived_effort}/10` : null]
                  .filter(Boolean).join(" · ") || w.status
              )
            ])
          )
        : empty("No workouts logged yet.")
    );
  }

  async function viewFiles() {
    const query = input({ type: "text", placeholder: "Search your files…" });
    const results = el("div", { class: "space-y-2" });
    const doSearch = async () => {
      const q = query.value.trim();
      if (!q) return;
      results.replaceChildren(empty("Searching…"));
      try {
        const hits = await api(`/api/v1/files/search?q=${encodeURIComponent(q)}&top_k=5`);
        results.replaceChildren(
          hits.length
            ? hits.map((c) =>
                card([
                  el("div", { class: "text-xs text-indigo-300" }, `${c.file_name} · chunk ${c.position + 1}`),
                  el("p", { class: "text-sm text-slate-300" }, c.text.length > 300 ? c.text.slice(0, 300) + "…" : c.text)
                ])
              )
            : empty("No matches.")
        );
      } catch (e) {
        results.replaceChildren(empty(e.message));
      }
    };
    viewEl.append(
      card([
        el("h2", { class: "font-semibold" }, "Search"),
        el("div", { class: "flex gap-2" }, query, btn("Search", doSearch, "bg-indigo-600 hover:bg-indigo-500"))
      ]),
      results
    );

    const files = await api("/api/v1/files?limit=20");
    viewEl.append(el("h2", { class: "font-semibold" }, "My files"));
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
              el("div", {}, btn("Delete", async () => {
                try {
                  await api(`/api/v1/files/${f.id}`, "DELETE");
                  await render();
                } catch (e) {
                  setStatus(e.message, true);
                }
              }, "bg-red-900/60 hover:bg-red-800"))
            ])
          )
        : empty("No files uploaded yet — send a document to the bot.")
    );
  }

  async function viewFacts() {
    const value = input({ type: "text", placeholder: "Tell the assistant something to remember" });
    const category = input({ type: "text", placeholder: "Category (optional)" });
    viewEl.append(
      card([
        el("h2", { class: "font-semibold" }, "Add a fact (stays proposed until confirmed)"),
        value,
        category,
        btn("Propose", async () => {
          try {
            await api("/api/v1/facts", "POST", {
              value: value.value,
              category: category.value || null,
            });
            setStatus("Proposed.");
            await render();
          } catch (e) {
            setStatus(e.message, true);
          }
        }, "bg-indigo-600 hover:bg-indigo-500")
      ])
    );

    const facts = await api("/api/v1/facts?limit=50");
    viewEl.append(el("h2", { class: "font-semibold" }, "My facts"));
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
                f.status === "proposed" ? btn("Confirm", async () => {
                  try {
                    await api(`/api/v1/facts/${f.id}/confirm`, "POST");
                    await render();
                  } catch (e) {
                    setStatus(e.message, true);
                  }
                }, "bg-emerald-600 hover:bg-emerald-500") : null,
                f.status === "proposed" ? btn("Reject", async () => {
                  try {
                    await api(`/api/v1/facts/${f.id}/reject`, "POST");
                    await render();
                  } catch (e) {
                    setStatus(e.message, true);
                  }
                }) : null,
                btn("Delete", async () => {
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
        : empty("No facts yet.")
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

    viewEl.append(
      card([
        el("h2", { class: "font-semibold" }, "Settings"),
        el("label", { class: "text-xs text-slate-400 space-y-1 block" }, "Timezone", tz),
        el("label", { class: "text-xs text-slate-400 space-y-1 block" }, "Daily digest time", digest),
        el("label", { class: "flex items-center gap-2 text-sm" }, motivation, "Send motivational messages"),
        btn("Save", async () => {
          try {
            const saved = await api("/api/v1/settings", "PATCH", {
              timezone: tz.value,
              digest_time: digest.value,
              motivation_enabled: motivation.checked,
            });
            me.settings = saved;
            setStatus("Settings saved.");
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
    for (const [key, label] of TABS) {
      const active = key === tab;
      tabsEl.append(
        btn(label, () => {
          tab = key;
          render();
        }, active ? "bg-indigo-600 hover:bg-indigo-500" : "bg-slate-800 hover:bg-slate-700")
      );
    }
    await VIEWS[tab]();
  }

  async function boot() {
    if (!INIT_DATA) {
      setStatus("Open this page inside Telegram to continue.", true);
      return;
    }
    try {
      me = await api("/api/v1/me");
      whoEl.textContent = [me.user.first_name, me.user.last_name].filter(Boolean).join(" ");
      setStatus("");
      await render();
    } catch (e) {
      setStatus(e.message, true);
    }
  }

  boot();
})();

/* Global Mini App entry point; feature modules are wired in during the Mini App milestone. */
(() => {
  "use strict";

  const tg = window.Telegram?.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
  }

  const statusEl = document.getElementById("status");
  const initData = tg?.initData || "";

  function setStatus(text) {
    if (statusEl) statusEl.textContent = text;
  }

  if (!initData) {
    setStatus("Open this page inside Telegram to continue.");
    return;
  }

  fetch("/api/v1/me", { headers: { "X-Telegram-Init-Data": initData } })
    .then((res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    })
    .then((user) => {
      const name = [user.first_name, user.last_name].filter(Boolean).join(" ");
      setStatus(name ? `Signed in as ${name}` : "Signed in.");
    })
    .catch(() => setStatus("Could not reach the assistant API."));
})();

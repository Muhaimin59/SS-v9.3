/* =========================================================================
   SmartServe V10 — app shell behaviour
   Vanilla JS only, no build step. Every feature degrades gracefully.
   ========================================================================= */
(function () {
  "use strict";

  /* ---------------- Theme (shared preference with the V9.3 shell) ------- */
  const THEME_KEY = "smartServeTheme";

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    document.querySelectorAll("[data-theme-icon]").forEach(function (el) {
      el.textContent = theme === "dark" ? "☀" : "☾";
    });
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "dark" ? "#060c17" : "#1663f0");
  }

  window.v10ToggleTheme = function () {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
    applyTheme(next);
    return next;
  };

  (function initTheme() {
    let stored = "light";
    try { stored = localStorage.getItem(THEME_KEY) || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"); } catch (e) {}
    applyTheme(stored);
  })();

  /* ---------------- Toasts --------------------------------------------- */
  function toastStack() {
    let stack = document.getElementById("v10Toasts");
    if (!stack) {
      stack = document.createElement("div");
      stack.id = "v10Toasts";
      stack.className = "v10-toasts";
      stack.setAttribute("aria-live", "polite");
      document.body.appendChild(stack);
    }
    return stack;
  }

  window.v10Toast = function (message, kind) {
    if (!message) return;
    const stack = toastStack();
    const el = document.createElement("div");
    el.className = "v10-toast " + (kind || "");
    el.setAttribute("role", "status");
    el.innerHTML = '<span>' + escapeHtml(message) + "</span>" +
      '<button type="button" aria-label="Dismiss">×</button>';
    el.querySelector("button").addEventListener("click", function () { el.remove(); });
    stack.appendChild(el);
    setTimeout(function () {
      el.style.transition = "opacity .35s, transform .35s";
      el.style.opacity = "0";
      el.style.transform = "translateY(-8px)";
      setTimeout(function () { el.remove(); }, 360);
    }, kind === "error" ? 7000 : 4200);
  };

  function escapeHtml(value) {
    if (window.escapeHtml && window.escapeHtml !== escapeHtml) return window.escapeHtml(value);
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] || c;
    });
  }

  /* ---------------- API helper ----------------------------------------- */
  window.v10Api = async function (url, options) {
    const config = Object.assign({ method: "GET" }, options || {});
    const isForm = config.body instanceof FormData;
    if (config.body && !isForm && typeof config.body !== "string") {
      config.headers = Object.assign({ "Content-Type": "application/json" }, config.headers || {});
      config.body = JSON.stringify(config.body);
    }
    config.credentials = "same-origin";
    config.headers = Object.assign({ "X-Requested-With": "XMLHttpRequest" }, config.headers || {});
    const response = await fetch(url, config);
    let payload = {};
    try { payload = await response.json(); } catch (e) { payload = {}; }
    if (!response.ok || payload.success === false) {
      const error = new Error(payload.message || "Something went wrong. Please try again.");
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  };

  window.v10Loading = function (button, label) {
    if (!button) return function () {};
    const original = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<span class="spinner' + (button.classList.contains("primary") ? "" : " dark") + '"></span> ' +
      escapeHtml(label || "Working…");
    return function () { button.disabled = false; button.innerHTML = original; };
  };

  /* ---------------- Bottom sheet --------------------------------------- */
  window.v10Sheet = function (id) {
    const sheet = document.getElementById(id);
    if (!sheet) return;
    let backdrop = document.getElementById(id + "Backdrop");
    if (!backdrop) {
      backdrop = document.createElement("div");
      backdrop.id = id + "Backdrop";
      backdrop.className = "sheet-backdrop";
      document.body.appendChild(backdrop);
      backdrop.addEventListener("click", function () { window.v10CloseSheet(id); });
    }
    sheet.classList.add("open");
    backdrop.classList.add("open");
    document.body.style.overflow = "hidden";
    const focusable = sheet.querySelector("input, textarea, select, button");
    if (focusable) setTimeout(function () { focusable.focus(); }, 220);
  };

  window.v10CloseSheet = function (id) {
    const sheet = document.getElementById(id);
    const backdrop = document.getElementById(id + "Backdrop");
    if (sheet) sheet.classList.remove("open");
    if (backdrop) backdrop.classList.remove("open");
    document.body.style.overflow = "";
  };

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    document.querySelectorAll(".sheet.open").forEach(function (sheet) {
      window.v10CloseSheet(sheet.id);
    });
  });

  /* ---------------- Search: debounce + live suggestions ---------------- */
  function debounce(fn, wait) {
    let timer = null;
    return function () {
      const args = arguments, context = this;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(context, args); }, wait);
    };
  }
  window.v10Debounce = debounce;

  function initSearch() {
    const input = document.querySelector("[data-live-search]");
    const box = document.getElementById("searchSuggestions");
    if (!input || !box) return;
    const run = debounce(async function () {
      const query = input.value.trim();
      if (query.length < 3) { box.innerHTML = ""; box.style.display = "none"; return; }
      box.style.display = "block";
      box.innerHTML = '<div class="skeleton line w60"></div><div class="skeleton line"></div>';
      try {
        const data = await window.v10Api("/api/search?q=" + encodeURIComponent(query));
        const results = data.results || {};
        const items = [];
        (results.suggestions || []).slice(0, 3).forEach(function (suggestion) {
          items.push('<a class="suggestion" href="/search?q=' + encodeURIComponent(suggestion.service || "") + '">' +
            '<span class="ico">🧠</span><span><strong>' + escapeHtml(suggestion.service || "") + '</strong>' +
            '<span class="muted"> ' + escapeHtml(suggestion.reason || "") + '</span></span></a>');
        });
        (results.services || []).slice(0, 5).forEach(function (service) {
          items.push('<a class="suggestion" href="/book/' + service.id + '">' +
            '<span class="ico">' + escapeHtml(service.icon || "🛠️") + '</span><span><strong>' + escapeHtml(service.name) +
            '</strong><span class="muted"> ' + escapeHtml(service.category || "") + '</span></span></a>');
        });
        (results.providers || []).slice(0, 3).forEach(function (provider) {
          items.push('<a class="suggestion" href="/provider/' + provider.provider_id + '">' +
            '<span class="ico">👷</span><span><strong>' + escapeHtml(provider.name) +
            '</strong><span class="muted"> ★ ' + escapeHtml(String(provider.rating || 0)) + ' · ' + escapeHtml(provider.skills || "") + '</span></span></a>');
        });
        box.innerHTML = items.join("") || '<div class="suggestion muted">No matches yet — describe the problem instead.</div>';
      } catch (error) {
        box.innerHTML = '<div class="suggestion muted">' + escapeHtml(error.message) + "</div>";
      }
    }, 260);
    input.addEventListener("input", run);
    input.addEventListener("focus", function () {
      if (input.value.trim().length >= 3) run();
    });
    document.addEventListener("click", function (event) {
      if (!box.contains(event.target) && event.target !== input) box.style.display = "none";
    });
  }

  /* ---------------- Voice input (search + problem description) ---------- */
  function initVoice() {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    document.querySelectorAll("[data-voice-target]").forEach(function (button) {
      if (!Recognition) { button.style.display = "none"; return; }
      button.addEventListener("click", function () {
        const target = document.getElementById(button.dataset.voiceTarget);
        if (!target) return;
        const recognition = new Recognition();
        recognition.lang = button.dataset.voiceLang || "en-IN";
        recognition.interimResults = false;
        recognition.maxAlternatives = 1;
        button.classList.add("listening");
        recognition.onresult = function (event) {
          const text = event.results[0][0].transcript;
          target.value = (target.value ? target.value + " " : "") + text;
          target.dispatchEvent(new Event("input", { bubbles: true }));
          window.v10Toast("Voice captured.", "success");
        };
        recognition.onerror = function () { window.v10Toast("Voice input is unavailable in this browser.", "warn"); };
        recognition.onend = function () { button.classList.remove("listening"); };
        recognition.start();
      });
    });
  }

  /* ---------------- Booking stepper ------------------------------------ */
  function initStepper() {
    const root = document.querySelector("[data-stepper]");
    if (!root) return;
    const panels = Array.prototype.slice.call(root.querySelectorAll("[data-step-panel]"));
    const steps = Array.prototype.slice.call(root.querySelectorAll("[data-step]"));
    if (!panels.length) return;
    let index = 0;

    function render() {
      panels.forEach(function (panel, i) { panel.hidden = i !== index; });
      steps.forEach(function (step, i) {
        step.classList.toggle("active", i === index);
        step.classList.toggle("done", i < index);
        step.setAttribute("aria-current", i === index ? "step" : "false");
      });
      const counter = root.querySelector("[data-step-counter]");
      if (counter) counter.textContent = "Step " + (index + 1) + " of " + panels.length;
      const back = root.querySelector("[data-step-back]");
      if (back) back.disabled = index === 0;
      const next = root.querySelector("[data-step-next]");
      if (next) next.textContent = index === panels.length - 1 ? (next.dataset.finalLabel || "Confirm") : "Continue →";
      const fill = root.querySelector("[data-step-progress]");
      if (fill) fill.style.width = Math.round(((index + 1) / panels.length) * 100) + "%";
      window.scrollTo({ top: Math.max(0, root.getBoundingClientRect().top + window.scrollY - 70), behavior: "smooth" });
    }

    function validate(index) {
      const panel = panels[index];
      const required = panel.querySelectorAll("[data-required]");
      for (let i = 0; i < required.length; i += 1) {
        const field = required[i];
        if (!field.value || !String(field.value).trim()) {
          field.focus();
          window.v10Toast(field.dataset.requiredMessage || "Please complete this step.", "warn");
          return false;
        }
      }
      return true;
    }

    root.querySelectorAll("[data-step-next]").forEach(function (button) {
      button.addEventListener("click", function () {
        if (!validate(index)) return;
        if (index < panels.length - 1) { index += 1; render(); }
        else if (typeof root.submitBooking === "function") root.submitBooking();
      });
    });
    root.querySelectorAll("[data-step-back]").forEach(function (button) {
      button.addEventListener("click", function () {
        if (index > 0) { index -= 1; render(); }
      });
    });
    steps.forEach(function (step, i) {
      step.addEventListener("click", function () {
        if (i <= index) { index = i; render(); }
      });
    });
    root.v10GoToStep = function (target) {
      index = Math.max(0, Math.min(panels.length - 1, target));
      render();
    };
    render();
  }

  /* ---------------- Socket.IO reuse ------------------------------------ */
  function initRealtime() {
    const requestId = document.body.dataset.requestId;
    if (!requestId || !window.io) return;
    let socket = window.smartSocket;
    if (!socket) {
      try {
        socket = window.io({ transports: ["websocket", "polling"], withCredentials: true });
        window.smartSocket = socket;
      } catch (e) { return; }
    }
    socket.emit("join_request", { request_id: Number(requestId) });
    ["request_update", "mission_created", "new_message", "scheduled_service_ready"].forEach(function (event) {
      socket.on(event, function () {
        const counter = document.querySelector("[data-live-counter]");
        if (counter) counter.textContent = "● live";
        if (typeof window.v10OnRealtime === "function") window.v10OnRealtime(event);
      });
    });
  }

  /* ---------------- Service worker (PWA) ------------------------------- */
  function initServiceWorker() {
    if (!("serviceWorker" in navigator)) return;
    if (location.protocol !== "https:" && !["localhost", "127.0.0.1"].includes(location.hostname)) return;
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js").catch(function () { /* offline shell is optional */ });
    });
  }

  /* ---------------- Copy to clipboard ---------------------------------- */
  window.v10Copy = async function (text, label) {
    try {
      await navigator.clipboard.writeText(text);
      window.v10Toast((label || "Copied") + " to clipboard.", "success");
    } catch (e) {
      window.v10Toast("Copy is not available — select the text manually.", "warn");
    }
  };

  document.addEventListener("DOMContentLoaded", function () {
    initSearch();
    initVoice();
    initStepper();
    initRealtime();
    initServiceWorker();
    document.querySelectorAll("[data-auto-dismiss]").forEach(function (el) {
      setTimeout(function () { el.remove(); }, 7000);
    });
  });
})();

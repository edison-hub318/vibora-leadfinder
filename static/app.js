/* Vibora lead terminal - small vanilla JS, no build step. */
(function () {
  "use strict";
  const $ = (s, el) => (el || document).querySelector(s);
  const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
  const csrf = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };

  /* ---------- theme (dark default, remembered) ---------- */
  window.lfToggleTheme = function () {
    const cur = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    const next = cur === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    store.set("lf-theme", next);
    document.dispatchEvent(new Event("lf-theme"));
  };

  /* ---------- CSRF on every HTMX request ---------- */
  document.addEventListener("htmx:configRequest", (e) => { e.detail.headers["X-CSRF-Token"] = csrf(); });

  /* ---------- toasts ---------- */
  function toast(message, kind, calendar) {
    const box = $("#toasts");
    if (!box) return;
    const t = document.createElement("div");
    t.className = "toast" + (kind === "error" ? " error" : "");
    t.textContent = message;
    if (calendar) {
      const row = document.createElement("div");
      row.className = "row";
      row.innerHTML = '<a class="btn btn-secondary" href="' + calendar.ics + '">Download .ics</a>' +
        '<a class="btn btn-secondary" target="_blank" rel="noopener" href="' + calendar.google + '">Google Calendar</a>';
      t.appendChild(row);
    }
    box.appendChild(t);
    setTimeout(() => t.remove(), calendar ? 15000 : 4000);
  }
  window.lfToast = toast;
  document.body.addEventListener("toast", (e) => toast(e.detail.message, e.detail.kind, e.detail.calendar));
  document.body.addEventListener("htmx:responseError", (e) => {
    if (e.detail.xhr.status === 403) toast("Security check failed. Reload the page and try again.", "error");
  });

  /* ---------- sidebar ---------- */
  if (store.get("lf-collapsed", "0") === "1") document.documentElement.classList.add("collapsed");
  window.lfCollapse = function () {
    const c = document.documentElement.classList.toggle("collapsed");
    store.set("lf-collapsed", c ? "1" : "0");
  };
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-menu-toggle]");
    const menu = $("#account-menu");
    if (btn && menu) { menu.classList.toggle("hidden"); return; }
    if (menu && !e.target.closest("#account-menu")) menu.classList.add("hidden");
    const pop = e.target.closest("[data-pop]");
    $$(".popover .pop").forEach((p) => { if (!pop || p !== pop.nextElementSibling) p.classList.add("hidden"); });
    if (pop) pop.nextElementSibling.classList.toggle("hidden");
  });

  /* ---------- tooltips for heatmap cells etc ---------- */
  const tip = document.createElement("div");
  tip.id = "tip";
  document.body.appendChild(tip);
  document.addEventListener("mouseover", (e) => {
    const el = e.target.closest("[data-tip]");
    if (!el) { tip.style.opacity = 0; return; }
    tip.textContent = el.getAttribute("data-tip");
    tip.style.opacity = 1;
  });
  document.addEventListener("mousemove", (e) => {
    tip.style.left = Math.min(e.clientX + 12, window.innerWidth - tip.offsetWidth - 8) + "px";
    tip.style.top = (e.clientY + 14) + "px";
  });

  /* ---------- charts (Chart.js, thin and monochrome) ---------- */
  function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
  const charts = [];
  function baseOpts() {
    return {
      responsive: true, maintainAspectRatio: false, animation: { duration: 150 },
      plugins: { legend: { display: false }, tooltip: {
        backgroundColor: cssVar("--primary-bg"), titleColor: cssVar("--primary-fg"), bodyColor: cssVar("--primary-fg"),
        displayColors: false, padding: 8, cornerRadius: 8 } },
      scales: {
        x: { grid: { display: false }, border: { display: false }, ticks: { color: cssVar("--text-3"), font: { size: 11 } } },
        y: { grid: { color: cssVar("--ink-1"), drawTicks: false }, border: { display: false },
             ticks: { color: cssVar("--text-3"), font: { size: 11 }, precision: 0, maxTicksLimit: 4 }, beginAtZero: true },
      },
    };
  }
  function drawCharts() {
    if (!window.Chart) return;
    charts.splice(0).forEach((c) => c.destroy());
    $$("canvas[data-spark]").forEach((cv) => {
      const data = JSON.parse(cv.getAttribute("data-spark"));
      charts.push(new Chart(cv, {
        type: "line",
        data: { labels: data.map((_, i) => i), datasets: [{ data, borderColor: cssVar("--ink-3"), borderWidth: 1.5, pointRadius: 0, tension: 0.35, fill: false }] },
        options: { responsive: false, animation: false, plugins: { legend: { display: false }, tooltip: { enabled: false } },
                   scales: { x: { display: false }, y: { display: false, beginAtZero: true } } },
      }));
    });
    $$("canvas[data-bars]").forEach((cv) => {
      const spec = JSON.parse(cv.getAttribute("data-bars"));
      const colors = (spec.colors || []).map((c) => (c ? cssVar(c) : cssVar("--ink-4")));
      const opts = baseOpts();
      if (spec.horizontal) { opts.indexAxis = "y"; opts.scales.x.grid = { color: cssVar("--ink-1"), drawTicks: false }; opts.scales.y.grid = { display: false }; opts.scales.x.beginAtZero = true; opts.scales.x.ticks.precision = 0; }
      charts.push(new Chart(cv, {
        type: "bar",
        data: { labels: spec.labels, datasets: [{ data: spec.values, backgroundColor: colors.length ? colors : cssVar("--ink-4"),
          borderRadius: 4, borderSkipped: false, maxBarThickness: 22 }] },
        options: opts,
      }));
    });
    $$("canvas[data-line]").forEach((cv) => {
      const spec = JSON.parse(cv.getAttribute("data-line"));
      const opts = baseOpts();
      opts.interaction = { mode: "index", intersect: false };
      charts.push(new Chart(cv, {
        type: "line",
        data: { labels: spec.labels, datasets: [{ data: spec.values, borderColor: cssVar("--text"), borderWidth: 2,
          pointRadius: 0, pointHoverRadius: 4, tension: 0.3 }] },
        options: opts,
      }));
    });
  }
  window.lfDrawCharts = drawCharts;
  document.addEventListener("DOMContentLoaded", drawCharts);
  document.addEventListener("lf-theme", drawCharts);
  document.body.addEventListener("htmx:afterSettle", drawCharts);

  /* ---------- leads side panel ---------- */
  window.lfOpenPanel = function (id) {
    const p = $("#side-panel");
    if (!p) return;
    htmx.ajax("GET", "/leads/" + id + "/panel", { target: "#side-panel-body" }).then(() => p.classList.add("open"));
    history.replaceState(null, "", "?open=" + id);
  };
  window.lfClosePanel = function () { const p = $("#side-panel"); if (p) p.classList.remove("open"); };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") window.lfClosePanel(); });

  /* ---------- kanban drag and drop ---------- */
  document.addEventListener("dragstart", (e) => {
    const card = e.target.closest(".kcard");
    if (!card) return;
    card.classList.add("dragging");
    e.dataTransfer.setData("text/plain", card.dataset.id);
  });
  document.addEventListener("dragend", (e) => { const c = e.target.closest(".kcard"); if (c) c.classList.remove("dragging"); });
  document.addEventListener("dragover", (e) => { const col = e.target.closest(".col"); if (col) { e.preventDefault(); col.classList.add("over"); } });
  document.addEventListener("dragleave", (e) => { const col = e.target.closest(".col"); if (col) col.classList.remove("over"); });
  document.addEventListener("drop", (e) => {
    const col = e.target.closest(".col");
    if (!col) return;
    e.preventDefault();
    col.classList.remove("over");
    const id = e.dataTransfer.getData("text/plain");
    const card = document.querySelector('.kcard[data-id="' + id + '"]');
    if (!card || card.parentElement === col.querySelector(".cards")) return;
    window.lfMoveStage(id, col.dataset.stage, card, col);
  });
  window.lfMoveStage = function (id, stage, card, col) {
    const dlg = $("#stage-dialog");
    const go = (extra) => {
      const body = new URLSearchParams(Object.assign({ lead_id: id, stage: stage }, extra || {}));
      fetch("/pipeline/move", { method: "POST", headers: { "X-CSRF-Token": csrf(), "Content-Type": "application/x-www-form-urlencoded" }, body })
        .then((r) => {
          const trig = r.headers.get("HX-Trigger");
          if (trig) { const t = JSON.parse(trig).toast; toast(t.message, t.kind); }
          if (r.ok) { col.querySelector(".cards").prepend(card); updateCounts(); }
        });
    };
    if (stage === "won" || stage === "lost") {
      $("#stage-won").classList.toggle("hidden", stage !== "won");
      $("#stage-lost").classList.toggle("hidden", stage !== "lost");
      $("#stage-title").textContent = stage === "won" ? "Deal won" : "Deal lost";
      dlg.returnValue = "";
      dlg.showModal();
      dlg.onclose = () => {
        if (dlg.returnValue !== "ok") return;
        go(stage === "won" ? { setup: $("#stage-setup").value, retainer: $("#stage-retainer").value } : { reason: $("#stage-reason").value });
      };
    } else { go(); }
  };
  function updateCounts() { $$(".col").forEach((c) => { const n = c.querySelectorAll(".kcard").length; const el = c.querySelector(".count"); if (el) el.textContent = n; }); }

  /* ---------- dial page ---------- */
  const Dial = {
    started: null, timerId: null, session: null, sessionStart: null,
    stats: { dials: 0, connects: 0, meetings: 0 },
    init() {
      if (!$("#dial-root")) return;
      const saved = JSON.parse(store.get("lf-dial-session", "null") || "null");
      if (saved && saved.id && Date.now() - saved.start < 12 * 3600e3) {
        this.session = saved.id; this.sessionStart = saved.start; this.stats = saved.stats || this.stats;
      }
      this.renderStrip();
      setInterval(() => this.renderStrip(), 1000);
      document.body.addEventListener("dialLogged", (e) => this.logged(e.detail.outcome));
      document.body.addEventListener("htmx:configRequest", (e) => {
        if (e.detail.path === "/dial/log" || e.detail.path === "/dial/dnc") {
          e.detail.parameters.duration = this.elapsed();
          if (this.session) e.detail.parameters.session_id = this.session;
        }
      });
      document.body.addEventListener("htmx:afterSwap", (e) => { if (e.target.id === "dial-card") this.resetCall(); });
      document.addEventListener("keydown", (e) => this.key(e));
    },
    elapsed() { return this.started ? Math.round((Date.now() - this.started) / 1000) : 0; },
    fmt(s) { const m = Math.floor(s / 60), r = s % 60; return (m < 10 ? "0" : "") + m + ":" + (r < 10 ? "0" : "") + r; },
    startCall() {
      if (this.started) return;
      this.started = Date.now();
      if (!this.session) this.startSession();
      const t = $("#call-timer");
      if (t) t.classList.add("on");
      this.timerId = setInterval(() => { const t2 = $("#call-timer"); if (t2) t2.textContent = this.fmt(this.elapsed()); }, 500);
      const b = $("#calling-btn"); if (b) { b.textContent = "On call"; b.classList.add("btn-secondary"); b.classList.remove("btn-primary"); }
    },
    resetCall() { clearInterval(this.timerId); this.started = null; },
    startSession() {
      fetch("/dial/session/start", { method: "POST", headers: { "X-CSRF-Token": csrf() } }).then((r) => r.json()).then((d) => {
        this.session = d.id; this.sessionStart = Date.now(); this.stats = { dials: 0, connects: 0, meetings: 0 }; this.save();
      });
    },
    endSession() {
      if (!this.session) return;
      const body = new URLSearchParams({ session_id: this.session });
      fetch("/dial/session/end", { method: "POST", headers: { "X-CSRF-Token": csrf(), "Content-Type": "application/x-www-form-urlencoded" }, body });
      this.session = null; this.sessionStart = null; this.stats = { dials: 0, connects: 0, meetings: 0 }; this.save(); this.renderStrip();
      toast("Session saved to My grafting");
    },
    logged(outcome) {
      this.stats.dials += 1;
      if (["connected", "meeting_booked", "not_interested"].includes(outcome)) this.stats.connects += 1;
      if (outcome === "meeting_booked") this.stats.meetings += 1;
      this.save(); this.renderStrip();
    },
    save() { store.set("lf-dial-session", JSON.stringify(this.session ? { id: this.session, start: this.sessionStart, stats: this.stats } : null)); },
    renderStrip() {
      const s = this.stats;
      const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
      set("ss-dials", s.dials); set("ss-connects", s.connects); set("ss-meetings", s.meetings);
      set("ss-time", this.sessionStart ? this.fmt(Math.round((Date.now() - this.sessionStart) / 1000)) : "00:00");
      const end = $("#ss-end"); if (end) end.classList.toggle("hidden", !this.session);
    },
    choose(outcome) {
      const form = $("#outcome-form");
      if (!form) return;
      form.querySelector('input[name="outcome"]').value = outcome;
      $$(".outcomes .btn").forEach((b) => b.classList.toggle("sel", b.dataset.outcome === outcome));
      const needsTags = (form.dataset.objOutcomes || "").split(",").includes(outcome);
      $("#objections-box").classList.toggle("hidden", !needsTags);
      $("#callback-box").classList.toggle("hidden", outcome !== "callback");
      $("#meeting-box").classList.toggle("hidden", outcome !== "meeting_booked");
      const save = $("#save-outcome"); save.disabled = false;
      save.textContent = "Save: " + form.querySelector('[data-outcome="' + outcome + '"]').dataset.label;
      if (!needsTags && outcome !== "callback" && outcome !== "meeting_booked") { htmx.trigger(form, "submit"); }
    },
    key(e) {
      if (e.target.closest("input,textarea,select") || e.metaKey || e.ctrlKey || e.altKey) return;
      const btn = $('.outcomes .btn[data-key="' + e.key + '"]');
      if (btn) { e.preventDefault(); this.choose(btn.dataset.outcome); return; }
      if (e.key === "n" || e.key === "N") { const nx = $("#next-btn"); if (nx) { e.preventDefault(); nx.click(); } }
      if (e.key === "c" || e.key === "C") { e.preventDefault(); this.startCall(); }
    },
  };
  window.lfDial = Dial;
  document.addEventListener("DOMContentLoaded", () => Dial.init());

  /* ---------- copy buttons ---------- */
  window.lfCopy = function (text) {
    (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(
      () => toast("Copied"), () => { window.prompt("Copy this:", text); });
  };
})();

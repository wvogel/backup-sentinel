// ── Settings page interactions ───────────────────────────────────────────────

// ── Settings: Test Gotify / E-Mail ──────────────────────────────────────────
document.addEventListener("click", (event) => {
  const gotifyBtn = event.target.closest(".js-test-gotify");
  if (gotifyBtn) {
    const result = document.querySelector(".js-test-gotify-result");
    if (result) result.textContent = T('js.sending');
    fetch("/settings/notifications/test-gotify", { method: "POST" })
      .then(async (r) => ({ ok: r.ok, body: await r.json().catch(() => ({ ok: false, detail: T('js.unknown_response') })) }))
      .then(({ ok, body }) => {
        if (!result) return;
        result.textContent = body.detail || (body.ok ? T('js.success') : T('js.failed'));
        result.classList.toggle("ok-text", ok && body.ok);
      })
      .catch(() => {
        if (!result) return;
        result.textContent = T('js.gotify_test_error');
        result.classList.remove("ok-text");
      });
    return;
  }

  const emailBtn = event.target.closest(".js-test-email");
  if (emailBtn) {
    const result = document.querySelector(".js-test-email-result");
    if (result) result.textContent = T('js.sending');
    fetch("/settings/notifications/test-email", { method: "POST" })
      .then(async (r) => ({ ok: r.ok, body: await r.json().catch(() => ({ ok: false, detail: T('js.unknown_response') })) }))
      .then(({ ok, body }) => {
        if (!result) return;
        result.textContent = body.detail || (body.ok ? T('js.success') : T('js.failed'));
        result.classList.toggle("ok-text", ok && body.ok);
      })
      .catch(() => {
        if (!result) return;
        result.textContent = T('js.email_test_error');
        result.classList.remove("ok-text");
      });
  }
});

// ── Sticky VM headers: measure cluster summary height ────────────────────────
(function () {
  const topbar = document.querySelector(".topbar");
  document.querySelectorAll(".report-cluster-details").forEach((details) => {
    const clusterSummary = details.querySelector(":scope > summary");
    if (!clusterSummary) return;
    const update = () => {
      const h = clusterSummary.getBoundingClientRect().height;
      const topbarH = topbar ? topbar.getBoundingClientRect().height : 0;
      details.style.setProperty("--vm-sticky-top", (topbarH + h) + "px");
    };
    update();
    window.addEventListener("resize", update);
  });
})();

// ── Appearance: colour pickers ↔ hex fields, preset switch, live preview ─────
(function () {
  const form = document.querySelector(".js-theme-form");
  if (!form) return;
  const presets = JSON.parse(form.dataset.presets || "{}");
  const preview = form.querySelector(".js-theme-preview");
  const hexOf = (value) => {
    const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec((value || "").trim());
    if (!m) return null;
    const d = m[1].length === 3 ? m[1].replace(/./g, (c) => c + c) : m[1];
    return "#" + d.toLowerCase();
  };
  const luminance = (hex) => {
    const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
      .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  };
  const contrast = (a, b) => {
    const [x, y] = [luminance(a), luminance(b)].sort((m, n) => n - m);
    return (x + 0.05) / (y + 0.05);
  };
  // Same rule as the server: first readable ink, else the better of black/white.
  const bestInk = (surface, ...candidates) =>
    candidates.find((ink) => contrast(ink, surface) >= 4.5) ||
    (contrast("#000000", surface) >= contrast("#ffffff", surface) ? "#000000" : "#ffffff");
  const value = (key) => hexOf(form.querySelector(`.js-theme-hex[data-key="${key}"]`).value);

  function updatePreview() {
    const [primary, accent, background, running] = ["primary", "accent", "background", "running"].map(value);
    if (!primary || !accent || !background || !running) return;
    preview.style.setProperty("--p-primary", primary);
    preview.style.setProperty("--p-accent", accent);
    preview.style.setProperty("--p-accent-ink", bestInk(accent, primary));
    preview.style.setProperty("--p-header-ink", bestInk(primary, "#f6f7ff"));
    preview.style.setProperty("--p-bg", background);
    preview.style.setProperty("--p-running", running);
  }

  function setColor(key, hex) {
    form.querySelector(`.js-theme-hex[data-key="${key}"]`).value = hex;
    form.querySelector(`.js-theme-picker[data-key="${key}"]`).value = hex;
  }

  form.addEventListener("input", (event) => {
    const el = event.target;
    if (el.classList.contains("js-theme-picker")) {
      form.querySelector(`.js-theme-hex[data-key="${el.dataset.key}"]`).value = el.value;
    } else if (el.classList.contains("js-theme-hex")) {
      const hex = hexOf(el.value);
      el.classList.toggle("is-invalid", !hex);
      if (hex) form.querySelector(`.js-theme-picker[data-key="${el.dataset.key}"]`).value = hex;
    }
    updatePreview();
  });
  form.querySelector(".js-theme-preset").addEventListener("change", (event) => {
    const base = presets[event.target.value];
    if (!base) return;
    for (const key of Object.keys(base)) setColor(key, base[key]);
    updatePreview();
  });
})();

// ── Settings: tabs ───────────────────────────────────────────────────────────
// The active tab is the URL hash (#cluster, #benachrichtigungen, …). A hash that
// names any element inside a panel opens that panel, so existing links and the
// redirects after saving keep working. Without JS all panels stay visible.
(function () {
  const bar = document.querySelector(".settings-tabs");
  if (!bar) return;
  const tabs = [...bar.querySelectorAll('[role="tab"]')];
  const panels = tabs.map((tab) => document.getElementById(tab.getAttribute("aria-controls")));

  function activate(key, { focus = false } = {}) {
    tabs.forEach((tab, i) => {
      const on = tab.dataset.tab === key;
      tab.classList.toggle("is-active", on);
      tab.setAttribute("aria-selected", on ? "true" : "false");
      tab.tabIndex = on ? 0 : -1;
      panels[i].hidden = !on;
      if (on && focus) tab.focus();
    });
  }

  function fromHash() {
    const target = location.hash.length > 1 && document.getElementById(decodeURIComponent(location.hash.slice(1)));
    const panel = target && target.closest('[role="tabpanel"]');
    const tab = panel && tabs.find((t) => t.getAttribute("aria-controls") === panel.id);
    activate(tab ? tab.dataset.tab : tabs[0].dataset.tab);
  }

  bar.addEventListener("click", (event) => {
    const tab = event.target.closest('[role="tab"]');
    if (!tab) return;
    activate(tab.dataset.tab);
    history.replaceState(null, "", "#" + tab.dataset.tab);
  });
  // Arrow keys move between tabs (WAI-ARIA tabs pattern).
  bar.addEventListener("keydown", (event) => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
    if (!step) return;
    const current = tabs.findIndex((t) => t.classList.contains("is-active"));
    const next = tabs[(current + step + tabs.length) % tabs.length];
    activate(next.dataset.tab, { focus: true });
    history.replaceState(null, "", "#" + next.dataset.tab);
    event.preventDefault();
  });
  window.addEventListener("hashchange", fromHash);
  fromHash();
  // The browser scrolls to the hash's section once the page has loaded; the tab
  // bar belongs in view instead.
  if (location.hash) {
    window.scrollTo(0, 0);
    window.addEventListener("load", () => requestAnimationFrame(() => window.scrollTo(0, 0)), { once: true });
  }
})();

// ── Result of saving the restore server (rendered by the server as data) ─────
document.querySelectorAll(".js-save-status").forEach((el) => {
  showStatusLightbox(el.dataset.ok === "1", el.dataset.title, el.dataset.msg);
});

// ── Storage performance tests of the restore server ─────────────────────────
(function () {
  function post(url, body) {
    return fetch(url, { method: "POST", headers: body ? { "Content-Type": "application/x-www-form-urlencoded" } : {}, body: body || undefined });
  }
  // Byte-rate in the app's style: decimal GB/s · MB/s, de-DE, no trailing ,0.
  function fmtRate(bps) {
    if (!bps || bps <= 0) return "–";
    var u = ["B/s", "kB/s", "MB/s", "GB/s", "TB/s"], i = 0, v = bps;
    while (v >= 1000 && i < u.length - 1) { v /= 1000; i++; }
    var dec = v >= 100 || i === 0 ? 0 : 1;
    return v.toLocaleString("de-DE", { minimumFractionDigits: 0, maximumFractionDigits: dec }) + " " + u[i];
  }
  // A rate series as an SVG polyline over the 100×24 viewBox. The y-axis
  // starts at 0 — the curve stands on its zero line (y = 23.5), the highest
  // rate reaches the top; never scaled between the lowest and highest sample,
  // which turns a ripple into a cliff.
  var SPARK_TOP = 1.5, SPARK_ZERO = 23.5;
  function sparkY(v, max) {
    return SPARK_ZERO - (max > 0 ? Math.max(0, v) / max : 0) * (SPARK_ZERO - SPARK_TOP);
  }
  function sparkPoints(series) {
    if (!series || series.length < 2) return [];
    var max = Math.max.apply(null, series), n = series.length;
    return series.map(function (v, i) {
      return ((i / (n - 1)) * 100).toFixed(1) + "," + sparkY(v, max).toFixed(1);
    });
  }
  // The curve in two stretches: the warm-up (the first warmN samples, dashed)
  // and what is measured (solid, with the area under it). They share the point
  // where one ends and the other begins, so the line has no gap.
  function sparkParts(series, warmN) {
    var pts = sparkPoints(series), n = pts.length;
    var cut = Math.max(0, Math.min(n, warmN || 0));
    var warm = cut > 0 ? pts.slice(0, Math.min(n, cut + 1)) : [];
    var main = cut < n ? pts.slice(cut) : [];
    var area = "";
    if (main.length > 1) {
      area = main[0].split(",")[0] + "," + SPARK_ZERO + " " + main.join(" ") + " " + main[main.length - 1].split(",")[0] + "," + SPARK_ZERO;
    }
    return { warm: warm.length > 1 ? warm.join(" ") : "", main: main.length > 1 ? main.join(" ") : "", area: area };
  }
  function meanRate(series) {
    if (!series || !series.length) return 0;
    return series.reduce(function (sum, v) { return sum + v; }, 0) / series.length;
  }
  // The value under the pointer: a line and a dot on the curve, the rate above.
  function initSparkHover(box) {
    var wrap = box.querySelector(".js-perf-spark-wrap");
    if (!wrap) return;
    var cursor = wrap.querySelector(".js-perf-cursor"), dot = wrap.querySelector(".js-perf-dot");
    var tip = wrap.querySelector(".js-perf-tip");
    function hide() { cursor.hidden = dot.hidden = tip.hidden = true; }
    wrap.addEventListener("mousemove", function (e) {
      var series = box._series;
      if (!series || series.length < 2) { hide(); return; }
      var r = wrap.getBoundingClientRect(), n = series.length;
      var i = Math.max(0, Math.min(n - 1, Math.round(((e.clientX - r.left) / r.width) * (n - 1))));
      var x = (i / (n - 1)) * 100;
      var y = (sparkY(series[i], Math.max.apply(null, series)) / 24) * 100;
      cursor.style.left = dot.style.left = x + "%";
      dot.style.top = y + "%";
      tip.textContent = fmtRate(series[i]) + (i < (box._warmupN || 0) ? " \u00b7 " + T("restore.perf_warmup") : "");
      // Keep the label inside the graph at both ends.
      tip.style.left = Math.max(12, Math.min(88, x)) + "%";
      cursor.hidden = dot.hidden = tip.hidden = false;
    });
    wrap.addEventListener("mouseleave", hide);
  }
  function renderProgress(box, p) {
    var fill = box.querySelector(".perf-bar-fill");
    var text = box.querySelector(".js-perf-text");
    var pct = Math.max(0, Math.min(100, p.pct || 0));
    box._series = p.series || [];
    // The samples of the warm-up stand at the start of the series: dashed in
    // the curve, named under the pointer, and not part of the average.
    var n = box._series.length;
    var warmN = (box._warmupN = Math.max(0, Math.min(n, p.warmup_n || 0)));
    if (fill) fill.style.width = pct.toFixed(1) + "%";
    if (text) {
      var bits = [Math.round(pct) + " %"];
      if (p.warming) bits.unshift(T("restore.perf_warmup"));
      if (p.rate_bps) bits.push(fmtRate(p.rate_bps));
      if (p.eta) bits.push("eta " + p.eta);
      text.textContent = bits.join(" · ");
    }
    // The performance bar of this storage shows the average of the rates
    // measured so far (the warm-up left out) — where the result is heading —
    // and moves with every new sample.
    var row = box.closest("tr");
    var meter = row && row.querySelector(".perf-meter");
    var bar = meter && meter.querySelector(".store-perf-bar > span");
    var scale = meter ? parseFloat(meter.dataset.scale) : 0;
    var avg = meanRate(box._series.slice(warmN));  // nothing measured yet: the bar stays where it is
    if (bar && scale > 0 && avg) {
      bar.style.width = Math.max(0, Math.min(100, (avg / scale) * 100)).toFixed(1) + "%";
    }
    var parts = sparkParts(box._series, warmN);
    var set = function (sel, points) { var el = box.querySelector(sel); if (el) el.setAttribute("points", points); };
    set(".js-perf-warm", parts.warm);
    set(".js-perf-line", parts.main);
    set(".js-perf-spark .perf-spark-area", parts.area);
  }
  // One poller per running test: updates the live box, reloads when it ends.
  function trackRun(box, storage) {
    var tick = function () {
      fetch("/settings/restore-server/benchmark-status").then(function (x) { return x.json(); }).then(function (s) {
        if (s.running !== storage) { clearInterval(timer); location.reload(); return; }
        if (s.progress && s.progress.storage === storage) renderProgress(box, s.progress);
      }).catch(function () {});
    };
    var timer = setInterval(tick, 5000);
    tick();
  }
  document.querySelectorAll(".js-perf-run").forEach(function (box) {
    initSparkHover(box);
    trackRun(box, box.dataset.storage);
  });
  document.querySelectorAll(".js-perf-cancel").forEach(function (btn) {
    btn.addEventListener("click", function () {
      btn.disabled = true;
      btn.textContent = T("restore.perf_cancelling");
      post("/settings/restore-server/benchmark-cancel", "storage=" + encodeURIComponent(btn.dataset.storage))
        .catch(function () {}); // the poller picks up the end and reloads
    });
  });
  document.querySelectorAll(".js-perf-test").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var storage = btn.dataset.storage;
      btn.disabled = true;
      post("/settings/restore-server/benchmark", "storage=" + encodeURIComponent(storage)).then(function (r) {
        if (!r.ok) { r.json().then(function (j) { alert(j.error || "Fehler"); }); btn.disabled = false; return; }
        location.reload(); // server renders the live progress UI; its poller takes over
      });
    });
  });
  var inst = document.querySelector(".js-install-fio");
  if (inst) inst.addEventListener("click", function (e) {
    e.preventDefault();
    inst.textContent = "…";
    post("/settings/restore-server/install-fio").then(function (r) {
      if (r.ok) { location.reload(); } else { r.json().then(function (j) { alert(j.error || "Fehler"); }); }
    });
  });
})();

// ── Notification channels: switch on/off, saved at once ──────────────────────
document.querySelectorAll('.js-fieldset-toggle').forEach(function(cb) {
  cb.addEventListener('change', function() {
    var fieldset = this.closest('.toggleable-fieldset');
    if (this.checked) {
      fieldset.classList.remove('is-disabled');
    } else {
      fieldset.classList.add('is-disabled');
    }
    // persist via AJAX
    fetch('/settings/notifications/toggle', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key: this.name, value: this.checked})
    });
  });
});

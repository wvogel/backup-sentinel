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

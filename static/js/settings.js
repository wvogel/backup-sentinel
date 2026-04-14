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
  const TOPBAR_H = 52;
  document.querySelectorAll(".report-cluster-details").forEach((details) => {
    const clusterSummary = details.querySelector(":scope > summary");
    if (!clusterSummary) return;
    const update = () => {
      const h = clusterSummary.getBoundingClientRect().height;
      details.style.setProperty("--vm-sticky-top", (TOPBAR_H + h) + "px");
    };
    update();
    window.addEventListener("resize", update);
  });
})();

// ── Shared utilities ──────────────────────────────────────────────────────────

// ── Animations follow the clock ───────────────────────────────────────────────
// Every endless animation stands where the wall clock says (time since the
// epoch modulo its period), not where its element happened to be created:
// a redraw, a rebuilt tile or a reload continues it instead of restarting it.
function syncAnimations() {
  if (!document.getAnimations) return;
  const now = Date.now();
  for (const anim of document.getAnimations()) {
    if (anim.playState !== "running" || !anim.effect) continue;
    const timing = anim.effect.getComputedTiming();
    if (timing.iterations !== Infinity || !(timing.duration > 0)) continue;
    const period = timing.duration * (String(timing.direction).startsWith("alternate") ? 2 : 1);
    anim.currentTime = (now % period) + timing.delay;
  }
}
document.addEventListener("DOMContentLoaded", syncAnimations);
// Anything started elsewhere (a class switched on later) falls in step too.
document.addEventListener("animationstart", syncAnimations);

// ── Status Lightbox ─────────────────────────────────────────────────────────
// Briefly flashes a result over the page: a green ✓ when ok, a red ✗ otherwise.
function showStatusLightbox(ok, title, msg, durationMs = 2600) {
  const overlay = document.getElementById("success-lightbox");
  if (!overlay) return;
  const icon = document.getElementById("success-lightbox-icon");
  const box = overlay.querySelector(".lightbox-box");
  if (icon) icon.innerHTML = ok ? "&#x2713;" : "&#x2717;";
  if (box) box.classList.toggle("lightbox-failure", !ok);
  document.getElementById("success-lightbox-title").textContent = title;
  document.getElementById("success-lightbox-msg").textContent = msg || "";
  overlay.classList.remove("hidden");
  clearTimeout(overlay._hideTimer);
  overlay._hideTimer = setTimeout(() => overlay.classList.add("hidden"), durationMs);
}

function showSuccessLightbox(title, msg, durationMs = 2000) {
  showStatusLightbox(true, title, msg, durationMs);
}

async function copyText(targetId, button) {
  const target = document.getElementById(targetId);
  if (!target) return;

  const text = target.textContent || "";
  try {
    await navigator.clipboard.writeText(text);
    const label = button.querySelector("span:last-child");
    if (!label) return;
    const previous = label.textContent;
    label.textContent = T('js.copied');
    window.setTimeout(() => { label.textContent = previous; }, 1500);
  } catch {
    window.alert(T('js.copy_failed'));
  }
}

// ── Topbar height → --topbar-h (sticky elements sit right below it) ─────────
(function () {
  const topbar = document.querySelector(".topbar");
  if (!topbar) return;
  const update = () =>
    document.documentElement.style.setProperty("--topbar-h", topbar.getBoundingClientRect().height + "px");
  update();
  window.addEventListener("resize", update);
})();

// ── Drag-to-reorder table rows (tbody[data-reorder]) ──────────────────────────
// Rows carry hidden inputs; the form submits them in DOM order, so reordering
// the rows reorders the saved sequence. No library — plain HTML5 drag events.
(function () {
  function initReorder(tbody) {
    let dragged = null;
    tbody.addEventListener("dragstart", (e) => {
      const row = e.target.closest("tr[draggable='true']");
      if (!row) return;
      dragged = row;
      row.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
    });
    tbody.addEventListener("dragend", () => {
      if (dragged) dragged.classList.remove("dragging");
      dragged = null;
      if (tbody.hasAttribute("data-autosave") && typeof saveTargetStorages === "function") saveTargetStorages(tbody);
    });
    tbody.addEventListener("dragover", (e) => {
      if (!dragged) return;
      const row = e.target.closest("tr[draggable='true']");
      e.preventDefault();
      if (!row || row === dragged) return;
      const rect = row.getBoundingClientRect();
      const after = e.clientY - rect.top > rect.height / 2;
      tbody.insertBefore(dragged, after ? row.nextSibling : row);
    });
  }
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("tbody[data-reorder]").forEach(initReorder);
  });
})();

// ── Generic confirm modal ─────────────────────────────────────────────────────
// Returns a Promise<{ok, suppress}>. `suppressLabel`, when given, adds a
// "don't warn again today" checkbox.
function confirmModal(opts) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "lightbox-overlay confirm-overlay";
    const box = document.createElement("div");
    box.className = "lightbox-box confirm-box";
    const h = document.createElement("h3");
    h.textContent = opts.title || "";
    const p = document.createElement("p");
    p.textContent = opts.body || "";
    box.append(h, p);
    let suppressCb = null;
    if (opts.suppressLabel) {
      const lbl = document.createElement("label");
      lbl.className = "checkbox-label confirm-suppress";
      suppressCb = document.createElement("input");
      suppressCb.type = "checkbox";
      lbl.append(suppressCb, document.createTextNode(" " + opts.suppressLabel));
      box.append(lbl);
    }
    const actions = document.createElement("div");
    actions.className = "lightbox-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "button-secondary";
    cancel.textContent = opts.cancelLabel || "Cancel";
    const ok = document.createElement("button");
    ok.type = "button";
    ok.className = "button-primary";
    ok.textContent = opts.confirmLabel || "OK";
    actions.append(cancel, ok);
    box.append(actions);
    overlay.append(box);
    document.body.append(overlay);
    const done = (result) => {
      overlay.remove();
      resolve(result);
    };
    cancel.addEventListener("click", () => done({ ok: false }));
    ok.addEventListener("click", () => done({ ok: true, suppress: !!(suppressCb && suppressCb.checked) }));
    overlay.addEventListener("click", (ev) => {
      if (ev.target === overlay) done({ ok: false });
    });
  });
}

// ── Two-step confirm on submit buttons (.js-confirm-submit) ────────────────────
// First click arms (shows data-confirm-text); a second click within 4s submits.
document.addEventListener(
  "click",
  (e) => {
    const btn = e.target.closest(".js-confirm-submit");
    if (!btn) return;
    if (btn.dataset.armed === "1") return; // armed → let the submit through
    e.preventDefault();
    const label = btn.querySelector(".js-confirm-label") || btn;
    if (!btn.dataset.orig) btn.dataset.orig = label.textContent;
    label.textContent = btn.dataset.confirmText || T("js.confirm");
    btn.classList.add("is-armed");
    btn.dataset.armed = "1";
    clearTimeout(btn._armTimer);
    btn._armTimer = setTimeout(() => {
      btn.dataset.armed = "";
      btn.classList.remove("is-armed");
      label.textContent = btn.dataset.orig;
    }, 4000);
  },
  true,
);

// ── Hold-on-failure: warn (debug only) when enabling, unless suppressed today ──
document.addEventListener("change", async (e) => {
  const cb = e.target;
  if (!(cb instanceof HTMLInputElement) || !cb.hasAttribute("data-confirm-hold") || !cb.checked) return;
  const today = new Date().toISOString().slice(0, 10);
  let suppressed = null;
  try {
    suppressed = localStorage.getItem("holdWarnSuppressed");
  } catch {}
  if (suppressed === today) return;
  cb.checked = false; // hold until confirmed
  const res = await confirmModal({
    title: T("restore.hold_confirm_title"),
    body: T("restore.hold_confirm_body"),
    confirmLabel: T("common.confirm"),
    cancelLabel: T("common.cancel"),
    suppressLabel: T("restore.hold_confirm_suppress"),
  });
  if (res.ok) {
    cb.checked = true;
    if (res.suppress) {
      try {
        localStorage.setItem("holdWarnSuppressed", today);
      } catch {}
    }
  }
});

// ── Live disk-class icon in the restore storage table ─────────────────────────
// Changing the disk-class dropdown swaps the icon (shape + colour) immediately.
(function () {
  const WRAP = (inner) =>
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    inner +
    "</svg>";
  const SSD = WRAP('<rect x="4" y="6" width="16" height="12" rx="2"/><line x1="8" y1="10" x2="8" y2="14"/><line x1="12" y1="10" x2="12" y2="14"/><line x1="16" y1="10" x2="16" y2="14"/>');
  const HDD = WRAP('<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>');
  const META = {
    "enterprise-ssd": ["disk-green", SSD],
    "standard-ssd": ["disk-blue", SSD],
    "sas-hdd": ["disk-green", HDD],
    "sata-hdd": ["disk-blue", HDD],
  };
  document.addEventListener("change", (e) => {
    const sel = e.target.closest(".js-disk-class");
    if (!sel) return;
    const icon = sel.closest("tr")?.querySelector(".disk-icon");
    const meta = META[sel.value];
    if (!icon || !meta) return;
    icon.className = "disk-icon " + meta[0];
    icon.innerHTML = meta[1];
    icon.setAttribute("data-tooltip", T("restore.class_" + sel.value));
  });
})();

// ── Auto-save the restore target-storage table (tbody[data-autosave]) ─────────
// Toggling a target, changing a disk class, or reordering saves immediately via
// fetch — no page reload, no bottom "Speichern".
function saveTargetStorages(tbody) {
  const url = tbody.getAttribute("data-autosave");
  if (!url) return;
  const fd = new FormData();
  tbody.querySelectorAll("tr").forEach((tr) => {
    const name = tr.querySelector("input[name='ts_name']")?.value;
    if (!name) return;
    fd.append("ts_name", name);
    fd.append("ts_class", tr.querySelector("select[name='ts_class']")?.value || "standard-ssd");
    if (tr.querySelector("input[name='ts_use']")?.checked) fd.append("ts_use", name);
  });
  fetch(url, { method: "POST", body: fd })
    .then((r) => {
      if (r.ok) showStatusLightbox(true, T("restore.saved"), "", 1100);
    })
    .catch(() => {});
}
document.addEventListener("change", (e) => {
  const tbody = e.target.closest("tbody[data-autosave]");
  if (tbody && (e.target.name === "ts_use" || e.target.name === "ts_class")) saveTargetStorages(tbody);
});

// Expose globally for other modules
window.showSuccessLightbox = showSuccessLightbox;
window.showStatusLightbox = showStatusLightbox;
window.confirmModal = confirmModal;
window.copyText = copyText;

// ── Instant tooltips for [data-tooltip] (replaces the native title delay) ──
(function () {
  const tip = document.createElement("div");
  tip.className = "tooltip-float";
  tip.hidden = true;
  tip.setAttribute("role", "tooltip");
  let current = null;

  function show(el) {
    const text = el.dataset.tooltip;
    if (!text) return;
    current = el;
    tip.textContent = text;
    tip.hidden = false;
    if (!tip.isConnected) document.body.append(tip);
    const r = el.getBoundingClientRect();
    const w = tip.offsetWidth;
    const h = tip.offsetHeight;
    const gap = 6;
    const left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
    const above = r.top - h - gap;
    tip.style.left = left + "px";
    tip.style.top = (above >= 8 ? above : r.bottom + gap) + "px";
  }

  function hide() {
    current = null;
    tip.hidden = true;
  }

  document.addEventListener("mouseover", (event) => {
    const el = event.target.closest?.("[data-tooltip]");
    if (el === current) return;
    if (el) show(el);
    else hide();
  });
  document.addEventListener("focusin", (event) => {
    const el = event.target.closest?.("[data-tooltip]");
    if (el) show(el);
  });
  document.addEventListener("focusout", hide);
  window.addEventListener("scroll", hide, { passive: true, capture: true });
})();

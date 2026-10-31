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

// Expose globally for other modules
window.showSuccessLightbox = showSuccessLightbox;
window.showStatusLightbox = showStatusLightbox;
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

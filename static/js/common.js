// ── Shared utilities ──────────────────────────────────────────────────────────

// ── Success Lightbox ──────────────────────────────────────────────────────────
function showSuccessLightbox(title, msg, durationMs = 2000) {
  const overlay = document.getElementById("success-lightbox");
  if (!overlay) return;
  document.getElementById("success-lightbox-title").textContent = title;
  document.getElementById("success-lightbox-msg").textContent = msg;
  overlay.classList.remove("hidden");
  setTimeout(() => {
    overlay.classList.add("hidden");
  }, durationMs);
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

// Expose globally for other modules
window.showSuccessLightbox = showSuccessLightbox;
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

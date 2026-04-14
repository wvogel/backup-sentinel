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

// Expose globally for other modules
window.showSuccessLightbox = showSuccessLightbox;
window.copyText = copyText;

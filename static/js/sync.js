// ── Sync-related interactions ────────────────────────────────────────────────

// ── Klick auf alles mit data-log → Log-Panel anzeigen
//    (Sync-Dots auf der Settings-Seite, Restore-Test-Logs etc.)
document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-log]");
  if (!trigger) return;
  event.preventDefault();
  const panel = document.getElementById("sync-log-panel");
  const content = document.getElementById("sync-log-content");
  const title = document.getElementById("sync-log-title");
  if (!panel || !content) return;
  if (title) title.textContent = trigger.dataset.logTitle || T('js.sync_log');
  content.textContent = trigger.dataset.log;
  panel.classList.remove("hidden");
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
});

const syncLogClose = document.getElementById("sync-log-close");
if (syncLogClose) {
  syncLogClose.addEventListener("click", () => {
    document.getElementById("sync-log-panel").classList.add("hidden");
  });
}

// ── Async Sync mit Spinner + Live-Log ───────────────────────────────────────
for (const form of document.querySelectorAll(".js-sync-form")) {
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const slug = form.dataset.clusterSlug;
    const btn = form.querySelector(".js-sync-btn");
    if (!btn || btn.classList.contains("syncing")) return;

    btn.classList.add("syncing");
    btn.title = T('js.syncing');

    const panel = document.getElementById("sync-log-panel");
    const logContent = document.getElementById("sync-log-content");
    const logStatus = document.getElementById("sync-log-status");

    // Init live log buffer
    window._liveSyncLog = "";
    window._liveSyncOffset = 0;
    window._liveSyncActive = true;

    fetch(form.action, { method: "POST", body: new FormData(form) })
      .catch(() => {});

    // Poll sync status + buffer log (panel updates only if visible)
    const poll = setInterval(async () => {
      try {
        const resp = await fetch(
          `/api/clusters/${encodeURIComponent(slug)}/sync-status?since=${window._liveSyncOffset}`
        );
        if (!resp.ok) return;
        const data = await resp.json();

        if (data.log && data.log.length > 0) {
          window._liveSyncLog += data.log.join("\n") + "\n";
          // Update panel if it's open
          if (panel && logContent && !panel.classList.contains("hidden") && window._liveSyncActive) {
            logContent.textContent = window._liveSyncLog;
            logContent.scrollTop = logContent.scrollHeight;
          }
        }
        window._liveSyncOffset = data.offset || window._liveSyncOffset;

        if (!data.syncing) {
          clearInterval(poll);
          window._liveSyncActive = false;
          btn.classList.remove("syncing");
          btn.title = T('cluster.sync_now');
          if (logStatus && panel && !panel.classList.contains("hidden")) {
            logStatus.textContent = "✓ " + T('js.sync_complete');
            logStatus.classList.remove("muted");
            logStatus.classList.add("ok-text");
          }
          window.showSuccessLightbox(T('js.sync_complete'), T('js.data_updating'));
          setTimeout(() => location.reload(), 2500);
        }
      } catch { /* ignore */ }
    }, 2000);
  });
}

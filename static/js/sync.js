// ── Sync-related interactions ────────────────────────────────────────────────

// ── Log dialog: anything with data-log opens it (sync symbols, restore-test
//    notes). While a sync runs, a sync symbol shows its live log.
const logDialog = document.getElementById("sync-log-panel");

function openLog(trigger) {
  const content = document.getElementById("sync-log-content");
  const title = document.getElementById("sync-log-title");
  const status = document.getElementById("sync-log-status");
  const live = window._liveSyncActive && trigger.classList.contains("sync-icon-btn");
  content.textContent = live ? window._liveSyncLog || "" : trigger.dataset.log || T("common.no_log");
  title.textContent = live ? T("js.live_sync_log") : trigger.dataset.logTitle || T("js.sync_log");
  status.textContent = live ? T("js.sync_running") : "";
  status.className = "muted";
  if (!logDialog.open) logDialog.showModal();
  content.scrollTop = content.scrollHeight;
}

async function copyLog(button) {
  const text = document.getElementById("sync-log-content").textContent;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // No clipboard API (plain http): copy the selected text instead.
    const range = document.createRange();
    range.selectNodeContents(document.getElementById("sync-log-content"));
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    document.execCommand("copy");
    selection.removeAllRanges();
  }
  button.classList.add("is-copied");
  button.setAttribute("aria-label", T("js.copied"));
  button.title = T("js.copied");
  clearTimeout(button._copiedTimer);
  button._copiedTimer = setTimeout(() => {
    button.classList.remove("is-copied");
    button.setAttribute("aria-label", T("common.copy"));
    button.title = T("common.copy");
  }, 1500);
}

if (logDialog) {
  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-log]");
    if (!trigger) return;
    event.preventDefault();
    openLog(trigger);
  });
  document.getElementById("sync-log-close").addEventListener("click", () => logDialog.close());
  document.getElementById("sync-log-copy").addEventListener("click", (event) => copyLog(event.currentTarget));
  // A click on the backdrop (outside the box) closes it; Esc does natively.
  logDialog.addEventListener("click", (event) => {
    if (event.target === logDialog) logDialog.close();
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
          if (panel && logContent && panel.open && window._liveSyncActive) {
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
          if (logStatus && panel && panel.open) {
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

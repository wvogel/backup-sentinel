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

// ── Sync with spinner + live log ────────────────────────────────────────────
// While a sync runs, its log is buffered in window._liveSync* (the log dialog
// shows it for sync symbols); when it ends, a lightbox shows and the page reloads.
function syncStatusUrl(slug, since) {
  return `/api/clusters/${encodeURIComponent(slug)}/sync-status?since=${since}`;
}

function setSyncing(btn, syncing) {
  if (!btn) return;
  btn.classList.toggle("syncing", syncing);
  btn.title = T(syncing ? "js.syncing" : "cluster.sync_now");
}

function appendLiveLog(lines) {
  if (!lines || lines.length === 0) return;
  window._liveSyncLog += lines.join("\n") + "\n";
  const logContent = document.getElementById("sync-log-content");
  if (logDialog && logContent && logDialog.open && window._liveSyncActive) {
    logContent.textContent = window._liveSyncLog;
    logContent.scrollTop = logContent.scrollHeight;
  }
}

function finishSync(btn) {
  window._liveSyncActive = false;
  setSyncing(btn, false);
  const logStatus = document.getElementById("sync-log-status");
  if (logStatus && logDialog && logDialog.open) {
    logStatus.textContent = "✓ " + T("js.sync_complete");
    logStatus.classList.remove("muted");
    logStatus.classList.add("ok-text");
  }
  window.showSuccessLightbox(T("js.sync_complete"), T("js.data_updating"));
  setTimeout(() => location.reload(), 2500);
}

// Follow a running sync of `slug` until it ends. `initial` is a sync-status
// answer already fetched (its log seeds the buffer).
function watchSync(slug, btn, initial) {
  setSyncing(btn, true);
  window._liveSyncLog = "";
  window._liveSyncOffset = 0;
  window._liveSyncActive = true;
  if (initial) {
    appendLiveLog(initial.log);
    window._liveSyncOffset = initial.offset || 0;
  }
  const poll = setInterval(async () => {
    try {
      const resp = await fetch(syncStatusUrl(slug, window._liveSyncOffset));
      if (!resp.ok) return;
      const data = await resp.json();
      appendLiveLog(data.log);
      window._liveSyncOffset = data.offset || window._liveSyncOffset;
      if (!data.syncing) {
        clearInterval(poll);
        finishSync(btn);
      }
    } catch { /* ignore */ }
  }, 2000);
}

for (const form of document.querySelectorAll(".js-sync-form")) {
  const slug = form.dataset.clusterSlug;
  const btn = form.querySelector(".js-sync-btn");
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!btn || btn.classList.contains("syncing")) return;
    fetch(form.action, { method: "POST", body: new FormData(form) }).catch(() => {});
    watchSync(slug, btn);
  });
  // data-sync-watch: also follow a sync that already runs when the page loads
  // (auto-sync or one started elsewhere).
  if ("syncWatch" in form.dataset) {
    fetch(syncStatusUrl(slug, 0))
      .then((resp) => (resp.ok ? resp.json() : null))
      .then((data) => {
        if (data && data.syncing && !(btn && btn.classList.contains("syncing"))) watchSync(slug, btn, data);
      })
      .catch(() => {});
  }
}

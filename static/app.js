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

// ── Bootstrap polling ─────────────────────────────────────────────────────────
function pollBootstrapStatus(clusterSlug, check, statusEl, successTitle, successMsg) {
  if (!statusEl) return;
  let attempts = 0;
  const maxAttempts = 120; // 120 x 3s = 6 min
  const interval = setInterval(async () => {
    attempts++;
    if (attempts > maxAttempts) {
      clearInterval(interval);
      statusEl.textContent = T('js.bootstrap_timeout');
      return;
    }
    try {
      const resp = await fetch(`/api/clusters/${encodeURIComponent(clusterSlug)}/bootstrap-status`);
      if (!resp.ok) return;
      const data = await resp.json();
      if (check(data)) {
        clearInterval(interval);
        statusEl.textContent = T('js.registered_success');
        statusEl.classList.remove("muted");
        statusEl.classList.add("ok-text");
        showSuccessLightbox(
          successTitle || T('js.registered_success'),
          successMsg || T('js.connection_established'),
        );
        setTimeout(() => location.reload(), 2200);
      }
    } catch { /* ignore network blips */ }
  }, 3000);
}

async function createCluster(event) {
  event.preventDefault();

  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form).entries());
  const response = await fetch("/api/clusters", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });

  if (!response.ok) {
    const problem = await response.json().catch(() => ({ detail: T('js.cluster_create_error') }));
    alert(problem.detail || T('js.cluster_create_error'));
    return;
  }

  const payload = await response.json();
  const result = document.getElementById("bootstrap-result");
  const target = document.getElementById("bootstrap-commands");
  target.textContent = payload.commands.join("\n");
  result.classList.remove("hidden");
  result.dataset.clusterSlug = payload.cluster_slug;

  // Start polling for PVE registration
  const statusEl = document.getElementById("pve-poll-status");
  pollBootstrapStatus(
    payload.cluster_slug,
    (data) => data.pve_registered,
    statusEl,
    T('js.pve_registered'),
    T('js.pve_registered_msg').replace('{name}', data.name || 'Cluster'),
  );
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

const clusterForm = document.getElementById("cluster-form");
if (clusterForm) {
  clusterForm.addEventListener("submit", createCluster);
}

// ── Cluster umbenennen – Lightbox ────────────────────────────────────────────
document.addEventListener("click", (event) => {
  const btn = event.target.closest(".js-rename-cluster-btn");
  if (!btn) return;
  event.preventDefault();
  const overlay = document.getElementById("rename-cluster-lightbox");
  if (!overlay) return;
  const input = document.getElementById("rename-cluster-input");
  const form = document.getElementById("rename-cluster-form");
  input.value = btn.dataset.clusterName;
  form.action = `/clusters/${btn.dataset.clusterSlug}/rename`;
  overlay.classList.remove("hidden");
  setTimeout(() => { input.focus(); input.select(); }, 50);
});

const renameCancelBtn = document.getElementById("rename-cluster-cancel");
if (renameCancelBtn) {
  renameCancelBtn.addEventListener("click", () => {
    document.getElementById("rename-cluster-lightbox").classList.add("hidden");
  });
}
const renameOverlay = document.getElementById("rename-cluster-lightbox");
if (renameOverlay) {
  renameOverlay.addEventListener("click", (e) => {
    if (e.target === renameOverlay) renameOverlay.classList.add("hidden");
  });
}

const renameForm = document.getElementById("rename-cluster-form");
if (renameForm) {
  renameForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const newName = document.getElementById("rename-cluster-input").value.trim();
    if (!newName) return;
    document.getElementById("rename-cluster-lightbox").classList.add("hidden");
    const formData = new FormData();
    formData.append("name", newName);
    fetch(renameForm.action, { method: "POST", body: formData }).then((resp) => {
      if (resp.ok || resp.redirected) {
        showSuccessLightbox(T('js.cluster_renamed'), T('js.new_name').replace('{name}', newName));
        setTimeout(() => location.reload(), 2200);
      } else {
        resp.json().then((data) => alert(data.detail || T('js.rename_error'))).catch(() => alert(T('js.rename_error')));
      }
    });
  });
}

// ── Cluster löschen – Lightbox ───────────────────────────────────────────────
document.addEventListener("click", (event) => {
  const btn = event.target.closest(".js-delete-cluster-btn");
  if (!btn) return;
  event.preventDefault();
  const overlay = document.getElementById("delete-cluster-lightbox");
  document.getElementById("delete-cluster-name").textContent = btn.dataset.clusterName;
  document.getElementById("delete-cluster-form").action = btn.dataset.deleteUrl;
  overlay.classList.remove("hidden");
});

const deleteClusterCancel = document.getElementById("delete-cluster-cancel");
if (deleteClusterCancel) {
  deleteClusterCancel.addEventListener("click", () => {
    document.getElementById("delete-cluster-lightbox").classList.add("hidden");
  });
}
// Close lightbox on overlay click
const deleteClusterOverlay = document.getElementById("delete-cluster-lightbox");
if (deleteClusterOverlay) {
  deleteClusterOverlay.addEventListener("click", (e) => {
    if (e.target === deleteClusterOverlay) deleteClusterOverlay.classList.add("hidden");
  });
}

// Intercept cluster delete form — show success lightbox after delete
const deleteClusterForm = document.getElementById("delete-cluster-form");
if (deleteClusterForm) {
  deleteClusterForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const clusterName = document.getElementById("delete-cluster-name").textContent;
    document.getElementById("delete-cluster-lightbox").classList.add("hidden");
    fetch(deleteClusterForm.action, { method: "POST" }).then(() => {
      showSuccessLightbox(T('js.cluster_deleted'), T('js.cluster_removed').replace('{name}', clusterName));
      setTimeout(() => location.reload(), 2200);
    });
  });
}

// ── PBS entfernen – Button-Text Sicherheitsabfrage + Lightbox ────────────────
document.addEventListener("submit", (event) => {
  const form = event.target.closest(".js-pbs-remove-form");
  if (!form) return;
  const btn = form.querySelector(".pbs-remove-btn");
  if (!btn) return;

  if (btn.dataset.confirmed === "1") {
    // Confirmed — intercept submit, do via fetch, show success lightbox
    event.preventDefault();
    fetch(form.action, { method: "POST" }).then(() => {
      showSuccessLightbox(T('js.pbs_removed'), T('js.pbs_removed_msg'));
      setTimeout(() => location.reload(), 2200);
    });
    return;
  }

  event.preventDefault();
  btn.textContent = T('js.pbs_remove_confirm');
  btn.dataset.confirmed = "1";
  // Reset after 3 seconds if not clicked again
  setTimeout(() => {
    if (btn.dataset.confirmed === "1") {
      btn.textContent = T('js.pbs_remove_btn');
      btn.dataset.confirmed = "";
    }
  }, 3000);
});

for (const button of document.querySelectorAll(".copy-button")) {
  button.addEventListener("click", () => {
    copyText(button.dataset.copyTarget, button);
  });
}

// ── PBS Bootstrap-Zeile per Cluster ein-/ausklappen + Polling ────────────────
document.addEventListener("click", (event) => {
  const btn = event.target.closest(".pbs-add-btn");
  if (!btn) return;
  const targetId = btn.dataset.target;
  const row = document.getElementById(targetId);
  if (!row) return;
  const isHidden = row.classList.contains("hidden");
  row.classList.toggle("hidden", !isHidden);
  btn.textContent = isHidden ? T('js.pbs_hide') : T('js.pbs_add');

  // Start polling when showing bootstrap panel
  if (isHidden && !btn.dataset.polling) {
    btn.dataset.polling = "1";
    const slug = btn.dataset.clusterSlug;
    const initialCount = parseInt(btn.dataset.pbsCount || "0", 10);
    const statusEl = row.querySelector(".js-pbs-poll-status");
    pollBootstrapStatus(
      slug,
      (data) => data.pbs_count > initialCount,
      statusEl,
      T('js.pbs_connected'),
      T('js.pbs_connected_msg'),
    );
  }
});

// ── Sync-Dot Klick → Log-Panel anzeigen (global, funktioniert auf allen Seiten) ──
document.addEventListener("click", (event) => {
  const dot = event.target.closest("button.sync-dot[data-log]");
  if (!dot) return;
  const panel = document.getElementById("sync-log-panel");
  const content = document.getElementById("sync-log-content");
  const title = document.getElementById("sync-log-title");
  if (!panel || !content) return;
  if (title) title.textContent = dot.dataset.logTitle || T('js.sync_log');
  content.textContent = dot.dataset.log;
  panel.classList.remove("hidden");
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
});

const syncLogClose = document.getElementById("sync-log-close");
if (syncLogClose) {
  syncLogClose.addEventListener("click", () => {
    document.getElementById("sync-log-panel").classList.add("hidden");
  });
}

// ── Sortierbare Tabellen ─────────────────────────────────────────────────────
for (const table of document.querySelectorAll(".js-sortable-table")) {
  const tbody = table.tBodies[0];
  if (!tbody) {
    continue;
  }

  const headers = Array.from(table.tHead?.rows[0]?.cells || []);
  for (const [index, header] of headers.entries()) {
    if (!header.classList.contains("sortable")) {
      continue;
    }

    const sortRows = () => {
      const currentDirection = header.dataset.direction === "asc" ? "asc" : (header.dataset.direction === "desc" ? "desc" : "none");
      const nextDirection = currentDirection === "asc" ? "desc" : "asc";
      const rows = Array.from(tbody.rows);
      const sortType = header.dataset.sortType || "text";

      for (const otherHeader of headers) {
        otherHeader.setAttribute("aria-sort", "none");
        if (otherHeader !== header) {
          otherHeader.dataset.direction = "none";
        }
      }

      rows.sort((leftRow, rightRow) => {
        const leftCell = leftRow.cells[index];
        const rightCell = rightRow.cells[index];
        const leftValue = (leftCell?.dataset.sortValue || leftCell?.textContent || "").trim();
        const rightValue = (rightCell?.dataset.sortValue || rightCell?.textContent || "").trim();

        if (sortType === "number") {
          return Number(leftValue) - Number(rightValue);
        }

        return leftValue.localeCompare(rightValue, undefined, {
          numeric: true,
          sensitivity: "base",
        });
      });

      if (nextDirection === "desc") {
        rows.reverse();
      }

      tbody.append(...rows);
      header.dataset.direction = nextDirection;
      header.setAttribute("aria-sort", nextDirection === "asc" ? "ascending" : "descending");
    };

    header.addEventListener("click", sortRows);
    header.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        sortRows();
      }
    });
  }
}

function applyRowVisibility(row) {
  const byNode = row.dataset.hiddenByNode === "true";
  const byStats = row.dataset.hiddenByStats === "true";
  row.classList.toggle("hidden", byNode || byStats);
}

for (const list of document.querySelectorAll(".js-node-filter-list")) {
  const container = list.closest(".table-card");
  const table = container?.querySelector(".js-node-filter-table");
  const tbody = table?.tBodies?.[0];
  const emptyState = container?.querySelector(".vm-filter-empty");
  if (!table || !tbody) {
    continue;
  }

  const rows = Array.from(tbody.rows);
  let activeNode = "";

  const applyNodeFilter = () => {
    for (const row of rows) {
      const passes = !activeNode || row.dataset.nodeName === activeNode;
      row.dataset.hiddenByNode = passes ? "false" : "true";
      applyRowVisibility(row);
    }

    for (const button of list.querySelectorAll(".node-filter-button")) {
      const buttonNode = button.dataset.nodeFilter || "";
      button.classList.toggle("is-active", buttonNode === activeNode || (!activeNode && buttonNode === ""));
    }

    if (emptyState) {
      const visibleRows = rows.filter((r) => !r.classList.contains("hidden")).length;
      emptyState.classList.toggle("hidden", visibleRows !== 0);
    }
  };

  for (const button of list.querySelectorAll(".node-filter-button")) {
    button.addEventListener("click", () => {
      const nextNode = button.dataset.nodeFilter || "";
      activeNode = activeNode === nextNode ? "" : nextNode;
      applyNodeFilter();
    });
  }
}

for (const tileGroup of document.querySelectorAll(".js-stat-filter-tiles")) {
  const targetId = tileGroup.dataset.filterTarget;
  const table = targetId ? document.querySelector(targetId) : tileGroup.closest("section")?.querySelector(".js-stat-filter-table");
  const tbody = table?.tBodies?.[0];
  if (!tbody) {
    continue;
  }

  const rows = Array.from(tbody.rows);
  const activeTags = new Set();

  const applyStatFilter = () => {
    for (const row of rows) {
      const tags = new Set((row.dataset.tags || "").split(" ").filter(Boolean));
      const passes = activeTags.size === 0 || [...activeTags].some((tag) => tags.has(tag));
      row.dataset.hiddenByStats = passes ? "false" : "true";
      applyRowVisibility(row);
    }

    for (const tile of tileGroup.querySelectorAll("[data-tile-filter]")) {
      tile.classList.toggle("is-filter-active", activeTags.has(tile.dataset.tileFilter));
    }
  };

  for (const tile of tileGroup.querySelectorAll("[data-tile-filter]")) {
    tile.style.cursor = "pointer";
    tile.addEventListener("click", () => {
      const tag = tile.dataset.tileFilter;
      if (activeTags.has(tag)) {
        activeTags.delete(tag);
      } else {
        activeTags.add(tag);
      }
      applyStatFilter();
    });
  }
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
          showSuccessLightbox(T('js.sync_complete'), T('js.data_updating'));
          setTimeout(() => location.reload(), 2500);
        }
      } catch { /* ignore */ }
    }, 2000);
  });
}

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

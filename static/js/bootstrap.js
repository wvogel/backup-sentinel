// ── Cluster/PBS Bootstrap flows ──────────────────────────────────────────────

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
        window.showSuccessLightbox(
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

const clusterForm = document.getElementById("cluster-form");
if (clusterForm) {
  clusterForm.addEventListener("submit", createCluster);
}

for (const button of document.querySelectorAll(".copy-button")) {
  button.addEventListener("click", () => {
    window.copyText(button.dataset.copyTarget, button);
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

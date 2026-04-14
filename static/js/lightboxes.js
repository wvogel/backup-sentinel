// ── Lightbox dialogs ─────────────────────────────────────────────────────────

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
        window.showSuccessLightbox(T('js.cluster_renamed'), T('js.new_name').replace('{name}', newName));
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
      window.showSuccessLightbox(T('js.cluster_deleted'), T('js.cluster_removed').replace('{name}', clusterName));
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
      window.showSuccessLightbox(T('js.pbs_removed'), T('js.pbs_removed_msg'));
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

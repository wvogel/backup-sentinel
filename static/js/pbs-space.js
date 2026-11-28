// ── Space per guest on a PBS datastore ───────────────────────────────────────
// A click on a datastore's ring ([data-pbs-space="<pbs id>"] data-store="…")
// lists what each guest's backups take there: an estimate the server keeps
// from the PBS's backup task logs (/api/pbs-space). The dialog is built here,
// once, so every page with a backup diagram has it.
(function () {
  let dialog = null;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function fill(key, values) {
    return T(key).replace(/\{(\w+)\}/g, (_, name) => (name in values ? values[name] : ""));
  }

  function build() {
    dialog = el("dialog", "log-dialog space-dialog");
    dialog.setAttribute("aria-labelledby", "space-title");
    const head = el("div", "log-dialog-head");
    const title = el("h3");
    title.id = "space-title";
    const close = el("button", "log-dialog-btn", "×");
    close.type = "button";
    close.title = T("common.close");
    close.setAttribute("aria-label", T("common.close"));
    close.addEventListener("click", () => dialog.close());
    head.append(title, close);
    const body = el("div", "space-body");
    body.tabIndex = 0;
    dialog.append(head, body);
    // A click on the backdrop (outside the box) closes it; Esc does natively.
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
    document.body.append(dialog);
  }

  function guestLabel(group) {
    const kind = group.backup_type === "ct" ? "CT" : "VM";
    return group.name ? group.name + " (" + kind + " " + group.backup_id + ")" : kind + " " + group.backup_id;
  }

  function render(body, data) {
    body.replaceChildren();
    if (!data.groups.length) {
      body.append(el("p", "muted space-empty", T("space.empty")));
      return;
    }
    const summary = el(
      "p",
      "space-summary",
      fill("space.summary", { guests: data.groups.length, snapshots: data.snapshots, size: data.size }),
    );
    if (data.known < data.snapshots) {
      summary.append(" ", el("span", "muted", fill("space.partial", { known: data.known, snapshots: data.snapshots })));
    }
    const list = el("div", "space-list");
    const namespaces = new Set(data.groups.map((group) => group.ns));
    data.groups.forEach((group) => {
      const row = el("div", "space-row");
      const name = el("div", "space-name", guestLabel(group));
      if (namespaces.size > 1 && group.ns) name.append(" ", el("span", "muted", group.ns));
      const bar = el("div", "space-bar");
      const fillBar = el("span");
      fillBar.style.width = group.share + "%";
      bar.append(fillBar);
      const count =
        group.snapshots === 1 ? T("space.snapshots_one") : fill("space.snapshots_other", { count: group.snapshots });
      const detail = el("div", "space-count muted", count);
      if (group.known < group.snapshots) detail.append(" · " + fill("space.known", { known: group.known }));
      row.append(name, bar, el("div", "space-size", group.size), detail);
      list.append(row);
    });
    body.append(summary, list, el("p", "muted space-note", T("space.note")));
  }

  async function open(trigger) {
    if (!dialog) build();
    const body = dialog.querySelector(".space-body");
    dialog.querySelector("h3").textContent = T("space.title") + " · " + trigger.dataset.store;
    body.replaceChildren(el("p", "muted space-empty", "…"));
    if (!dialog.open) dialog.showModal();
    try {
      const query = "pbs=" + encodeURIComponent(trigger.dataset.pbsSpace) + "&store=" + encodeURIComponent(trigger.dataset.store);
      const resp = await fetch("/api/pbs-space?" + query);
      if (!resp.ok) throw new Error(String(resp.status));
      render(body, await resp.json());
    } catch {
      body.replaceChildren(el("p", "critical-text space-empty", T("space.error")));
    }
    body.focus();
  }

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-pbs-space]");
    if (trigger) open(trigger);
  });
  document.addEventListener("keydown", (event) => {
    if ((event.key === "Enter" || event.key === " ") && event.target.matches && event.target.matches("[data-pbs-space]")) {
      event.preventDefault();
      open(event.target);
    }
  });
})();

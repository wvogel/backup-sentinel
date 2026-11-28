// ── Space per guest on a PBS datastore ───────────────────────────────────────
// A click on a datastore's ring ([data-pbs-space="<pbs id>"] data-store="…")
// lists what each guest's backups take there, counted by the server from the
// snapshots' index files (/api/pbs-space). The dialog is built here, once, so
// every page with a backup diagram has it.
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

  // Columns the list sorts by: a click on a heading sorts by it, a second
  // one turns the order. Sizes and counts start with the largest.
  const COLUMNS = [
    { key: "name", label: "space.col_guest", className: "", value: (g) => guestLabel(g).toLowerCase(), first: "asc" },
    { key: "bytes", label: "space.col_size", className: "space-size", value: (g) => g.bytes ?? -1, first: "desc" },
    {
      key: "exclusive",
      label: "space.col_exclusive",
      className: "space-size",
      value: (g) => g.exclusive_bytes ?? -1,
      first: "desc",
    },
    { key: "snapshots", label: "space.col_snapshots", className: "space-count", value: (g) => g.snapshots, first: "desc" },
  ];
  let order = { key: "bytes", direction: "desc" };

  function sorted(groups) {
    const column = COLUMNS.find((c) => c.key === order.key);
    const sign = order.direction === "asc" ? 1 : -1;
    return groups.slice().sort((a, b) => {
      const x = column.value(a);
      const y = column.value(b);
      if (x === y) return guestLabel(a).localeCompare(guestLabel(b));
      return (typeof x === "string" ? x.localeCompare(y) : x - y) * sign;
    });
  }

  function heading(column, redraw) {
    const cell = el("div", ("sortable " + column.className).trim(), T(column.label));
    cell.setAttribute("role", "button");
    cell.tabIndex = 0;
    const active = order.key === column.key;
    if (active) cell.dataset.direction = order.direction;
    cell.setAttribute("aria-sort", active ? (order.direction === "asc" ? "ascending" : "descending") : "none");
    const sort = () => {
      const turned = order.direction === "asc" ? "desc" : "asc";
      order = { key: column.key, direction: active ? turned : column.first };
      redraw(column.key);
    };
    cell.addEventListener("click", sort);
    cell.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        event.stopPropagation();
        sort();
      }
    });
    return cell;
  }

  function draw(list, data, focusKey) {
    list.replaceChildren();
    const head = el("div", "space-row space-head");
    const cells = COLUMNS.map((column) => heading(column, (key) => draw(list, data, key)));
    head.append(cells[0], el("div"), ...cells.slice(1));
    list.append(head);
    if (focusKey) cells[COLUMNS.findIndex((c) => c.key === focusKey)].focus();
    const namespaces = new Set(data.groups.map((group) => group.ns));
    sorted(data.groups).forEach((group) => {
      const row = el("div", "space-row");
      const name = el("div", "space-name", guestLabel(group));
      if (namespaces.size > 1 && group.ns) name.append(" ", el("span", "muted", group.ns));
      // The bar runs from 0 to the largest guest: solid for the chunks that
      // are this guest's alone, light for those it shares.
      const bar = el("div", "space-bar");
      const all = el("span", "space-bar-all");
      all.style.width = group.share + "%";
      const own = el("span", "space-bar-own");
      own.style.width = group.exclusive_share + "%";
      bar.append(all, own);
      const count = el("div", "space-count", String(group.snapshots));
      if (group.known < group.snapshots) {
        count.textContent = fill("space.known", { known: group.known, count: group.snapshots });
        count.classList.add("muted");
      }
      row.append(name, bar, el("div", "space-size", group.size), el("div", "space-size space-own", group.exclusive_size), count);
      list.append(row);
    });
  }

  function render(body, data) {
    body.replaceChildren();
    if (data.denied && !data.known) {
      body.append(el("p", "critical-text space-empty", T("space.denied")));
      return;
    }
    if (!data.known) {
      body.append(el("p", "muted space-empty", T("space.empty")));
      return;
    }
    // What the figures mean: behind the heading, as a "?".
    const size = data.chunk_size ? fill("space.note_size", { chunk: data.chunk_size }) : T("space.note_raw");
    const help = el("span", "help-dot", "?");
    help.tabIndex = 0;
    help.dataset.tooltip = T("space.note") + "\n\n" + size;
    dialog.querySelector("h3").append(" ", help);
    const summary = el(
      "p",
      "space-summary",
      fill("space.summary", { guests: data.groups.length, snapshots: data.snapshots }),
    );
    if (data.known < data.snapshots) {
      summary.append(" ", el("span", "muted", fill("space.partial", { known: data.known, snapshots: data.snapshots })));
    }
    const list = el("div", "space-list");
    draw(list, data);
    body.append(summary, list);
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

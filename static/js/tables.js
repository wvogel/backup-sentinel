// ── Table interaction ────────────────────────────────────────────────────────

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
      // Open detail rows (VM history) would be sorted as rows of their own.
      for (const detail of tbody.querySelectorAll(".vm-detail-row")) {
        detail.previousElementSibling?.classList.remove("is-expanded");
        detail.previousElementSibling?.setAttribute("aria-expanded", "false");
        detail.remove();
      }
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
  const byProblems = row.dataset.hiddenByProblems === "true";
  row.classList.toggle("hidden", byNode || byStats || byProblems);
}

// "Only problems": server-side flag (data-tags "problem") or a running backup
// flagged as slow by the live status (data-running-warning).
function rowHasProblem(row) {
  return (row.dataset.tags || "").split(" ").includes("problem") || Boolean(row.dataset.runningWarning);
}

for (const button of document.querySelectorAll(".js-problem-filter")) {
  const table = button.closest(".table-card")?.querySelector("table");
  const rows = Array.from(table?.tBodies?.[0]?.rows || []);
  const count = button.querySelector(".js-problem-count");
  let active = false;

  const apply = () => {
    if (count) count.textContent = rows.filter(rowHasProblem).length;
    for (const row of rows) {
      row.dataset.hiddenByProblems = active && !rowHasProblem(row) ? "true" : "false";
      applyRowVisibility(row);
    }
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
    scheduleAutosize();
  };

  button.addEventListener("click", () => {
    active = !active;
    apply();
  });
  // Live status may flag/unflag slow backups.
  document.addEventListener("backup-progress-updated", apply);
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

    for (const button of list.querySelectorAll(".node-filter-button[data-node-filter]")) {
      const buttonNode = button.dataset.nodeFilter || "";
      button.classList.toggle("is-active", buttonNode === activeNode || (!activeNode && buttonNode === ""));
    }

    if (emptyState) {
      const visibleRows = rows.filter((r) => !r.classList.contains("hidden")).length;
      emptyState.classList.toggle("hidden", visibleRows !== 0);
    }

    scheduleAutosize();
  };

  for (const button of list.querySelectorAll(".node-filter-button[data-node-filter]")) {
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

    scheduleAutosize();
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

// ── Auto-fit column widths (minimize wrapping) ───────────────────────────────
// Sizes every column to its natural single-line width. Only when the table is
// wider than its container do we let designated columns wrap — the ones with the
// lowest `data-wrappable` priority give way first — so as little wraps as needed.
function setColumnWrap(table, shouldWrap) {
  for (const row of table.rows) {
    const cells = row.cells;
    for (let i = 0; i < cells.length; i++) {
      cells[i].style.whiteSpace = shouldWrap(i) ? "" : "nowrap";
    }
  }
}

const SPARK_WIDTH = 246; // 31 days × 6 px + 30 × 2 px gaps (matches style.css)
const SPARK_MIN_WIDTH = 122; // 31 × 2 px + gaps

function autosizeTable(table) {
  const wrapper = table.closest(".table-scroll") || table.parentElement;
  const head = table.tHead?.rows?.[0];
  if (!wrapper || !head || !table.tBodies[0]) {
    return;
  }

  // On phones the table is restyled into stacked cards (see CSS); column sizing
  // is meaningless there, so clear any inline wrapping styles and bail.
  if (window.matchMedia && window.matchMedia("(max-width: 640px)").matches) {
    setColumnWrap(table, () => true);
    return;
  }

  // Columns allowed to wrap, ordered so the lowest priority gives way first.
  const wrapOrder = Array.from(head.cells)
    .map((header, index) => ({ index, priority: Number(header.dataset.wrappable) }))
    .filter((entry) => Number.isFinite(entry.priority) && entry.priority > 0)
    .sort((a, b) => a.priority - b.priority)
    .map((entry) => entry.index);

  // Start with everything on a single line so the browser sizes each column to
  // its natural width.
  setColumnWrap(table, () => false);
  table.style.removeProperty("--spark-w");
  if (table.scrollWidth <= wrapper.clientWidth + 1) {
    return; // fits already — no wrapping needed
  }

  // Too wide: first narrow the 30-day bars (down to ~2 px per day) — thinner
  // bars beat wrapped names.
  if (table.querySelector(".sparkline")) {
    const excess = table.scrollWidth - wrapper.clientWidth;
    table.style.setProperty("--spark-w", Math.max(SPARK_MIN_WIDTH, SPARK_WIDTH - excess) + "px");
    if (table.scrollWidth <= wrapper.clientWidth + 1) {
      return;
    }
  }

  // Too wide: enable wrapping one column at a time until it fits.
  const wrapped = new Set();
  for (const index of wrapOrder) {
    wrapped.add(index);
    setColumnWrap(table, (i) => wrapped.has(i));
    if (table.scrollWidth <= wrapper.clientWidth + 1) {
      return;
    }
  }

  // Even wrapping every wrappable column isn't enough (e.g. narrow phone): a
  // clean single-line layout with horizontal scroll beats cells that break
  // character-by-character, so revert to no wrapping and let the container scroll.
  setColumnWrap(table, () => false);
}

let autosizeFrame = 0;
function scheduleAutosize() {
  if (autosizeFrame) {
    cancelAnimationFrame(autosizeFrame);
  }
  autosizeFrame = requestAnimationFrame(() => {
    autosizeFrame = 0;
    for (const table of document.querySelectorAll(".js-autosize-table")) {
      autosizeTable(table);
    }
  });
}

scheduleAutosize();
window.addEventListener("resize", scheduleAutosize);
// Also re-fit when a table's container changes width without a window resize
// (late layout, scrollbar appearing, zoom).
if (window.ResizeObserver) {
  // Width only: autosizing itself changes row heights, which must not re-trigger it.
  const widths = new WeakMap();
  const observer = new ResizeObserver((entries) => {
    let changed = false;
    for (const entry of entries) {
      const width = Math.round(entry.contentRect.width);
      if (widths.get(entry.target) !== width) {
        widths.set(entry.target, width);
        changed = true;
      }
    }
    if (changed) {
      scheduleAutosize();
    }
  });
  for (const table of document.querySelectorAll(".js-autosize-table")) {
    observer.observe(table.closest(".table-scroll") || table.parentElement);
  }
}
if (document.fonts && document.fonts.ready) {
  document.fonts.ready.then(scheduleAutosize);
}

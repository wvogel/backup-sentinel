// Draws the lines of the cluster topology (templates/_topology.html):
// PVE → datastore (solid pipes) and replication (pipes of blocks). One line
// per pair of boxes. Neighbours are joined straight across; lines skipping a
// column (or going back, mutual backup) take a lane above or below all boxes.
// Steps per draw: fit the layout, measure, bundle edges per box pair, choose
// lanes, route, render. Only data moving right now is animated.
(function () {
  "use strict";
  var SVG_NS = "http://www.w3.org/2000/svg";
  var GAP = 18; // half the column gap
  var RADIUS = 8;

  // Polyline with rounded corners.
  function rounded(points) {
    var d = "M" + points[0][0] + "," + points[0][1];
    for (var i = 1; i < points.length - 1; i++) {
      var p = points[i - 1], c = points[i], n = points[i + 1];
      var r = Math.min(RADIUS, Math.hypot(c[0] - p[0], c[1] - p[1]) / 2, Math.hypot(n[0] - c[0], n[1] - c[1]) / 2);
      var a = [c[0] - Math.sign(c[0] - p[0]) * r, c[1] - Math.sign(c[1] - p[1]) * r];
      var b = [c[0] + Math.sign(n[0] - c[0]) * r, c[1] + Math.sign(n[1] - c[1]) * r];
      d += " L" + a[0] + "," + a[1] + " Q" + c[0] + "," + c[1] + " " + b[0] + "," + b[1];
    }
    var last = points[points.length - 1];
    return d + " L" + last[0] + "," + last[1];
  }

  // Every box as tall as the tallest one: a calm grid without stretching
  // single boxes to the height of a column holding several.
  function equalizeHeights(root, stacked) {
    var boxes = root.querySelectorAll(".topo-server, .topo-cluster");
    var tallest = 0;
    boxes.forEach(function (el) { el.style.minHeight = ""; });
    if (stacked) return;
    boxes.forEach(function (el) { tallest = Math.max(tallest, el.offsetHeight); });
    boxes.forEach(function (el) { el.style.minHeight = tallest + "px"; });
  }

  var calm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Direct lines are broad pipes; the long way around stays thin.
  function widthOf(edge) {
    if (edge.minor) return edge.lane ? 3 : 10;
    return edge.lane ? 5 : 24;
  }

  function headLength(width) {
    return width * 0.8 + 6;
  }

  // Filled arrowhead with its tip at (x, y), pointing along (dx, dy); a bit
  // wider than the pipe it ends.
  function head(x, y, dx, dy, width) {
    var len = headLength(width), half = width * 0.8 + 5;
    var bx = x - dx * len, by = y - dy * len;
    return "M" + x + "," + y + " L" + (bx - dy * half) + "," + (by + dx * half) +
      " L" + (bx + dy * half) + "," + (by - dx * half) + " Z";
  }

  function overlay(svg, d, cls, width) {
    var el = document.createElementNS(SVG_NS, "path");
    el.setAttribute("d", d);
    el.setAttribute("class", cls);
    el.setAttribute("stroke-width", width);
    svg.appendChild(el);
    return el;
  }

  // Only while data moves (running backup or sync): packets flow through
  // the pipe, a light sweep runs along it, a replication's blocks move.
  function animate(svg, path, d, edge, cls, width) {
    if (calm || !edge.active) return;
    var speed = edge.kind === "primary" ? 80 : 55; // px per second
    var length = path.getTotalLength();
    if (edge.kind === "sync") {
      var period = edge.lane ? width * 3.4 : width * 2; // dash + gap, see below
      path.style.setProperty("--flow-shift", -period + "px");
      path.style.setProperty("--flow-time", (period / speed).toFixed(3) + "s");
    }
    var dot = Math.max(2, width * 0.36), gap = Math.max(10, width * 1.4);
    var packets = overlay(svg, d, "topo-packets " + cls, dot);
    packets.setAttribute("stroke-dasharray", "0.1 " + gap);
    packets.style.setProperty("--flow-shift", -gap - 0.1 + "px");
    packets.style.setProperty("--flow-time", ((gap + 0.1) / speed).toFixed(3) + "s");
    var streak = Math.min(90, length * 0.35);
    var sweep = overlay(svg, d, "topo-sweep " + cls, width * 0.8);
    sweep.setAttribute("stroke-dasharray", streak + " " + (length + streak));
    sweep.style.strokeDashoffset = streak + "px";
    sweep.style.setProperty("--flow-shift", -length + "px");
    sweep.style.setProperty("--sweep-time", Math.max(1.4, length / 160).toFixed(2) + "s");
  }

  // PVE storages running backups write to: the guests' recent backup
  // storages, for the rows the progress poll marks as running.
  function activeStorages(root) {
    var result = new Set();
    var data = root.querySelector(".topology-vm-storages");
    if (!data) return result;
    var byVmid = JSON.parse(data.textContent || "{}");
    document.querySelectorAll("#vm-table tbody tr.is-running[data-vmid]").forEach(function (tr) {
      (byVmid[tr.dataset.vmid] || []).forEach(function (s) { result.add(s); });
    });
    return result;
  }

  // Many columns: narrow the gaps step by step until the diagram fits.
  function fitGaps(root, stacked) {
    root.style.columnGap = "";
    if (stacked) return;
    var gap = parseFloat(getComputedStyle(root).columnGap) || 0;
    while (root.scrollWidth > root.clientWidth + 1 && gap > 48) {
      gap -= 8;
      root.style.columnGap = gap + "px";
    }
  }

  // Box geometry relative to the SVG overlay.
  function measure(root) {
    var outer = root.getBoundingClientRect();
    var ox = outer.left + root.clientLeft - root.scrollLeft;
    var oy = outer.top + root.clientTop;
    function rect(el) {
      var r = el.getBoundingClientRect();
      return { left: r.left - ox, right: r.right - ox, top: r.top - oy, bottom: r.bottom - oy };
    }
    var cols = Array.prototype.map.call(root.querySelectorAll(".topology-col"), function (col) {
      var boxes = Array.prototype.map.call(col.querySelectorAll(".topo-server, .topo-cluster"), rect);
      return {
        el: col,
        left: Math.min.apply(null, boxes.map(function (b) { return b.left; })),
        right: Math.max.apply(null, boxes.map(function (b) { return b.right; })),
        middle: (Math.min.apply(null, boxes.map(function (b) { return b.top; })) +
          Math.max.apply(null, boxes.map(function (b) { return b.bottom; }))) / 2,
      };
    });
    var boxEls = Array.prototype.slice.call(root.querySelectorAll(".topo-server, .topo-cluster"));
    var boxes = boxEls.map(rect);
    return {
      cols: cols,
      boxEls: boxEls,
      boxes: boxes,
      top: Math.min.apply(null, boxes.map(function (b) { return b.top; })),
      bottom: Math.max.apply(null, boxes.map(function (b) { return b.bottom; })),
      // Middle of the gap after / before a column.
      gapAfter: function (i) {
        return i + 1 < cols.length ? (cols[i].right + cols[i + 1].left) / 2 : cols[i].right + GAP;
      },
      gapBefore: function (i) {
        return i > 0 ? (cols[i - 1].right + cols[i].left) / 2 : cols[i].left - GAP;
      },
      anchor: function (id) {
        var el = root.querySelector('[data-node="' + id + '"]');
        if (!el) return null;
        var boxEl = el.closest(".topo-server") || el;
        var b = rect(boxEl);
        var col = el.closest(".topology-col");
        b.box = boxEl;
        b.cx = (b.left + b.right) / 2;
        b.cy = (b.top + b.bottom) / 2;
        b.col = cols.findIndex(function (c) { return c.el === col; });
        return b;
      },
    };
  }

  // One connection per pair of boxes, however many datastores and jobs lie
  // between them; it takes the most telling state of its edges.
  var RANK = { running: 4, failed: 3, warning: 2, ok: 1, never: 0 };
  function bundle(edges, geo, active) {
    var groups = [], byKey = {};
    edges.forEach(function (edge) {
      var a = geo.anchor(edge.from), b = geo.anchor(edge.to);
      if (!a || !b) return;
      var isActive = edge.state === "running" || (edge.kind === "primary" && active.has(edge.storage_id));
      var key = [geo.boxEls.indexOf(a.box), geo.boxEls.indexOf(b.box), edge.kind, edge.back].join("|");
      var g = byKey[key];
      if (!g) {
        g = byKey[key] = { a: a, b: b, kind: edge.kind, back: edge.back, span: edge.span, state: edge.state, minor: true, active: false };
        groups.push(g);
      }
      if ((RANK[edge.state] || 0) > (RANK[g.state] || 0)) g.state = edge.state;
      g.minor = g.minor && !!edge.minor;
      g.active = g.active || isActive;
      g.span = Math.max(g.span, edge.span);
    });
    return groups;
  }

  // Lines skipping a column run above the boxes, or below them when their
  // target is a lower box of its column (then they cross nothing on the way
  // up). Decided by the boxes alone, so reserving lane space cannot flip it.
  function chooseLanes(groups, geo, stacked) {
    groups.forEach(function (g) {
      g.lane = !stacked && (g.back || g.span > 1);
      g.low = g.back || (g.span > 1 && g.b.cy > geo.cols[g.b.col].middle + 2);
    });
  }

  // Reserve room above / below the boxes for lanes; true if that changed.
  function reserveLanes(root, groups) {
    var top = groups.some(function (g) { return g.lane && !g.low; });
    var bottom = groups.some(function (g) { return g.lane && g.low; });
    var changed = root.classList.contains("has-top-lane") !== top || root.classList.contains("has-bottom-lane") !== bottom;
    root.classList.toggle("has-top-lane", top);
    root.classList.toggle("has-bottom-lane", bottom);
    return changed;
  }

  // Path of one connection, ending where its arrowhead begins.
  function route(g, geo, stacked, lanes) {
    var a = g.a, b = g.b, width = widthOf(g);
    var tip = headLength(width) - 1;
    if (stacked) {
      var direct = !g.back && b.top > a.bottom && !geo.boxes.some(function (box) {
        return box.top > a.bottom + 1 && box.bottom < b.top - 1; // a box in between
      });
      if (direct) {
        var my = (a.bottom + b.top) / 2;
        return {
          d: "M" + a.cx + "," + a.bottom + " C" + a.cx + "," + my + " " + b.cx + "," + my + " " + b.cx + "," + (b.top - tip),
          end: [b.cx, b.top], dir: [0, 1], width: width,
        };
      }
      var lx = 6 + 4 * lanes.side++;
      return {
        d: rounded([[a.left, a.cy], [lx, a.cy], [lx, b.cy], [b.left - tip, b.cy]]),
        end: [b.left, b.cy], dir: [1, 0], width: width,
      };
    }
    if (g.lane) {
      // Into the gap after the source column, along a lane just outside all
      // boxes, down the gap before the target; leaving and entering clear of
      // the broad pipe and its arrowhead between neighbours.
      var lane = g.low ? geo.bottom + 11 + 4 * lanes.bottom++ : geo.top - 11 - 4 * lanes.top++;
      var xa = geo.gapAfter(a.col), xb = geo.gapBefore(b.col);
      var clear = 24 * 0.8 + 5 + width / 2 + 8;
      var shift = g.low ? clear : -clear;
      return {
        d: rounded([[a.right, a.cy + shift], [xa, a.cy + shift], [xa, lane], [xb, lane], [xb, b.cy + shift], [b.left - tip, b.cy + shift]]),
        end: [b.left, b.cy + shift], dir: [1, 0], width: width,
      };
    }
    // Straight across through the height both boxes share.
    var top = Math.max(a.top, b.top), bottom = Math.min(a.bottom, b.bottom);
    if (bottom - top >= 16) {
      var y = (top + bottom) / 2;
      return { d: "M" + a.right + "," + y + " L" + (b.left - tip) + "," + y, end: [b.left, y], dir: [1, 0], width: width };
    }
    var mx = (a.right + b.left) / 2;
    return {
      d: "M" + a.right + "," + a.cy + " C" + mx + "," + a.cy + " " + mx + "," + b.cy + " " + (b.left - tip) + "," + b.cy,
      end: [b.left, b.cy], dir: [1, 0], width: width,
    };
  }

  // One group per connection (line, arrowhead, moving parts), so colour and
  // dimming apply to it as a whole.
  function render(svg, g, r) {
    var cls = "topo-line-" + g.kind + " topo-state-" + g.state + (g.minor ? " topo-minor" : "") +
      (g.lane ? " topo-lane" : "") + (g.active ? " topo-active" : "");
    var group = document.createElementNS(SVG_NS, "g");
    group.setAttribute("class", "topo-edge " + cls);
    svg.appendChild(group);
    var path = overlay(group, r.d, "topo-line " + cls, r.width);
    if (g.kind === "sync") {
      // Replication: a pipe of blocks, thin lanes finer.
      path.setAttribute("stroke-dasharray", g.lane ? r.width * 2 + " " + r.width * 1.4 : r.width * 1.4 + " " + r.width * 0.6);
    }
    var arrowhead = document.createElementNS(SVG_NS, "path");
    arrowhead.setAttribute("d", head(r.end[0], r.end[1], r.dir[0], r.dir[1], r.width));
    arrowhead.setAttribute("class", "topo-head " + cls);
    group.appendChild(arrowhead);
    animate(group, path, r.d, g, cls, r.width);
  }

  function draw(root) {
    var svg = root.querySelector(".topology-lines");
    var data = root.querySelector(".topology-edges");
    if (!svg || !data) return;
    var edges = JSON.parse(data.textContent || "[]");
    var stacked = getComputedStyle(root).flexDirection === "column";
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    fitGaps(root, stacked);
    equalizeHeights(root, stacked);
    var active = activeStorages(root);
    var geo = measure(root);
    var groups = bundle(edges, geo, active);
    chooseLanes(groups, geo, stacked);
    if (reserveLanes(root, groups)) {
      // Lane room moved the boxes: measure once more (lane choice is stable).
      geo = measure(root);
      groups = bundle(edges, geo, active);
      chooseLanes(groups, geo, stacked);
    }
    var lanes = { top: 0, bottom: 0, side: 0 };
    groups.forEach(function (g) { render(svg, g, route(g, geo, stacked, lanes)); });
  }

  function drawAll() {
    document.querySelectorAll("[data-topology]").forEach(draw);
  }

  // Live progress of running replication syncs, every 30 s while one runs.
  function pollSyncProgress(root) {
    if (!root.querySelector("[data-sync-key]")) return;
    var slug = decodeURIComponent(location.pathname.split("/")[2] || "");
    fetch("/api/clusters/" + encodeURIComponent(slug) + "/sync-progress")
      .then(function (resp) { return resp.ok ? resp.json() : {}; })
      .then(function (progress) {
        var finishedAny = false;
        root.querySelectorAll("[data-sync-key]").forEach(function (el) {
          var info = progress[el.dataset.syncKey];
          if (!info) return;
          var ring = root.querySelector('[data-sync-ring="' + el.dataset.syncKey + '"]');
          if (info.finished) {
            el.classList.remove("topo-state-running");
            el.classList.add("topo-state-ok");
            el.removeAttribute("data-sync-key");
            var when = el.querySelector(".topo-sync-when");
            if (when) when.textContent = T("topology.just_finished");
            if (ring) ring.remove();
            finishedAny = true;
          } else if (ring && info.percent !== null) {
            ring.classList.remove("is-indeterminate");
            ring.style.setProperty("--p", info.percent);
            ring.querySelector(".running-ring-label").textContent = Math.round(info.percent) + "%";
          }
        });
        if (finishedAny) {
          // The line stops glowing; the full result arrives with the next sync.
          var data = root.querySelector(".topology-edges");
          var finishedKeys = Object.keys(progress).filter(function (k) { return progress[k].finished; });
          var edges = JSON.parse(data.textContent || "[]");
          edges.forEach(function (edge) {
            if (edge.state === "running" && finishedKeys.indexOf(edge.sync_key) !== -1) edge.state = "ok";
          });
          data.textContent = JSON.stringify(edges);
          draw(root);
        }
      })
      .catch(function () { /* keep the last state */ })
      .then(function () { setTimeout(function () { pollSyncProgress(root); }, 30000); });
  }

  document.addEventListener("backup-progress-updated", drawAll);
  document.addEventListener("DOMContentLoaded", function () {
    drawAll();
    document.querySelectorAll("[data-topology]").forEach(function (root) {
      // Without a live value yet (ring turning, no number): ask right away.
      var pending = root.querySelector("[data-sync-ring].is-indeterminate");
      setTimeout(function () { pollSyncProgress(root); }, pending ? 100 : 2000);
    });
    if (window.ResizeObserver) {
      document.querySelectorAll("[data-topology]").forEach(function (root) {
        new ResizeObserver(function () { draw(root); }).observe(root);
      });
    } else {
      window.addEventListener("resize", drawAll);
    }
  });
})();

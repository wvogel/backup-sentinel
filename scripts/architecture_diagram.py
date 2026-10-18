"""Architecture diagram: writes docs/architecture-{en,de}.svg from one template.

Usage: python scripts/architecture_diagram.py [output dir, default docs]
The PNG copies (docs/architecture-*.png) are 2x renders of these SVGs.
"""

import sys
from html import escape

BG, PANEL, LINE, INK, MUTED = "#131325", "#1b1b34", "#363668", "#eeeef1", "#aeaec2"
BLUE, YELLOW, GREEN = "#89abcf", "#e8e87d", "#8dd3b0"
FONT = 'font-family="Inter,Segoe UI,system-ui,sans-serif"'

T = {
    "en": dict(
        title="Backup Sentinel — Architecture",
        browser="Browser",
        users="Admins, auditors",
        proxy_sub="OIDC (Entra ID, Keycloak …)",
        app="Backup Sentinel",
        app_sub="FastAPI · Uvicorn :80",
        app_lines=["Web UI (Jinja2)", "Sync worker", "Live progress", "Topology diagram", "Reports & notifications"],
        pve_sub="Nodes, VMs, backup jobs, task logs",
        pbs_sub="Snapshots, verify, datastores, sync jobs",
        gotify_sub="Push alerts",
        smtp_sub="Alerts, weekly report",
        db_sub="Clusters, VMs, backups,\ntopology, settings",
        rep="Reports",
        rep_sub="PDF / JSON\n/reports volume",
        prom="Prometheus",
        prom_sub="Metrics scraper",
        scrape="GET /metrics",
        legend_sync="Sync: every 60 min (configurable) and on demand",
        legend_live="Live: every 30 s while backups or replications run",
    ),
    "de": dict(
        title="Backup Sentinel — Architektur",
        browser="Browser",
        users="Admins, Auditoren",
        proxy_sub="OIDC (Entra ID, Keycloak …)",
        app="Backup Sentinel",
        app_sub="FastAPI · Uvicorn :80",
        app_lines=[
            "Web-Oberfläche (Jinja2)",
            "Sync-Worker",
            "Live-Fortschritt",
            "Topologie-Schaubild",
            "Berichte & Benachrichtigungen",
        ],
        pve_sub="Nodes, VMs, Backup-Jobs, Task-Logs",
        pbs_sub="Snapshots, Verify, Datastores, Sync-Jobs",
        gotify_sub="Push-Alarme",
        smtp_sub="Alarme, Wochenbericht",
        db_sub="Cluster, VMs, Backups,\nTopologie, Einstellungen",
        rep="Berichte",
        rep_sub="PDF / JSON\nVolume /reports",
        prom="Prometheus",
        prom_sub="Metriken-Abfrage",
        scrape="GET /metrics",
        legend_sync="Sync: alle 60 min (einstellbar) und auf Knopfdruck",
        legend_live="Live: alle 30 s, solange Backups oder Replikationen laufen",
    ),
}


def text(x, y, s, size=11, fill=INK, weight=400, anchor="middle"):
    return f'<text x="{x}" y="{y}" text-anchor="{anchor}" fill="{fill}" {FONT} font-size="{size}" font-weight="{weight}">{escape(s)}</text>'


def box(x, y, w, h, title, sub="", accent=None):
    out = [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{PANEL}" stroke="{accent or LINE}" stroke-width="1.2"/>'
    ]
    lines = sub.split("\n") if sub else []
    top = y + h / 2 - (len(lines) * 13) / 2 + 4
    out.append(text(x + w / 2, top, title, 12, INK, 600))
    for i, line in enumerate(lines):
        out.append(text(x + w / 2, top + 16 + i * 13, line, 9.5, MUTED))
    return out


def arrow(points, color=BLUE, dashed=False, label=None, lx=0, ly=0, anchor="middle"):
    d = "M" + " L".join(f"{x},{y}" for x, y in points)
    (x1, y1), (x2, y2) = points[-2], points[-1]
    dx, dy = x2 - x1, y2 - y1
    n = (dx * dx + dy * dy) ** 0.5
    ux, uy = dx / n, dy / n
    bx, by = x2 - ux * 8, y2 - uy * 8
    head = f"{x2},{y2} {bx - uy * 4:.1f},{by + ux * 4:.1f} {bx + uy * 4:.1f},{by - ux * 4:.1f}"
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    out = [
        f'<path d="{d}" fill="none" stroke="{color}" stroke-width="1.5"{dash}/>',
        f'<polygon points="{head}" fill="{color}"/>',
    ]
    if label:
        out.append(text(lx, ly, label, 9, color, 500, anchor))
    return out


def build(lang):
    t = T[lang]
    W, H = 960, 470
    o = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}">',
        f'<rect width="{W}" height="{H}" rx="12" fill="{BG}"/>',
        text(W / 2, 36, t["title"], 17, INK, 700),
    ]
    # Left: browser, proxy, prometheus
    o += box(30, 140, 130, 64, t["browser"], t["users"])
    o += box(200, 140, 140, 64, "OAuth2-Proxy", t["proxy_sub"])
    o += box(200, 330, 140, 64, t["prom"], t["prom_sub"])
    # App
    ax, ay, aw, ah = 380, 70, 280, 210
    o.append(
        f'<rect x="{ax}" y="{ay}" width="{aw}" height="{ah}" rx="8" fill="{PANEL}" stroke="{YELLOW}" stroke-width="1.4"/>'
    )
    o.append(text(ax + aw / 2, ay + 28, t["app"], 14, YELLOW, 700))
    o.append(text(ax + aw / 2, ay + 45, t["app_sub"], 9.5, MUTED))
    for i, line in enumerate(t["app_lines"]):
        o.append(text(ax + aw / 2, ay + 78 + i * 24, line, 11, INK))
    # Below the app: database and reports
    o += box(380, 330, 135, 74, "PostgreSQL 18", t["db_sub"])
    o += box(525, 330, 135, 74, t["rep"], t["rep_sub"])
    # Right: PVE, PBS, Gotify, SMTP
    rx, rw = 720, 210
    o += box(rx, 60, rw, 60, "Proxmox VE API", t["pve_sub"], GREEN)
    o += box(rx, 140, rw, 60, "Proxmox Backup Server API", t["pbs_sub"], GREEN)
    o += box(rx, 245, rw, 50, "Gotify", t["gotify_sub"])
    o += box(rx, 310, rw, 50, "SMTP", t["smtp_sub"])
    # Arrows
    o += arrow([(160, 172), (198, 172)], label="HTTPS", lx=179, ly=165)
    o += arrow([(340, 172), (378, 172)], label="Auth", lx=359, ly=165)
    o += arrow([(340, 362), (360, 362), (360, 250), (378, 250)], label=t["scrape"], lx=354, ly=325, anchor="end")
    o += arrow([(447, 280), (447, 328)], label="SQL", lx=454, ly=308, anchor="start")
    o += arrow([(592, 280), (592, 328)], label="PDF", lx=599, ly=308, anchor="start")
    # Sync (solid) and live (dashed) to PVE and PBS
    o += arrow([(660, 86), (718, 86)])
    o += arrow([(660, 100), (718, 100)], dashed=True)
    o += arrow([(660, 166), (718, 166)])
    o += arrow([(660, 180), (718, 180)], dashed=True)
    o += arrow([(660, 255), (684, 255), (684, 270), (718, 270)], label="Push", lx=702, ly=264)
    o += arrow([(660, 268), (674, 268), (674, 335), (718, 335)], label="E-Mail", lx=697, ly=329)
    # Legend
    ly = 438
    o += arrow([(380, ly), (414, ly)])
    o.append(text(422, ly + 3.5, t["legend_sync"], 10, MUTED, 400, "start"))
    o += arrow([(380, ly + 18), (414, ly + 18)], dashed=True)
    o.append(text(422, ly + 21.5, t["legend_live"], 10, MUTED, 400, "start"))
    o.append("</svg>")
    return "\n".join(o) + "\n"


if __name__ == "__main__":
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "docs"
    for lang in ("en", "de"):
        with open(f"{out_dir}/architecture-{lang}.svg", "w") as f:
            f.write(build(lang))

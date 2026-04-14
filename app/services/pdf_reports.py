from __future__ import annotations

import io
import logging
from datetime import UTC
from pathlib import Path
from urllib import request as urllib_request

from reportlab.graphics import renderPDF
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.flowables import Flowable
from svglib.svglib import svg2rlg

from app.config import BRAND_LOGO_LIGHT_URL, DEFAULT_TIMEZONE

logger = logging.getLogger(__name__)

# ── Colour palette ──────────────────────────────────────────────────────────
C_HEADER = colors.HexColor("#141450")
C_ACCENT = colors.HexColor("#1e3a8a")
C_OK = colors.HexColor("#059669")
C_FAIL = colors.HexColor("#b22c3a")
C_MUTED = colors.HexColor("#64748b")
C_ROW_ALT = colors.HexColor("#f1f5f9")
C_LINE = colors.HexColor("#e2e8f0")
C_WHITE = colors.white
C_BLACK = colors.HexColor("#1e293b")
C_CLUSTER = colors.HexColor("#dbeafe")

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm


# ── Label dictionaries (minimal i18n for PDF reports) ────────────────────────
# The rest of the app uses app.i18n; PDF reports intentionally use this small
# self-contained dict so the PDF generator stays independent of the web layer.
_PDF_LABELS_DE: dict[str, str] = {
    "backup_report": "Backup-Report",
    "period": "Zeitraum",
    "vms": "VMs",
    "events": "Ereignisse",
    "successful": "Erfolgreich",
    "errors": "Fehler",
    "compare_prev_month": "Vergleich zum Vormonat",
    "new": "neu",
    "current": "Aktuell",
    "prev_month": "Vormonat",
    "change": "Veränderung",
    "cluster_prefix": "Cluster: ",
    "deleted": "gelöscht",
    "deleted_status": "Gelöscht",
    "yes": "Ja",
    "no": "Nein",
    "ok": "OK",
    "error": "Fehler",
    "date": "Datum",
    "size": "Größe",
    "encrypted": "Verschlüsselt",
    "encrypted_short": "Encrypted",
    "verified": "Verifiziert",
    "verify_short": "Verify",
    "status": "Status",
    "node": "Node",
    "vm": "VM",
    "created_at": "Erstellt:",
    "page_of": "Seite {n} von {total}",
    "footer_brand": "Backup Sentinel {version}",
    "header_banner_label": "BACKUP REPORT",
    "notifications_title": "Benachrichtigungsprotokoll",
    "notifications_subtitle": "{count} Benachrichtigungen im Berichtszeitraum (NIST CSF Detect / NIS2 Art. 21)",
    "notif_col_date": "Datum",
    "notif_col_type": "Typ",
    "notif_col_cluster": "Cluster",
    "notif_col_channel": "Kanal",
    "notif_col_message": "Nachricht",
    "notif_type_backup_critical": "Backup kritisch",
    "notif_type_size_anomaly": "Größenanomalie",
    "notif_type_sync_failed": "Sync fehlgeschlagen",
    "notif_type_sync_overdue": "Sync überfällig",
    "notif_type_restore_overdue": "Restore überfällig",
    "notif_type_unencrypted_backups": "Unverschlüsselt",
    # Date/time formatting strings (German style: day.month.year)
    "fmt_datetime": "%d.%m.%Y %H:%M",
}

_PDF_LABELS_EN: dict[str, str] = {
    "backup_report": "Backup Report",
    "period": "Period",
    "vms": "VMs",
    "events": "Events",
    "successful": "Successful",
    "errors": "Errors",
    "compare_prev_month": "Comparison to previous month",
    "new": "new",
    "current": "Current",
    "prev_month": "Previous month",
    "change": "Change",
    "cluster_prefix": "Cluster: ",
    "deleted": "deleted",
    "deleted_status": "Deleted",
    "yes": "Yes",
    "no": "No",
    "ok": "OK",
    "error": "Error",
    "date": "Date",
    "size": "Size",
    "encrypted": "Encrypted",
    "encrypted_short": "Encrypted",
    "verified": "Verified",
    "verify_short": "Verify",
    "status": "Status",
    "node": "Node",
    "vm": "VM",
    "created_at": "Generated:",
    "page_of": "Page {n} of {total}",
    "footer_brand": "Backup Sentinel {version}",
    "header_banner_label": "BACKUP REPORT",
    "notifications_title": "Notification log",
    "notifications_subtitle": "{count} notifications in the reporting period (NIST CSF Detect / NIS2 Art. 21)",
    "notif_col_date": "Date",
    "notif_col_type": "Type",
    "notif_col_cluster": "Cluster",
    "notif_col_channel": "Channel",
    "notif_col_message": "Message",
    "notif_type_backup_critical": "Backup critical",
    "notif_type_size_anomaly": "Size anomaly",
    "notif_type_sync_failed": "Sync failed",
    "notif_type_sync_overdue": "Sync overdue",
    "notif_type_restore_overdue": "Restore overdue",
    "notif_type_unencrypted_backups": "Unencrypted",
    # ISO-ish date style for English reports
    "fmt_datetime": "%Y-%m-%d %H:%M",
}


def _labels(lang: str) -> dict[str, str]:
    """Return the PDF label dict for the requested language (fallback: DE)."""
    if (lang or "").lower() == "en":
        return _PDF_LABELS_EN
    return _PDF_LABELS_DE


# Backward-compat: older callers and tests import this module-level dict.
# It mirrors the German labels for the notification types. For multi-language
# output use the per-language lookup via _labels(lang).
_NOTIFICATION_TYPE_LABELS = {
    "backup_critical": _PDF_LABELS_DE["notif_type_backup_critical"],
    "size_anomaly": _PDF_LABELS_DE["notif_type_size_anomaly"],
    "sync_failed": _PDF_LABELS_DE["notif_type_sync_failed"],
    "sync_overdue": _PDF_LABELS_DE["notif_type_sync_overdue"],
    "restore_overdue": _PDF_LABELS_DE["notif_type_restore_overdue"],
    "unencrypted_backups": _PDF_LABELS_DE["notif_type_unencrypted_backups"],
}


def _notif_type_label(L: dict[str, str], notification_type: str) -> str:
    """Look up a localized label for a notification type, or echo the raw key."""
    key = f"notif_type_{notification_type}"
    return L.get(key, notification_type)


# ── Logo fetch ───────────────────────────────────────────────────────────────
def _fetch_logo(url: str):
    """Fetch logo from URL. Returns bytes (PNG/JPEG) or Drawing (SVG) or None."""
    if not url:
        return None
    try:
        req = urllib_request.Request(url, headers={"Accept": "*/*"})
        with urllib_request.urlopen(req, timeout=5) as resp:
            ct = resp.headers.get("Content-Type", "")
            data = resp.read()
            if any(t in ct for t in ("png", "jpeg", "jpg")):
                return data
            if "svg" in ct or url.lower().endswith(".svg"):
                drawing = svg2rlg(io.BytesIO(data))
                return drawing
    except Exception as exc:
        logger.debug("Logo fetch failed (%s): %s", url, exc)
    return None


def _logo_url_normal() -> str:
    """Derive the normal (dark-on-light) logo URL from the invers URL."""
    url = BRAND_LOGO_LIGHT_URL or ""
    # logo-invers.svg → logo.svg
    if "logo-invers" in url:
        return url.replace("logo-invers", "logo")
    return url


# ── Header banner (custom Flowable) ─────────────────────────────────────────
def _draw_logo_on_canvas(canvas, logo, x, y, target_h) -> bool:
    """Draw a logo (bytes or SVG Drawing) on a canvas at (x, y) with given height."""
    from reportlab.graphics.shapes import Drawing

    if not logo:
        return False
    if isinstance(logo, bytes):
        reader = ImageReader(io.BytesIO(logo))
        canvas.drawImage(reader, x, y, height=target_h, preserveAspectRatio=True, mask="auto")
        return True
    if isinstance(logo, Drawing):
        scale = target_h / logo.height if logo.height else 1
        canvas.saveState()
        canvas.translate(x, y)
        canvas.scale(scale, scale)
        renderPDF.draw(logo, canvas, 0, 0)
        canvas.restoreState()
        return True
    return False


class HeaderBanner(Flowable):
    def __init__(self, title: str, subtitle: str, logo=None, banner_label: str = "BACKUP REPORT"):
        super().__init__()
        self._title = title
        self._subtitle = subtitle
        self._logo = logo
        self._banner_label = banner_label
        self._h = 24 * mm

    def wrap(self, aW, aH):
        self._w = aW
        return aW, self._h

    def draw(self):
        c = self.canv
        w, h = self._w, self._h
        # Background
        c.setFillColor(C_HEADER)
        c.rect(0, 0, w, h, fill=1, stroke=0)
        # Logo or company name
        logo_drawn = False
        if self._logo:
            try:
                logo_drawn = _draw_logo_on_canvas(c, self._logo, 6 * mm, 4 * mm, 16 * mm)
            except Exception:
                pass
        if not logo_drawn:
            c.setFillColor(C_WHITE)
            c.setFont("Helvetica-Bold", 13)
            c.drawString(6 * mm, h / 2 - 5, self._title)
        # Subtitle right-aligned
        c.setFillColor(colors.HexColor("#93c5fd"))
        c.setFont("Helvetica", 8)
        c.drawRightString(w - 6 * mm, h / 2 + 4, self._banner_label)
        c.setFillColor(C_WHITE)
        c.setFont("Helvetica-Bold", 9)
        c.drawRightString(w - 6 * mm, h / 2 - 7, self._subtitle)


# ── Numbered canvas for page numbering ───────────────────────────────────────
class _NumberedCanvas:
    """Mixin-style wrapper: collects page count, draws footer + header logo."""

    def __init__(
        self,
        generated_at: str,
        logo_normal_bytes: bytes | None,
        build_version: str = "",
        labels: dict[str, str] | None = None,
    ):
        self._generated_at = generated_at
        self._logo_normal = logo_normal_bytes
        self._build_version = build_version
        self._labels = labels or _PDF_LABELS_DE

    def __call__(self, filename, **kwargs):
        from reportlab.pdfgen.canvas import Canvas

        outer = self

        class _Inner(Canvas):
            def __init__(self, *args, **kw):
                super().__init__(*args, **kw)
                self._saved_pages: list = []
                self._gen_at = outer._generated_at
                self._logo_normal = outer._logo_normal
                self._build_version = outer._build_version
                self._labels = outer._labels

            def showPage(self):
                self._saved_pages.append(dict(self.__dict__))
                self._startPage()  # reset canvas for next page WITHOUT emitting

            def save(self):
                total = len(self._saved_pages)
                for i, state in enumerate(self._saved_pages, 1):
                    self.__dict__.update(state)
                    self._draw_page_furniture(i, total)
                    super().showPage()
                super().save()

            def _draw_page_furniture(self, page_num: int, total: int):
                self.saveState()
                y = 12 * mm
                # Footer line
                self.setStrokeColor(C_LINE)
                self.setLineWidth(0.4)
                self.line(MARGIN, y + 5 * mm, PAGE_W - MARGIN, y + 5 * mm)
                self.setFont("Helvetica", 7.5)
                self.setFillColor(C_MUTED)
                L = self._labels
                self.drawString(MARGIN, y, L["footer_brand"].format(version=self._build_version))
                self.drawCentredString(PAGE_W / 2, y, L["page_of"].format(n=page_num, total=total))
                self.drawRightString(PAGE_W - MARGIN, y, f"{L['created_at']} {self._gen_at}")

                # Normal logo top-right on pages 2+ (page 1 has the banner)
                if page_num > 1 and self._logo_normal:
                    try:
                        from reportlab.graphics.shapes import Drawing

                        logo_h = 8 * mm
                        logo = self._logo_normal
                        if isinstance(logo, bytes):
                            reader = ImageReader(io.BytesIO(logo))
                            iw, ih = reader.getSize()
                            logo_w = logo_h * (iw / ih) if ih else 20 * mm
                            self.setFillAlpha(0.6)
                            self.drawImage(
                                reader,
                                PAGE_W - MARGIN - logo_w,
                                PAGE_H - MARGIN + 1 * mm,
                                width=logo_w,
                                height=logo_h,
                                preserveAspectRatio=True,
                                mask="auto",
                            )
                            self.setFillAlpha(1.0)
                        elif isinstance(logo, Drawing):
                            scale = logo_h / logo.height if logo.height else 1
                            logo_w = logo.width * scale
                            self.saveState()
                            self.setFillAlpha(0.6)
                            self.translate(PAGE_W - MARGIN - logo_w, PAGE_H - MARGIN + 1 * mm)
                            self.scale(scale, scale)
                            renderPDF.draw(logo, self, 0, 0)
                            self.restoreState()
                    except Exception:
                        pass

                self.restoreState()

        return _Inner(filename, **kwargs)


# ── Style helpers ─────────────────────────────────────────────────────────────
def _styles() -> dict:
    return {
        "h1": ParagraphStyle(
            "h1", fontName="Helvetica-Bold", fontSize=20, textColor=C_HEADER, spaceAfter=2 * mm, leading=24
        ),
        "h2": ParagraphStyle(
            "h2",
            fontName="Helvetica-Bold",
            fontSize=13,
            textColor=C_HEADER,
            spaceBefore=4 * mm,
            spaceAfter=1 * mm,
            leading=16,
        ),
        "body": ParagraphStyle("body", fontName="Helvetica", fontSize=9, textColor=C_BLACK, leading=13),
        "muted": ParagraphStyle("muted", fontName="Helvetica", fontSize=8, textColor=C_MUTED, leading=11),
        "ok": ParagraphStyle("ok", fontName="Helvetica-Bold", fontSize=9, textColor=C_OK),
        "fail": ParagraphStyle("fail", fontName="Helvetica-Bold", fontSize=9, textColor=C_FAIL),
        "th": ParagraphStyle("th", fontName="Helvetica-Bold", fontSize=8, textColor=C_WHITE, leading=10),
        "td": ParagraphStyle("td", fontName="Helvetica", fontSize=8, textColor=C_BLACK, leading=11),
        "cluster_hd": ParagraphStyle(
            "cluster_hd", fontName="Helvetica-Bold", fontSize=9, textColor=C_ACCENT, leading=12
        ),
        "center": ParagraphStyle("center", fontName="Helvetica", fontSize=9, alignment=TA_CENTER, textColor=C_BLACK),
    }


# ── Number formatting ────────────────────────────────────────────────────────
def _fmt_num(n: int | float, lang: str = "de") -> str:
    """Format integer with thousand separators.
    DE uses '.', EN uses ','. Kept backward-compatible: default is DE."""
    if (lang or "").lower() == "en":
        return f"{int(n):,}"
    return f"{int(n):,}".replace(",", ".")


# ── Summary table ─────────────────────────────────────────────────────────────
def _summary_table(totals: dict, st: dict, L: dict[str, str], lang: str) -> Table:
    data = [
        [
            _stat_cell(L["vms"], _fmt_num(totals["vms"], lang), st),
            _stat_cell(L["events"], _fmt_num(totals["events"], lang), st),
            _stat_cell(L["successful"], _fmt_num(totals["ok"], lang), st, color=C_OK),
            _stat_cell(L["errors"], _fmt_num(totals["failed"], lang), st, color=C_FAIL),
        ]
    ]
    col_w = (PAGE_W - 2 * MARGIN) / 4
    t = Table(data, colWidths=[col_w] * 4, rowHeights=[22 * mm])
    t.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.5, C_LINE),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, C_LINE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
                ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.HexColor("#f8fafc")]),
            ]
        )
    )
    return t


def _stat_cell(label: str, value: str, st: dict, color=None) -> list:
    val_style = ParagraphStyle(
        "sv", fontName="Helvetica-Bold", fontSize=18, textColor=color or C_HEADER, alignment=TA_CENTER, leading=22
    )
    lbl_style = ParagraphStyle(
        "sl", fontName="Helvetica", fontSize=8, textColor=C_MUTED, alignment=TA_CENTER, leading=10
    )
    return [Paragraph(value, val_style), Paragraph(label, lbl_style)]


# ── Month comparison section ──────────────────────────────────────────────────
def _comparison_section(comparison: dict, st: dict, L: dict[str, str], lang: str) -> list:
    """Build flowables for month-over-month comparison."""
    flowables = []
    flowables.append(Paragraph(L["compare_prev_month"], st["h2"]))

    def _delta_str(pct: float | None) -> str:
        if pct is None:
            return L["new"]
        sign = "+" if pct > 0 else ""
        return f"{sign}{pct:.1f}%"

    def _delta_color(pct: float | None, invert: bool = False) -> colors.Color:
        if pct is None:
            return C_MUTED
        if invert:
            return C_OK if pct <= 0 else C_FAIL
        return C_OK if pct >= 0 else C_FAIL

    cur = comparison["current"]
    prev = comparison["previous"]

    td = st["td"]
    td_r = ParagraphStyle("td_right", parent=td, alignment=TA_RIGHT)

    header = [
        Paragraph("", st["th"]),
        Paragraph(L["current"], st["th"]),
        Paragraph(L["prev_month"], st["th"]),
        Paragraph(L["change"], st["th"]),
    ]
    rows_data = [header]

    metrics = [
        (L["events"], cur["total"], prev["total"], comparison["delta_total_pct"], False),
        (L["successful"], cur["ok"], prev["ok"], comparison["delta_ok_pct"], False),
        (L["errors"], cur["failed"], prev["failed"], comparison["delta_failed_pct"], True),
        (L["vms"], cur["vms"], prev["vms"], None, False),
    ]

    for label, cur_val, prev_val, delta_pct, invert in metrics:
        delta_text = (
            _delta_str(delta_pct)
            if delta_pct is not None
            else (f"+{comparison['delta_vms']}" if comparison["delta_vms"] > 0 else str(comparison["delta_vms"]))
            if label == L["vms"]
            else "–"
        )
        dc = _delta_color(delta_pct, invert) if delta_pct is not None else C_MUTED
        rows_data.append(
            [
                Paragraph(f"<b>{label}</b>", td),
                Paragraph(_fmt_num(cur_val, lang), td_r),
                Paragraph(_fmt_num(prev_val, lang), td_r),
                Paragraph(f"<font color='{dc.hexval()}'>{delta_text}</font>", td_r),
            ]
        )

    col_w = (PAGE_W - 2 * MARGIN) / 4
    t = Table(rows_data, colWidths=[col_w] * 4)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), C_HEADER),
                ("TEXTCOLOR", (0, 0), (-1, 0), C_WHITE),
                ("GRID", (0, 0), (-1, -1), 0.3, C_LINE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [C_WHITE, C_ROW_ALT]),
            ]
        )
    )
    flowables.append(t)
    return flowables


# ── Backup events table ───────────────────────────────────────────────────────
_COL_W = [36 * mm, 24 * mm, 26 * mm, 22 * mm, 20 * mm, 18 * mm, 24 * mm]  # total ≈ 170mm usable


def _fmt_bytes(value: object, lang: str = "de") -> str:
    try:
        v = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "–"
    # Thousands separator depends on language; decimal stays '.'
    if (lang or "").lower() == "en":
        if v >= 1_073_741_824:
            return f"{v / 1_073_741_824:,.1f} GB"
        if v >= 1_048_576:
            return f"{v / 1_048_576:,.0f} MB"
        return f"{v / 1024:,.0f} KB"
    # German: swap thousand sep to '.' (kept as in the original implementation)
    if v >= 1_073_741_824:
        return f"{v / 1_073_741_824:,.1f} GB".replace(",", ".")
    if v >= 1_048_576:
        return f"{v / 1_048_576:,.0f} MB".replace(",", ".")
    return f"{v / 1024:,.0f} KB".replace(",", ".")


def _events_table(rows: list[dict], st: dict, L: dict[str, str], lang: str, tz_name: str = "Europe/Berlin") -> list:
    """Return a list of flowables: per-cluster chapter with header + table."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    dt_fmt = L["fmt_datetime"]

    def fmt_dt(dt, fmt: str) -> str:
        if dt is None:
            return "–"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(tz).strftime(fmt)

    th_cols = [
        L["vm"],
        L["node"],
        L["date"],
        L["size"],
        L["encrypted_short"],
        L["verify_short"],
        L["status"],
    ]

    flowables = []
    clusters: dict[str, list] = {}
    for row in rows:
        clusters.setdefault(row["cluster_name"], []).append(row)

    first_cluster = True
    for cluster_name, vm_rows in clusters.items():
        # Each cluster starts on a new page (except the first which follows the summary)
        if not first_cluster:
            flowables.append(PageBreak())
        first_cluster = False

        # Cluster chapter headline
        flowables.append(Paragraph(f"{L['cluster_prefix']}{cluster_name}", st["h1"]))
        # Cluster-level summary line
        cluster_vms = len(vm_rows)
        cluster_events = sum(r["event_count"] for r in vm_rows)
        cluster_ok = sum(r["ok_count"] for r in vm_rows)
        cluster_fail = sum(r["failed_count"] for r in vm_rows)
        cluster_deleted = sum(r.get("deleted_count", 0) for r in vm_rows)
        flowables.append(
            Paragraph(
                f"{_fmt_num(cluster_vms, lang)} {L['vms']}  ·  {_fmt_num(cluster_events, lang)} {L['events']}  ·  "
                f"<font color='#059669'>{_fmt_num(cluster_ok, lang)} {L['ok']}</font>  ·  "
                f"<font color='#b22c3a'>{_fmt_num(cluster_fail, lang)} {L['errors']}</font>"
                + (f"  ·  {_fmt_num(cluster_deleted, lang)} {L['deleted']}" if cluster_deleted else ""),
                st["body"],
            )
        )
        flowables.append(HRFlowable(width="100%", thickness=0.8, color=C_ACCENT, spaceAfter=3 * mm))

        header = [Paragraph(h, st["th"]) for h in th_cols]
        table_data = [header]
        row_styles: list[tuple] = []
        data_row_idx = 1

        for vm_row in vm_rows:
            vm_name = vm_row["vm_name"]
            node_name = vm_row["node_name"]
            first = True
            for evt in vm_row["events"]:
                status_for_report = evt.get("status_for_report", "ok" if evt["status"] == "ok" else "failed")
                is_ok = status_for_report == "ok"
                is_deleted = status_for_report == "deleted"
                status_para = Paragraph(
                    L["deleted_status"] if is_deleted else (L["ok"] if is_ok else L["error"]),
                    st["muted"] if is_deleted else (st["ok"] if is_ok else st["fail"]),
                )

                enc = evt.get("encrypted")
                enc_str = L["yes"] if enc is True else (L["no"] if enc is False else "–")

                vs = evt.get("verify_state")
                verify_str = L["ok"] if vs == "ok" else (L["error"] if vs == "failed" else "–")
                verify_style = st["ok"] if vs == "ok" else (st["fail"] if vs == "failed" else st["td"])

                row = [
                    Paragraph(vm_name if first else "", st["td"]),
                    Paragraph(node_name if first else "", st["td"]),
                    Paragraph(fmt_dt(evt["started_at"], dt_fmt), st["td"]),
                    Paragraph(_fmt_bytes(evt.get("size_bytes"), lang), st["td"]),
                    Paragraph(enc_str, st["td"]),
                    Paragraph(verify_str, verify_style),
                    status_para,
                ]
                table_data.append(row)
                if status_for_report == "failed":
                    row_styles.append(("BACKGROUND", (0, data_row_idx), (-1, data_row_idx), colors.HexColor("#fef2f2")))
                elif is_deleted:
                    row_styles.append(("BACKGROUND", (0, data_row_idx), (-1, data_row_idx), colors.HexColor("#f8fafc")))
                elif data_row_idx % 2 == 0:
                    row_styles.append(("BACKGROUND", (0, data_row_idx), (-1, data_row_idx), C_ROW_ALT))
                first = False
                data_row_idx += 1

        t = Table(table_data, colWidths=_COL_W, repeatRows=1)
        base_style = [
            ("BACKGROUND", (0, 0), (-1, 0), C_HEADER),
            ("TEXTCOLOR", (0, 0), (-1, 0), C_WHITE),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [C_WHITE, C_ROW_ALT]),
            ("GRID", (0, 0), (-1, -1), 0.3, C_LINE),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (2, 0), (2, -1), "CENTER"),  # date centred
            ("ALIGN", (3, 0), (3, -1), "RIGHT"),  # size right
            ("ALIGN", (4, 0), (5, -1), "CENTER"),  # Enc/Verify centred
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]
        t.setStyle(TableStyle(base_style + row_styles))
        flowables.append(t)

    return flowables


# ── Notification section for NIS2 compliance ─────────────────────────────────


def _notifications_section(notifications: list[dict], st: dict, lang: str = "de") -> list:
    """Build flowables for the notification log section in the PDF."""
    L = _labels(lang)
    flowables: list = [
        PageBreak(),
        Paragraph(L["notifications_title"], st["h1"]),
        Paragraph(
            L["notifications_subtitle"].format(count=len(notifications)),
            st["body"],
        ),
        Spacer(1, 3 * mm),
    ]

    from zoneinfo import ZoneInfo

    tz = ZoneInfo(DEFAULT_TIMEZONE)
    dt_fmt = L["fmt_datetime"]

    header = [
        Paragraph(h, st["th"])
        for h in [
            L["notif_col_date"],
            L["notif_col_type"],
            L["notif_col_cluster"],
            L["notif_col_channel"],
            L["notif_col_message"],
        ]
    ]
    table_data = [header]
    row_styles: list[tuple] = []

    # The styles dict supplied by write_backup_period_pdf does not provide a
    # "cell" key, but some callers/tests inject one. Prefer "cell" if present,
    # else fall back to the standard "td" style.
    cell_style = st.get("cell") or st["td"]

    for i, n in enumerate(notifications):
        row_idx = i + 1
        created = n["created_at"]
        if hasattr(created, "astimezone"):
            created = created.astimezone(tz)
        date_str = created.strftime(dt_fmt) if hasattr(created, "strftime") else str(created)
        type_label = _notif_type_label(L, n.get("notification_type", ""))
        cluster = n.get("cluster_name") or "–"
        channel = (n.get("channel") or "").capitalize()
        message = n.get("message", "")
        if len(message) > 120:
            message = message[:117] + "..."

        table_data.append(
            [
                Paragraph(date_str, cell_style),
                Paragraph(type_label, cell_style),
                Paragraph(cluster, cell_style),
                Paragraph(channel, cell_style),
                Paragraph(message, cell_style),
            ]
        )
        if row_idx % 2 == 0:
            row_styles.append(("BACKGROUND", (0, row_idx), (-1, row_idx), C_ROW_ALT))

    avail = PAGE_W - 2 * MARGIN
    col_widths = [avail * 0.14, avail * 0.14, avail * 0.14, avail * 0.08, avail * 0.50]
    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), C_HEADER),
                ("TEXTCOLOR", (0, 0), (-1, 0), C_WHITE),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, C_LINE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                *row_styles,
            ]
        )
    )
    flowables.append(t)
    return flowables


# ── Main entry point ─────────────────────────────────────────────────────────
def write_backup_period_pdf(
    report: dict,
    target: Path,
    notifications: list[dict] | None = None,
    lang: str = "de",
) -> None:
    """Write a professional backup period report PDF.

    lang: "de" (default) or "en" — selects the label dictionary used
    throughout the report. Anything other than "en" falls back to German.
    """
    target.parent.mkdir(parents=True, exist_ok=True)

    from zoneinfo import ZoneInfo

    tz = ZoneInfo(DEFAULT_TIMEZONE)
    L = _labels(lang)

    # Logo invers (white on dark) for the header banner on page 1
    logo_invers_bytes = _fetch_logo(BRAND_LOGO_LIGHT_URL)
    # Normal logo (dark on light) for subtle header on pages 2+
    logo_normal_bytes = _fetch_logo(_logo_url_normal())

    st = _styles()
    gen_local = report["generated_at"].astimezone(tz)
    generated_str = gen_local.strftime(L["fmt_datetime"])

    doc = SimpleDocTemplate(
        str(target),
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN + 4 * mm,  # small extra space for page header logo
        bottomMargin=22 * mm,
        # "Backup Sentinel" is the product name — intentionally not translated.
        title=f"Backup Sentinel – {L['backup_report']} {report['period_label']}",
        author="Backup Sentinel",
    )

    company = "Backup Sentinel"  # proper noun, intentionally kept
    story = [
        HeaderBanner(company, report["period_label"], logo_invers_bytes, banner_label=L["header_banner_label"]),
        Spacer(1, 6 * mm),
        Paragraph(L["backup_report"], st["h1"]),
        Paragraph(f"{L['period']}: {report['period_label']}", st["body"]),
        Spacer(1, 4 * mm),
        _summary_table(report["totals"], st, L, lang),
        Spacer(1, 4 * mm),
    ]

    # Feature 8: Month-over-month comparison
    comparison = report.get("comparison")
    if comparison and comparison.get("has_previous"):
        story += _comparison_section(comparison, st, L, lang)
        story.append(Spacer(1, 4 * mm))

    story += _events_table(report["rows"], st, L, lang, tz_name=DEFAULT_TIMEZONE)

    if notifications:
        story += _notifications_section(notifications, st, lang=lang)

    from app.config import APP_VERSION

    canvas_factory = _NumberedCanvas(generated_str, logo_normal_bytes, APP_VERSION, labels=L)
    doc.build(story, canvasmaker=canvas_factory)


# Keep backward compat for any code that still calls the old name
def write_monthly_report_pdf(report: dict, target: Path) -> None:
    write_backup_period_pdf(report, target)

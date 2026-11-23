"""Generate the NIS2 report (overall + one PDF per cluster + JSON) and archive
it — shared by the button on /reports (interim) and the monthly run."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app import db
from app.config import DEFAULT_TIMEZONE, REPORT_DIR, REPORT_LANGUAGE
from app.services.nis2_render import render_html, write_pdf
from app.services.nis2_report import build_nis2_report, cluster_view
from app.services.reporting import serialize_report_payload
from app.services.theme_store import active_theme

logger = logging.getLogger(__name__)

# Layout version of the archived PDFs. Raise it when a change must reach the
# archive too: rebuild_report_archive() then renders every month anew, once.
REPORT_FORMAT = 3
_FORMAT_SETTING = "report_archive_format"


def file_sha256(path: str | Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _paths(row: dict[str, Any]) -> dict[str, str]:
    """The archived files of a report by key: "all" / a cluster slug / "json"."""
    files = dict(row.get("files") or {"all": row["pdf_path"]})
    files["json"] = row["json_path"]
    return files


def file_intact(row: dict[str, Any], key: str) -> bool | None:
    """Whether the archived file `key` still has the hash recorded when it was
    written. None: no hash recorded (should not happen after the start-up seal)."""
    recorded = (row.get("sha256") or {}).get(key)
    path = _paths(row).get(key)
    if not recorded or not path:
        return None
    try:
        return file_sha256(path) == recorded
    except OSError:
        return False


def report_intact(row: dict[str, Any]) -> bool:
    """False as soon as one archived file is missing or was changed."""
    return all(file_intact(row, key) is not False for key in _paths(row))


def seal_archived_reports() -> int:
    """Record the hashes of reports archived before hashes existed. Returns how many."""
    sealed = 0
    for row in db.list_monthly_reports():
        if row.get("sha256"):
            continue
        hashes = {}
        for key, path in _paths(row).items():
            try:
                hashes[key] = file_sha256(path)
            except OSError:
                continue  # file already gone: nothing to vouch for
        if hashes:
            db.set_monthly_report_hashes(row["id"], hashes)
            _audit("system", "report.sealed", row, f"sha256={hashes.get('all', '')}")
            sealed += 1
    return sealed


def _audit(actor: str, event: str, row: dict[str, Any], details: str) -> None:
    month = row["report_month"]
    target = f"{month:%Y-%m}/{row.get('trigger_mode', '')}"
    try:
        db.append_audit_event(actor, event, target, details)
    except Exception as exc:  # the audit entry must not break the report itself
        logger.error("Audit entry %s for %s failed: %s", event, target, exc)


def delete_archived_report(report_id: int, actor: str, reason: str = "") -> bool:
    """Delete a report and its files, leaving an audit entry with its hash."""
    row = db.delete_monthly_report(report_id)
    if not row:
        return False
    remove_archived_files(row)
    details = f"generated_at={row['generated_at'].isoformat()}, sha256={(row.get('sha256') or {}).get('all', '')}"
    _audit(actor, "report.deleted", row, details + (f", reason={reason}" if reason else ""))
    return True


def generate_archived_report(
    month: date, trigger_mode: str, now: datetime | None = None, actor: str = "system"
) -> Path:
    """Report for `month` (interim while it runs); returns the overall PDF's path."""
    now = now or datetime.now(UTC)
    report = build_nis2_report(month, trigger=trigger_mode, lang=REPORT_LANGUAGE, now=now)
    try:
        theme_colors = active_theme()[1]
    except Exception as exc:  # no settings table yet, DB hiccup: default colours
        logger.warning("Report theme unavailable: %s", exc)
        theme_colors = None

    stem = f"nis2-report-{month:%Y-%m}-{now:%Y%m%d-%H%M%S}"
    pdf_path = REPORT_DIR / f"{stem}.pdf"
    write_pdf(render_html(report, theme_colors=theme_colors), pdf_path)
    files = {"all": str(pdf_path)}
    for cluster in report["clusters"]:
        view = cluster_view(report, cluster["slug"])
        target = REPORT_DIR / f"{stem}-{cluster['slug']}.pdf"
        write_pdf(render_html(view, theme_colors=theme_colors), target)
        files[cluster["slug"]] = str(target)

    json_path = REPORT_DIR / f"{stem}.json"
    payload = serialize_report_payload(report)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    hashes = {key: file_sha256(path) for key, path in {**files, "json": str(json_path)}.items()}
    db.archive_monthly_report(month, trigger_mode, pdf_path, json_path, payload, files, REPORT_FORMAT, hashes)
    _audit(
        actor,
        "report.generated",
        {"report_month": month, "trigger_mode": trigger_mode},
        f"sha256={hashes['all']}",
    )
    return pdf_path


def remove_archived_files(row: dict[str, Any]) -> None:
    """Delete the files of an archived report (the DB row is deleted separately)."""
    for path in {row["pdf_path"], row["json_path"], *(row.get("files") or {}).values()}:
        Path(path).unlink(missing_ok=True)


def request_archive_rebuild() -> None:
    """Rebuild the archive with the next sync — the evidence start of a cluster
    changed, so every monthly report must be measured anew."""
    db.save_settings({_FORMAT_SETTING: "0"})


def rebuild_report_archive(now: datetime | None = None) -> int:
    """Once per REPORT_FORMAT (or after request_archive_rebuild): replace every
    monthly report and every report of an older layout, reconstructing the
    monthly report of each finished month since recording began. Interim
    reports of an older layout are dropped — the month's report replaces them.
    Returns how many reports were written."""
    settings = db.get_settings_dict(_FORMAT_SETTING)
    if int(settings.get(_FORMAT_SETTING) or 0) >= REPORT_FORMAT:
        return 0
    now = now or datetime.now(UTC)
    for row in db.list_monthly_reports():
        if row["trigger_mode"] == "auto" or (row.get("format") or 1) < REPORT_FORMAT:
            delete_archived_report(row["id"], "system", reason="archive rebuild")
    written = 0
    starts = db.nis2_recording_start_by_cluster().values()
    if starts:
        tz = ZoneInfo(DEFAULT_TIMEZONE)
        month = min(starts).astimezone(tz).date().replace(day=1)
        current = now.astimezone(tz).date().replace(day=1)
        while month < current:
            if not db.get_monthly_report_for_month(month):
                generate_archived_report(month, "auto", now)
                written += 1
            month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    db.save_settings({_FORMAT_SETTING: str(REPORT_FORMAT)})
    logger.info("Report archive rebuilt for layout %d: %d monthly reports", REPORT_FORMAT, written)
    return written

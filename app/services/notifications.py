"""Gotify push notifications and email sending for alerts and reports.

Features:
- Enable/disable toggles for Gotify and E-Mail (default: off)
- Quiet hours: messages queued during quiet period, sent after
- Gotify batching: alerts queued for 5 min cooldown, then sent as one combined message
- 24h dedup per alert key
"""
from __future__ import annotations

import base64
import json
import logging
import smtplib
import ssl
import threading
import time
from datetime import datetime, timedelta
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Any
from urllib import error, request
from zoneinfo import ZoneInfo

from app import db
from app.services.crypto import decrypt

logger = logging.getLogger(__name__)

# ── Dedup: same alert at most once per 24h ────────────────────────────────
_sent_alerts: dict[str, float] = {}   # key → unix timestamp (time.time)
_DEDUP_SECONDS = 86400  # 24 hours


def _should_send(dedup_key: str) -> bool:
    """Return True if this alert hasn't been sent in the last 24h."""
    now = time.time()
    last = _sent_alerts.get(dedup_key)
    if last is not None and (now - last) < _DEDUP_SECONDS:
        return False
    _sent_alerts[dedup_key] = now
    # Housekeeping: purge old entries
    cutoff = now - _DEDUP_SECONDS
    stale = [k for k, v in _sent_alerts.items() if v < cutoff]
    for k in stale:
        del _sent_alerts[k]
    return True


# ── Enable/disable + quiet hours helpers ──────────────────────────────────

def _notify_settings() -> dict[str, str]:
    return db.get_settings_dict("notify_")


def _is_enabled(channel: str) -> bool:
    """Check if a notification channel is enabled (gotify/email)."""
    cfg = _notify_settings()
    return cfg.get(f"notify_{channel}_enabled", "false").lower() in ("1", "true", "on", "yes")


def _quiet_hours() -> tuple[str, str]:
    """Return (start, end) quiet hours, e.g. ('22:00', '07:00'). Empty = disabled."""
    cfg = _notify_settings()
    return cfg.get("notify_quiet_start", ""), cfg.get("notify_quiet_end", "")


def _in_quiet_hours() -> bool:
    """Check if we're currently in quiet hours (only when quiet hours are enabled)."""
    cfg = _notify_settings()
    if cfg.get("notify_quiet_enabled", "false").lower() not in ("1", "true", "on", "yes"):
        return False
    start_s, end_s = _quiet_hours()
    if not start_s or not end_s:
        return False
    try:
        from app.config import DEFAULT_TIMEZONE
        tz = ZoneInfo(DEFAULT_TIMEZONE)
        now = datetime.now(tz).time()
        start = datetime.strptime(start_s, "%H:%M").time()
        end = datetime.strptime(end_s, "%H:%M").time()
        if start <= end:
            # e.g. 08:00 – 18:00 (quiet during day — unusual but supported)
            return start <= now <= end
        else:
            # e.g. 22:00 – 07:00 (quiet overnight — typical)
            return now >= start or now <= end
    except Exception:
        return False


# ── Gotify Queue & Batching ──────────────────────────────────────────────
# Messages are collected in _gotify_queue. A background timer waits 5 min
# after the last addition. If nothing new comes in, all queued messages are
# combined into one Gotify notification. During quiet hours, messages stay
# queued until quiet hours end.

_gotify_queue: list[dict[str, Any]] = []   # [{title, message, priority}]
_gotify_lock = threading.Lock()
_gotify_timer: threading.Timer | None = None
_BATCH_COOLDOWN = 300  # 5 minutes


def _schedule_gotify_flush() -> None:
    """(Re-)schedule the flush timer. Must be called with _gotify_lock held."""
    global _gotify_timer
    if _gotify_timer is not None:
        _gotify_timer.cancel()
    _gotify_timer = threading.Timer(_BATCH_COOLDOWN, _flush_gotify_queue)
    _gotify_timer.daemon = True
    _gotify_timer.start()


def _flush_gotify_queue() -> None:
    """Flush queued Gotify messages. If in quiet hours, reschedule for later."""
    global _gotify_timer
    with _gotify_lock:
        _gotify_timer = None
        if not _gotify_queue:
            return
        if _in_quiet_hours():
            # Retry in 10 minutes
            _gotify_timer = threading.Timer(600, _flush_gotify_queue)
            _gotify_timer.daemon = True
            _gotify_timer.start()
            logger.debug("Gotify flush deferred (quiet hours), %d messages queued", len(_gotify_queue))
            return
        msgs = list(_gotify_queue)
        _gotify_queue.clear()

    # Build combined message
    if len(msgs) == 1:
        title = msgs[0]["title"]
        body = msgs[0]["message"]
        prio = msgs[0]["priority"]
    else:
        prio = max(m["priority"] for m in msgs)
        title = f"Backup Sentinel: {len(msgs)} Meldungen"
        parts = []
        for m in msgs:
            parts.append(f"── {m['title']} ──\n{m['message']}")
        body = "\n\n".join(parts)

    if _send_gotify_raw(title, body, prio):
        try:
            from app.db_notifications import insert_notification_log
            for m in msgs:
                if m.get("notification_type"):
                    insert_notification_log(
                        notification_type=m["notification_type"],
                        title=m["title"],
                        message=m["message"],
                        channel="gotify",
                        cluster_name=m.get("cluster_name"),
                    )
        except Exception as exc:
            logger.warning("Notification log insert failed: %s", exc)


def _send_gotify_raw(title: str, message: str, priority: int) -> bool:
    """Actually send one Gotify HTTP request."""
    cfg = db.get_settings_dict("gotify_")
    url = cfg.get("gotify_url", "").rstrip("/")
    token = decrypt(cfg.get("gotify_token", ""))
    if not url or not token:
        return False

    payload = json.dumps({
        "title": title,
        "message": message,
        "priority": priority,
    }).encode()

    req = request.Request(
        f"{url}/message?token={token}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10):
            pass
        logger.info("Gotify notification sent: %s", title[:80])
        return True
    except (error.URLError, error.HTTPError, OSError) as exc:
        logger.warning("Gotify notification failed: %s", exc)
        return False


def send_gotify(title: str, message: str, priority: int = 5, *,
                notification_type: str = "", cluster_name: str | None = None) -> bool:
    """Queue a Gotify notification for batched delivery.

    Messages are held for 5 min. If no new messages arrive, all queued
    messages are combined and sent as one. During quiet hours, delivery
    is delayed until quiet hours end.
    """
    if not _is_enabled("gotify"):
        logger.debug("Gotify disabled, dropping: %s", title)
        return False

    with _gotify_lock:
        _gotify_queue.append({
            "title": title, "message": message, "priority": priority,
            "notification_type": notification_type, "cluster_name": cluster_name,
        })
        _schedule_gotify_flush()
    return True


def test_gotify_notification() -> tuple[bool, str]:
    """Send a test notification directly (bypasses queue + enable check)."""
    ok = _send_gotify_raw("Test", "Backup Sentinel: Gotify-Verbindung erfolgreich!", 3)
    if ok:
        return True, "Gotify-Nachricht erfolgreich gesendet."
    return False, "Gotify-Nachricht konnte nicht gesendet werden. URL, Token und Erreichbarkeit prüfen."


# ── Alert helpers (all go through send_gotify → queue) ────────────────────

def notify_backup_critical(cluster_name: str, vm_name: str, vmid: int, age_days: int | None) -> None:
    key = f"backup-critical:{cluster_name}:{vmid}"
    if not _should_send(key):
        return
    age_str = f"seit {age_days} Tagen" if age_days is not None else "nie"
    send_gotify(
        title=f"Backup kritisch: {vm_name}",
        message=f"Cluster: {cluster_name}\nVM: {vm_name} (VMID {vmid})\nLetztes Backup: {age_str}",
        priority=8,
        notification_type="backup_critical", cluster_name=cluster_name,
    )


def notify_size_anomaly(cluster_name: str, vm_name: str, vmid: int,
                        latest_size: int, avg_size: float, deviation: float) -> None:
    key = f"size-anomaly:{cluster_name}:{vmid}"
    if not _should_send(key):
        return
    direction = "größer" if latest_size > avg_size else "kleiner"
    send_gotify(
        title=f"Backup-Größe Anomalie: {vm_name}",
        message=(
            f"Cluster: {cluster_name}\n"
            f"VM: {vm_name} (VMID {vmid})\n"
            f"Aktuell: {latest_size / 1_073_741_824:.1f} GB\n"
            f"Durchschnitt: {avg_size / 1_073_741_824:.1f} GB\n"
            f"{deviation * 100:.0f}% {direction} als üblich"
        ),
        priority=5,
        notification_type="size_anomaly", cluster_name=cluster_name,
    )


def notify_sync_failed(cluster_name: str, detail: str) -> None:
    key = f"sync-failed:{cluster_name}"
    if not _should_send(key):
        return
    send_gotify(
        title=f"Sync fehlgeschlagen: {cluster_name}",
        message=f"Cluster: {cluster_name}\n{detail[:500]}",
        priority=7,
        notification_type="sync_failed", cluster_name=cluster_name,
    )


def notify_sync_overdue(cluster_name: str, failure_started_at: object) -> None:
    started = str(failure_started_at)
    title = f"Cluster-Sync seit 24h fehlgeschlagen: {cluster_name}"
    message = (
        f"Cluster: {cluster_name}\n"
        f"Fehlerzustand seit: {started}\n"
        "Seit mindestens 24 Stunden war kein erfolgreicher Cluster-Sync mehr möglich."
    )
    send_gotify(title=title, message=message, priority=9,
                notification_type="sync_overdue", cluster_name=cluster_name)
    if send_email(
        subject=title,
        body=(
            f"Der Cluster-Sync für '{cluster_name}' ist seit mindestens 24 Stunden im Fehlerzustand.\n\n"
            f"Fehlerzustand seit: {started}\n"
            "Bitte PVE/PBS-Erreichbarkeit, Credentials und API-Fehler prüfen."
        ),
    ):
        try:
            from app.db_notifications import insert_notification_log
            insert_notification_log("sync_overdue", title, message, "email", cluster_name)
        except Exception:
            pass


def notify_restore_overdue(cluster_name: str, vm_name: str, vmid: int, days_overdue: int) -> None:
    key = f"restore-overdue:{cluster_name}:{vmid}"
    if not _should_send(key):
        return
    send_gotify(
        title=f"Restore-Test überfällig: {vm_name}",
        message=f"Cluster: {cluster_name}\nVM: {vm_name} (VMID {vmid})\n{days_overdue} Tage überfällig",
        priority=6,
        notification_type="restore_overdue", cluster_name=cluster_name,
    )


def notify_unencrypted_backups(cluster_name: str, unencrypted_count: int, vm_names: list[str]) -> None:
    key = f"unencrypted:{cluster_name}"
    if not _should_send(key):
        return
    vm_list = ", ".join(vm_names[:10])
    if len(vm_names) > 10:
        vm_list += f" (+{len(vm_names) - 10} weitere)"
    send_gotify(
        title=f"Unverschlüsselte Backups: {cluster_name}",
        message=(
            f"Cluster: {cluster_name}\n"
            f"{unencrypted_count} VMs mit unverschlüsselten Backups:\n"
            f"{vm_list}"
        ),
        priority=6,
        notification_type="unencrypted_backups", cluster_name=cluster_name,
    )


# ── E-Mail ────────────────────────────────────────────────────────────────

def _email_config() -> dict[str, str]:
    return db.get_settings_dict("email_")


def _encode_address(addr: str, display_name: str | None = None) -> Address | str:
    """Build a structured address so non-ASCII display names are encoded safely."""
    parsed_name, email = parseaddr(addr)
    name = display_name if display_name is not None else parsed_name
    if not email:
        return addr
    username, at, domain = email.rpartition("@")
    if not at:
        return addr
    return Address(display_name=name, username=username, domain=domain)


def _smtp_auth_plain_utf8(smtp: smtplib.SMTP, user: str, password: str) -> tuple[int, bytes]:
    payload = f"\0{user}\0{password}".encode("utf-8")
    encoded = base64.b64encode(payload)
    smtp.send(b"AUTH PLAIN " + encoded + b"\r\n")
    return smtp.getreply()


def _smtp_auth_login_utf8(smtp: smtplib.SMTP, user: str, password: str) -> tuple[int, bytes]:
    encoded_user = base64.b64encode(user.encode("utf-8"))
    smtp.send(b"AUTH LOGIN " + encoded_user + b"\r\n")
    code, resp = smtp.getreply()
    if code != 334:
        return code, resp

    encoded_password = base64.b64encode(password.encode("utf-8"))
    smtp.send(encoded_password + b"\r\n")
    return smtp.getreply()


def _smtp_login(smtp: smtplib.SMTP, user: str, password: str) -> None:
    try:
        smtp.login(user, password)
        return
    except UnicodeEncodeError:
        logger.info("SMTP-Login enthält Nicht-ASCII-Zeichen, verwende UTF-8-AUTH-Fallback")

    smtp.ehlo_or_helo_if_needed()
    authlist = smtp.esmtp_features.get("auth", "").upper().split()

    if "PLAIN" in authlist:
        code, resp = _smtp_auth_plain_utf8(smtp, user, password)
    elif "LOGIN" in authlist:
        code, resp = _smtp_auth_login_utf8(smtp, user, password)
    else:
        raise smtplib.SMTPException("No suitable SMTP AUTH mechanism found for UTF-8 credentials.")

    if code not in (235, 503):
        raise smtplib.SMTPAuthenticationError(code, resp)


def send_email(subject: str, body: str, attachments: list[Path] | None = None) -> bool:
    """Send an email with optional attachments. Returns True on success.

    Respects the email_enabled toggle. Test emails bypass this check.
    """
    if not _is_enabled("email"):
        logger.debug("E-Mail disabled, dropping: %s", subject)
        return False

    return _send_email_raw(subject, body, attachments)


def _send_email_raw(subject: str, body: str, attachments: list[Path] | None = None) -> bool:
    """Actually send an email (no enable check)."""
    cfg = _email_config()
    host = cfg.get("email_host", "")
    recipients = cfg.get("email_recipients", "")
    sender = cfg.get("email_from", "")
    if not host or not recipients or not sender:
        logger.info("E-Mail nicht konfiguriert, überspringe Versand")
        return False

    port = int(cfg.get("email_port", "465"))
    user = cfg.get("email_user", "")
    password = decrypt(cfg.get("email_password", ""))
    tls_mode = cfg.get("email_tls", "ssl").lower()
    parsed_recipients = [
        _encode_address(addr, name)
        for name, addr in getaddresses([recipients])
        if addr.strip()
    ]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = _encode_address(sender)
    msg["To"] = tuple(parsed_recipients) if parsed_recipients else recipients
    msg.set_content(body, charset="utf-8")

    if attachments:
        for path in attachments:
            if path.exists():
                with open(path, "rb") as f:
                    data = f.read()
                maintype = "application"
                subtype = path.suffix.lstrip(".") or "octet-stream"
                if subtype == "pdf":
                    maintype = "application"
                msg.add_attachment(data, maintype=maintype, subtype=subtype,
                                   filename=path.name)

    try:
        context = ssl.create_default_context()
        if tls_mode == "ssl":
            with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as smtp:
                if user and password:
                    _smtp_login(smtp, user, password)
                smtp.send_message(msg)
        elif tls_mode == "starttls":
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.starttls(context=context)
                if user and password:
                    _smtp_login(smtp, user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                if user and password:
                    _smtp_login(smtp, user, password)
                smtp.send_message(msg)
        logger.info("E-Mail gesendet an %s: %s", recipients, subject)
        return True
    except Exception as exc:
        logger.error("E-Mail-Versand fehlgeschlagen: %s", exc)
        return False


def test_email_delivery() -> tuple[bool, str]:
    """Send a test email directly (bypasses enable check)."""
    ok = _send_email_raw("Test – Backup Sentinel", "Dies ist eine Test-E-Mail vom Backup Sentinel.")
    if ok:
        return True, "Test-E-Mail wurde erfolgreich versendet."
    return False, "Test-E-Mail konnte nicht gesendet werden. SMTP, Zugangsdaten und Empfänger prüfen."


def send_monthly_report_email(pdf_path: Path, month_label: str) -> bool:
    """Send the monthly backup report PDF via email."""
    return send_email(
        subject=f"Backup Sentinel – Report {month_label}",
        body=(
            f"Im Anhang der automatisch generierte Backup-Report für {month_label}.\n\n"
            "Dieser Report wurde automatisch vom Backup Sentinel erstellt."
        ),
        attachments=[pdf_path],
    )

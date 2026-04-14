"""Shared TypedDict definitions for database row types."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, TypedDict


class ClusterRow(TypedDict):
    id: int
    name: str
    slug: str
    api_url: str
    enrollment_secret: str
    api_user: str | None
    token_id: str | None
    token_secret: str | None
    fingerprint: str | None
    registered_at: datetime | None
    created_at: datetime
    last_pve_sync_at: datetime | None
    last_pve_sync_ok: bool | None
    last_pve_sync_log: str | None
    last_pve_sync_success_at: datetime | None
    last_pve_sync_failure_started_at: datetime | None
    last_pve_sync_failure_notified_at: datetime | None
    consecutive_sync_failures: int


class NodeRow(TypedDict):
    id: int
    cluster_id: int
    name: str
    status: str


class VMRow(TypedDict):
    id: int
    cluster_id: int
    node_name: str
    vmid: int
    name: str
    backup_kind: str
    last_backup_at: datetime | None


class PBSConnectionRow(TypedDict):
    id: int
    cluster_id: int
    name: str
    api_url: str
    api_token_id: str
    api_token_secret: str
    fingerprint: str
    last_sync_at: datetime | None
    last_sync_ok: bool | None
    last_sync_log: str | None
    created_at: datetime


class BackupEventRow(TypedDict):
    id: int
    vm_id: int
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: int | None
    size_bytes: int | None
    encrypted: bool | None
    verify_state: str | None
    status: str
    upid: str | None
    source: str | None
    removed_at: date | None


class RestoreTestRow(TypedDict):
    id: int
    vm_id: int
    tested_at: date
    result: str
    recovery_type: str
    duration_minutes: int
    evidence_note: str
    created_at: datetime


class MonthlyReportRow(TypedDict):
    id: int
    report_month: date
    generated_at: datetime
    trigger_mode: str
    pdf_path: str
    json_path: str
    snapshot_json: dict[str, Any]


class AuditLogRow(TypedDict):
    actor: str
    key: str
    old_value: str
    new_value: str
    created_at: datetime


class SyncHealthRow(TypedDict):
    failures: int
    stale: bool


class NotificationLogRow(TypedDict):
    id: int
    notification_type: str
    title: str
    message: str
    cluster_name: str | None
    channel: str
    created_at: datetime

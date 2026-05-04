from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

BackupKind = Literal["daily", "weekly", "none"]
Severity = Literal["ok", "warning", "critical", "unknown"]
Criticality = Literal["low", "medium", "high", "critical"]
RestoreResult = Literal["passed", "failed", "limited"]


class ClusterCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    api_url: str = Field(min_length=8, max_length=255)


class BootstrapPayload(BaseModel):
    cluster_slug: str = Field(min_length=2, max_length=80)
    server_url: str
    enrollment_secret: str
    script_url: str
    commands: list[str]


class BootstrapFinalize(BaseModel):
    cluster_slug: str
    enrollment_secret: str
    proxmox_node: str
    api_user: str
    token_id: str
    token_secret: str
    fingerprint: str = ""


class VmStatus(BaseModel):
    vm_name: str
    vmid: int
    node_name: str
    backup_kind: BackupKind
    last_backup_at: datetime | None
    severity: Severity
    age_days: int | None


class ClusterSummary(BaseModel):
    cluster_name: str
    cluster_slug: str
    api_url: str
    registered_at: datetime | None
    node_count: int
    vm_count: int
    warning_count: int
    critical_count: int


class RestoreTestCreate(BaseModel):
    vm_id: int
    tested_at: date
    result: RestoreResult
    recovery_type: str = Field(min_length=3, max_length=80)
    duration_minutes: int = Field(ge=0, le=10080)
    evidence_note: str = Field(default="", max_length=50000)

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

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


class RestoreTestCreate(BaseModel):
    vm_id: int
    tested_at: date
    result: RestoreResult
    recovery_type: str = Field(min_length=3, max_length=80)
    duration_minutes: int = Field(ge=0, le=10080)
    evidence_note: str = Field(default="", max_length=50000)

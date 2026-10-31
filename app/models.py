from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

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


DISK_CLASSES = ("enterprise-ssd", "standard-ssd", "sas-hdd", "sata-hdd")
RESTORE_CADENCES = ("month", "quarter", "half_year", "year")


class TargetStorage(BaseModel):
    storage: str = Field(min_length=1, max_length=120)
    type: str = Field(default="", max_length=40)  # PVE storage type, display-only
    disk_class: str = Field(default="standard-ssd")
    # The operator assigns a disk class to every image-capable storage; `use`
    # marks the subset selected as restore targets (order is the fill order).
    use: bool = Field(default=True)
    order: int = Field(default=0, ge=0)

    @field_validator("disk_class")
    @classmethod
    def _known_class(cls, v: str) -> str:
        if v not in DISK_CLASSES:
            raise ValueError(f"disk_class must be one of {DISK_CLASSES}")
        return v


class RestoreServerPayload(BaseModel):
    """Validated restore-server (PVE-Restore) configuration.

    Secrets (``token_secret``, ``ssh_private_key``) are encrypted at rest by
    ``app.services.restore_config``; this model only validates shape/ranges.
    """

    # API access is not entered by hand: the app derives api_url from the SSH
    # host and provisions the token over SSH. These are filled by the save
    # handler, so they are optional here.
    name: str = Field(default="", max_length=120)
    api_url: str = Field(default="", max_length=255)
    api_user: str = Field(default="root@pam", max_length=120)
    token_id: str = Field(default="", max_length=120)
    token_secret: str = Field(default="", max_length=400)
    fingerprint: str = Field(default="", max_length=200)

    ssh_host: str = Field(min_length=1, max_length=255)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(default="root", min_length=1, max_length=64)
    ssh_private_key: str = Field(default="", max_length=20000)
    ssh_host_key: str = Field(default="", max_length=4000)

    @field_validator(
        "api_url",
        "api_user",
        "token_id",
        "token_secret",
        "fingerprint",
        "ssh_host",
        "ssh_user",
        "ssh_host_key",
        "mgmt_iface",
        "mgmt_gateway",
        "mgmt_netmask",
        "mgmt_ip_from",
        "mgmt_ip_to",
        "sandbox_bridge",
        mode="before",
    )
    @classmethod
    def _strip(cls, v: object) -> object:
        # Stray whitespace/newlines (common when pasting a token secret) make a
        # PVE token header '...=SECRET\\n' → HTTP 401. Trim credential/text fields.
        # ssh_private_key is deliberately NOT stripped (its newlines matter).
        return v.strip() if isinstance(v, str) else v

    target_storages: list[TargetStorage] = Field(default_factory=list)

    sandbox_bridge: str = Field(default="vmbr9", min_length=1, max_length=15)
    sandbox_vlan_from: int = Field(default=900, ge=1, le=4094)
    sandbox_vlan_to: int = Field(default=999, ge=1, le=4094)

    mgmt_iface: str = Field(min_length=1, max_length=15)
    mgmt_vlan: int | None = Field(default=None, ge=1, le=4094)
    # The sandbox never routes outside, so gateway is unused and the mask is
    # always /24; both are set by the handler, not entered in the form.
    mgmt_gateway: str = Field(default="", max_length=45)
    mgmt_netmask: str = Field(default="255.255.255.0", max_length=45)
    mgmt_ip_from: str = Field(min_length=7, max_length=45)
    mgmt_ip_to: str = Field(min_length=7, max_length=45)

    restore_vmid_from: int = Field(default=9000, ge=100, le=999999999)
    restore_vmid_to: int = Field(default=9100, ge=100, le=999999999)
    probe_offset: int = Field(default=254, ge=1, le=254)

    probe_window_seconds: int = Field(default=300, ge=30, le=86400)
    probe_first_delay_seconds: int = Field(default=30, ge=0, le=3600)
    probe_interval_seconds: int = Field(default=30, ge=5, le=3600)
    stall_backstop_factor: float = Field(default=6.0, ge=1.0, le=100.0)
    hard_cap_seconds: int | None = Field(default=None, ge=60)

    @model_validator(mode="after")
    def _check_ranges(self) -> RestoreServerPayload:
        import ipaddress

        if self.sandbox_vlan_to < self.sandbox_vlan_from:
            raise ValueError("sandbox VLAN range: to < from")
        if self.restore_vmid_to < self.restore_vmid_from:
            raise ValueError("restore VMID range: to < from")
        try:
            lo = int(ipaddress.IPv4Address(self.mgmt_ip_from))
            hi = int(ipaddress.IPv4Address(self.mgmt_ip_to))
        except ipaddress.AddressValueError as exc:
            raise ValueError(f"invalid management IP: {exc}") from exc
        if hi < lo:
            raise ValueError("management IP range: to < from")
        return self

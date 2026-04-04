from __future__ import annotations

import secrets
from dataclasses import dataclass

from app.config import APP_BASE_URL


def slugify_cluster(name: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in name.strip())
    collapsed = "-".join(part for part in cleaned.split("-") if part)
    return collapsed[:80] or secrets.token_hex(4)


@dataclass(slots=True)
class EnrollmentBundle:
    cluster_slug: str
    enrollment_secret: str
    script_url: str
    server_url: str
    commands: list[str]


def build_enrollment_bundle(cluster_slug: str) -> EnrollmentBundle:
    enrollment_secret = secrets.token_urlsafe(24)
    server_url = APP_BASE_URL.rstrip("/")
    script_url = f"{server_url}/bootstrap/proxmox-agent.sh"
    commands = [
        f"curl -fsSL {script_url} -o /root/proxmox-agent.sh",
        "chmod 700 /root/proxmox-agent.sh",
        (
            f"/root/proxmox-agent.sh "
            f"--server-url {server_url} "
            f"--cluster-slug {cluster_slug} "
            f"--enrollment-secret '{enrollment_secret}'"
        ),
    ]
    return EnrollmentBundle(
        cluster_slug=cluster_slug,
        enrollment_secret=enrollment_secret,
        script_url=script_url,
        server_url=server_url,
        commands=commands,
    )

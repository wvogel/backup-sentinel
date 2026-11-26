"""Running garbage collections and verifications, as the pages show them:
which kinds run on a PBS and on each datastore, how far, and the tooltip.
Built from the cached state only (`pbs_activity.tasks`): no PBS is asked here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.i18n import t
from app.services import pbs_activity
from app.services.backup_status import format_duration
from app.services.human_format import format_count
from app.web.common import APP_TIMEZONE, templates

# Below this the share done says too little to extrapolate from.
_ETA_MIN_PERCENT = 3.0
_ETA_MIN_SECONDS = 120


def _when(moment: datetime, now: datetime) -> str:
    """Time of day; with the date when it is not today."""
    local, today = moment.astimezone(APP_TIMEZONE), now.astimezone(APP_TIMEZONE).date()
    return local.strftime("%H:%M" if local.date() == today else "%d.%m. %H:%M")


def _what(task: dict[str, Any]) -> str:
    if task["kind"] == "gc":
        return t("activity.gc")
    if task["worker_type"] == "verificationjob":
        return t("activity.verify_job", job=task["target"])
    return t("activity.verify_target", target=task["target"]) if task["target"] else t("activity.verify")


def verify_eta(started: datetime | None, percent: float | None, now: datetime) -> datetime | None:
    """When a verification would end at the pace so far. Rough by nature:
    PBS counts groups, not data, and skips snapshots verified recently."""
    if started is None or percent is None or not _ETA_MIN_PERCENT <= percent < 100:
        return None
    elapsed = (now - started).total_seconds()
    if elapsed < _ETA_MIN_SECONDS:
        return None
    return started + timedelta(seconds=elapsed * 100 / percent)


def _percent(progress: dict[str, Any]) -> int | None:
    """Whole and cut off, not rounded: 100 only when it is done."""
    return int(progress["percent"]) if progress.get("percent") is not None else None


def task_lines(task: dict[str, Any], now: datetime, with_store: bool) -> list[str]:
    progress, started = task.get("progress") or {}, task.get("started")
    head = _what(task) + (f" · {task['store']}" if with_store else "")
    if started:
        duration = format_duration(max(0, int((now - started).total_seconds())))
        head += " · " + t("activity.since", when=_when(started, now), duration=duration)
    details = []
    percent = _percent(progress)
    if task["kind"] == "verify":
        if percent is not None:
            key = "activity.verify_snapshots" if progress.get("unit") == "snapshots" else "activity.verify_groups"
            details.append(t(key, pct=percent, done=progress["done"], total=progress["total"]))
        if progress.get("group"):
            details.append(t("activity.current", group=progress["group"]))
        eta = verify_eta(started, progress.get("percent"), now)
        if eta:
            details.append(t("activity.eta", when=_when(eta, now)))
    else:
        phase = progress.get("phase")
        label = t(f"activity.gc_phase_{phase}") if phase in (1, 2, 3) else ""
        if percent is not None and "total" in progress:
            figures = t(
                "activity.gc_marked",
                pct=percent,
                done=format_count(progress["done"]),
                total=format_count(progress["total"]),
            )
        elif percent is not None:
            figures = t("activity.gc_processed", pct=percent, chunks=format_count(progress.get("chunks", 0)))
        else:
            figures = ""
        if label or figures:
            details.append(" · ".join(part for part in (label, figures) if part))
    if not details:
        details.append(t("activity.no_progress"))
    if task.get("last_seconds"):
        details.append(t("activity.last_duration", duration=format_duration(task["last_seconds"])))
    return [head, *(f" • {line}" for line in details)]


def view(pbs_id: int | None, now: datetime | None = None) -> dict[str, Any] | None:
    """{"kinds": ["gc", "verify"], "tooltip", "stores": {store: {"kinds":
    {"verify": 42, "gc": None}, "tooltip"}}} for a PBS with something running,
    else None. A kind's value is its percentage (None: not known yet); of
    several tasks of one kind on a datastore, the first that has one."""
    tasks = pbs_activity.tasks(pbs_id) if pbs_id is not None else []
    if not tasks:
        return None
    now = now or datetime.now(UTC)
    stores: dict[str, dict[str, Any]] = {}
    lines: list[str] = []
    for task in tasks:
        lines += task_lines(task, now, with_store=True)
        store = stores.setdefault(task["store"], {"kinds": {}, "lines": []})
        store["lines"] += task_lines(task, now, with_store=False)
        percent = _percent(task.get("progress") or {})
        if store["kinds"].get(task["kind"]) is None:
            store["kinds"][task["kind"]] = percent
    return {
        "kinds": sorted({task["kind"] for task in tasks}),
        "tooltip": "\n".join(lines),
        "stores": {
            name: {"kinds": store["kinds"], "tooltip": "\n".join(store["lines"])} for name, store in stores.items()
        },
    }


templates.env.globals["pbs_activity"] = view

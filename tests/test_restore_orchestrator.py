def test_ui_target_storages_does_not_wait_long_for_the_host(monkeypatch):
    """Rendering /settings must stay well below a reverse proxy's timeout."""
    from app.services import restore_config, restore_orchestrator, restore_perf

    seen = {}

    class _Server:
        def fio_available(self):
            return False

        def list_target_storages(self, timeout=None):
            seen["timeout"] = timeout
            return {"targets": [], "total": 0, "pbs": 0}

    monkeypatch.setattr(restore_config, "load", lambda: {"target_storages": []})
    monkeypatch.setattr(restore_orchestrator, "build_server", lambda config: _Server())
    for name in ("get_perf", "involved_storages", "running_storage", "get_progress"):
        monkeypatch.setattr(restore_perf, name, lambda *a, **k: {})
    restore_orchestrator.ui_target_storages()
    assert seen["timeout"] is not None and seen["timeout"] <= 10


def test_a_storage_gone_from_the_host_is_a_complete_row(monkeypatch):
    """A saved storage the host no longer lists (removed there) still renders:
    its row carries every field the Settings table reads, as "unknown"."""
    from app.services import restore_config, restore_orchestrator, restore_perf

    class _Server:
        def fio_available(self):
            return True

        def list_target_storages(self, timeout=None):
            return {
                "targets": [
                    {"storage": "tank", "type": "zfspool", "images": True, "rootdir": True, "used": 1, "avail": 2}
                ],
                "total": 1,
                "pbs": 0,
            }

    saved = [
        {"storage": "tank", "disk_class": "nvme", "use": True, "order": 0},
        {"storage": "nas", "disk_class": "hdd", "use": True, "order": 1},
    ]
    monkeypatch.setattr(restore_config, "load", lambda: {"target_storages": saved})
    monkeypatch.setattr(restore_orchestrator, "build_server", lambda config: _Server())
    for name in ("get_perf", "involved_storages", "running_storage", "get_progress"):
        monkeypatch.setattr(restore_perf, name, lambda *a, **k: {})
    rows = {r["storage"]: r for r in restore_orchestrator.ui_target_storages()["rows"]}
    gone = rows["nas"]
    assert gone["missing"] is True
    assert set(rows["tank"]) <= set(gone)
    assert gone["used"] is None and gone["free"] is None and gone["busy"] is False

    from app.web.common import templates

    # What the table does with those fields must not raise.
    snippet = (
        "{% if s.used is none and s.free is none %}-{% else %}"
        "{{ s.used | size_coarse if s.used is not none else '-' }}{% endif %}"
    )
    assert templates.env.from_string(snippet).render(s=gone) == "-"


def test_a_missing_storage_can_be_removed_from_the_table():
    from pathlib import Path

    html = Path("templates/settings.html").read_text()
    js = Path("static/js/common.js").read_text()
    assert "js-store-remove" in html and "js-store-remove" in js

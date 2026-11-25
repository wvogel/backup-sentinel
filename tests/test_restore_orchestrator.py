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

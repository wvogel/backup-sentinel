import json
from unittest import mock

from app.web import routes_restore


def _call(ajax, matchable=True):
    with (
        mock.patch.object(routes_restore.restore_config, "load", return_value={"x": 1}),
        mock.patch.object(routes_restore.restore_orchestrator, "build_server"),
        mock.patch.object(routes_restore.restore_orchestrator, "restore_host_pbs_entries", return_value=[]),
        mock.patch.object(routes_restore.restore_orchestrator, "vm_backup_locations", return_value=[]),
        mock.patch("app.services.restore_match.is_auto_restorable", return_value=matchable),
        mock.patch.object(routes_restore.db_restore, "create_run") as create,
        mock.patch("app.web.sync._kick_restore_queue"),
    ):
        return routes_restore.manual_test(mock.Mock(), 7, ajax=ajax), create


def test_ajax_start_answers_json_and_stays_on_page():
    resp, create = _call("1")
    assert resp.status_code == 200
    assert json.loads(resp.body) == {"ok": True, "error": ""}
    create.assert_called_once_with(7, trigger="manual")


def test_ajax_not_restorable_reports_error_without_queueing():
    resp, create = _call("1", matchable=False)
    assert json.loads(resp.body) == {"ok": False, "error": "not_restorable"}
    create.assert_not_called()


def test_plain_post_still_redirects_to_restore():
    resp, _ = _call("")
    assert resp.status_code == 303 and resp.headers["location"] == "/restore"

import json
import unittest
from unittest import mock

from app.web import routes_restore


class RestoreHostStateTests(unittest.TestCase):
    """/restore/progress.json tells a page rendered before the first host fetch
    whether that fetch is still running, succeeded or failed."""

    def _state(self, data, error):
        view = {"at": 0.0, "data": data, "error": error}
        with (
            mock.patch.object(routes_restore, "_host_view", view),
            mock.patch.object(routes_restore.db_restore, "list_runs", return_value=[]),
            mock.patch.object(routes_restore.restore_config, "load", return_value={}),
            mock.patch.object(routes_restore.threading, "Thread"),
        ):
            return json.loads(routes_restore.restore_progress(None).body)["host"]

    def test_loading_until_first_fetch_settles(self) -> None:
        self.assertEqual(self._state(None, None), "loading")

    def test_ready_once_data_is_there(self) -> None:
        self.assertEqual(self._state({"host": {}, "host_pbs": [], "vm_locations": []}, None), "ready")

    def test_error_only_after_a_failed_fetch(self) -> None:
        self.assertEqual(self._state(None, "TimeoutError: ssh"), "error")


if __name__ == "__main__":
    unittest.main()

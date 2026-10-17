import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from app.services import api_client
from app.services.api_client import _is_retryable_http


class IsRetryableHttpTests(unittest.TestCase):
    def test_500_is_retryable(self) -> None:
        self.assertTrue(_is_retryable_http(500))

    def test_502_is_retryable(self) -> None:
        self.assertTrue(_is_retryable_http(502))

    def test_503_is_retryable(self) -> None:
        self.assertTrue(_is_retryable_http(503))

    def test_504_is_retryable(self) -> None:
        self.assertTrue(_is_retryable_http(504))

    def test_429_is_retryable(self) -> None:
        self.assertTrue(_is_retryable_http(429))

    def test_404_is_not_retryable(self) -> None:
        self.assertFalse(_is_retryable_http(404))

    def test_401_is_not_retryable(self) -> None:
        self.assertFalse(_is_retryable_http(401))

    def test_400_is_not_retryable(self) -> None:
        self.assertFalse(_is_retryable_http(400))

    def test_200_is_not_retryable(self) -> None:
        # Sanity check: success codes aren't retried.
        self.assertFalse(_is_retryable_http(200))


if __name__ == "__main__":
    unittest.main()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    connections = 0
    drop_after_reply = False

    def setup(self) -> None:
        type(self).connections += 1
        super().setup()

    def do_GET(self) -> None:
        status = 500 if self.path.startswith("/fail") else 200
        body = json.dumps({"data": [{"path": self.path}]}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        # Close without "Connection: close", as a server does with an idle
        # keep-alive connection: the client only notices on its next request.
        self.close_connection = type(self).drop_after_reply

    def log_message(self, *args) -> None:
        pass


class KeepAliveTests(unittest.TestCase):
    def setUp(self) -> None:
        _Handler.connections = 0
        _Handler.drop_after_reply = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        api_client._local.__dict__.pop("pool", None)

    def tearDown(self) -> None:
        api_client._local.__dict__.pop("pool", None)
        self.server.shutdown()
        self.server.server_close()

    def test_requests_to_one_server_share_a_connection(self) -> None:
        for i in range(3):
            self.assertEqual(api_client.api_get_json(self.base, f"/a{i}", "x"), [{"path": f"/a{i}"}])
        self.assertEqual(_Handler.connections, 1)

    def test_connection_closed_by_the_server_is_reopened_without_waiting(self) -> None:
        _Handler.drop_after_reply = True
        api_client.api_get_json(self.base, "/a", "x")
        with mock.patch.object(api_client.time, "sleep") as sleep:
            self.assertEqual(api_client.api_get_json(self.base, "/b", "x"), [{"path": "/b"}])
        sleep.assert_not_called()
        self.assertEqual(_Handler.connections, 2)

    def test_http_error_keeps_status_and_body(self) -> None:
        with self.assertRaises(api_client.APIError) as ctx:
            api_client.api_get_json(self.base, "/fail", "x", retries=0)
        self.assertIn("HTTP 500", str(ctx.exception))
        self.assertIn('"/fail"', str(ctx.exception))
        # The connection stays usable after an error reply.
        self.assertEqual(api_client.api_get_json(self.base, "/ok", "x"), [{"path": "/ok"}])
        self.assertEqual(_Handler.connections, 1)

    def test_unreachable_server_is_an_api_error(self) -> None:
        with socket.socket() as probe:  # a port nobody listens on
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with self.assertRaises(api_client.APIError):
            api_client.api_get_json(f"http://127.0.0.1:{port}", "/a", "x", retries=0)

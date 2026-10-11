"""HTTP integration tests: real sockets, real JSON wire format, bounded
request validation. Uses the fixture snapshot, not the live network."""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from airport_intel.conversation.orchestrator import Orchestrator
from airport_intel.planning import llm
from airport_intel.server import app as server_module
from tests.fixtures import build_snapshot


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # HTTP integration tests must stay offline regardless of an ambient
        # GEMINI_API_KEY/.env.local on the machine running the suite.
        cls._gemini_patch = mock.patch.object(llm, "active", return_value=None)
        cls._gemini_patch.start()
        snapshot = build_snapshot()
        orchestrator = Orchestrator(snapshot)
        metrics = server_module.RequestMetrics()
        handler = server_module.build_handler(orchestrator, snapshot, metrics)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls._gemini_patch.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _post(self, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self._url("/api/chat"), data=data, method="POST",
                                           headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read())
            finally:
                exc.close()

    def test_health_reports_keyless_status_and_coverage(self):
        with urllib.request.urlopen(self._url("/health"), timeout=10) as response:
            body = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(body["model_status"], "keyless_fallback")
        self.assertIn("congestion", body["coverage"])

    def test_static_index_served(self):
        with urllib.request.urlopen(self._url("/"), timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"Airport Investment Intelligence", response.read())

    def test_static_path_traversal_blocked(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self._url("/../README.md"), timeout=10)
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()

    def test_chat_computed_round_trip(self):
        status, body = self._post({"session_id": "s1", "message": "Compare XQA and XQB on delays."})
        self.assertEqual(status, 200)
        self.assertEqual(body["result_state"], "computed")
        self.assertEqual(body["model_status"], "keyless_fallback")

    def test_chat_empty_message_returns_422(self):
        status, body = self._post({"session_id": "s1", "message": ""})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "invalid_request")

    def test_chat_malformed_json_returns_400(self):
        request = urllib.request.Request(self._url("/api/chat"), data=b"{not json",
                                           method="POST", headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()

    def test_oversized_body_rejected_before_parsing(self):
        request = urllib.request.Request(
            self._url("/api/chat"), data=json.dumps({"session_id": "s1", "message": "x" * 9000}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()

    def test_unknown_route_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self._url("/does-not-exist"), timeout=10)
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()

    def test_session_persists_follow_up_across_requests(self):
        self._post({"session_id": "persist1", "message": "What share of XQA departures go at least 2500 miles?"})
        status, body = self._post({"session_id": "persist1", "message": "And at 3500 miles?"})
        self.assertEqual(status, 200)
        self.assertEqual(body["follow_up_context"]["airport_codes"], ["XQA"])
        self.assertEqual(body["follow_up_context"]["threshold_miles"], 3500)


if __name__ == "__main__":
    unittest.main()

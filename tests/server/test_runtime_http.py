"""HTTP tests over the real SQLite runtime (`build_runtime`), offline.

Covers the browser-facing contract: explorer search/preview, US-only scope,
session clear, durable follow-up context across a simulated restart, and the
local-only request boundary (Host allowlist, JSON content type).
"""

import http.client
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from airport_intel.planning import llm
from airport_intel.server import app as server_module
from airport_intel.server.runtime import build_runtime


class SQLiteRuntimeHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._gemini_patch = mock.patch.object(llm, "active", return_value=None)
        cls._gemini_patch.start()
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.database = Path(cls.tempdir.name) / "runtime.sqlite3"
        cls._start()

    @classmethod
    def _start(cls):
        runtime = build_runtime(database_path=cls.database, offline=True)
        handler = server_module.runtime_handler(runtime, server_module.RequestMetrics())
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def _stop(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    @classmethod
    def tearDownClass(cls):
        cls._stop()
        cls._gemini_patch.stop()
        cls.tempdir.cleanup()

    def _get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            finally:
                exc.close()

    def _post(self, path, payload, content_type="application/json", host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": content_type}
        if host:
            headers["Host"] = host
        try:
            connection.request("POST", path, body=json.dumps(payload), headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            connection.close()

    def test_health_reports_sqlite_offline_state_and_forecast_coverage(self):
        status, body = self._get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["storage"], "sqlite")
        self.assertTrue(all(item["state"] == "offline" for item in body["refresh"].values()))
        self.assertEqual(body["coverage"]["demand_pressure"]["forecast_covered_airports"], 712)
        self.assertEqual(set(body["source_runs"]), {"airport", "airport_registry", "faa_taf"})

    def test_airport_search_and_preview_are_us_only(self):
        status, body = self._get("/api/airports?query=SFO")
        self.assertEqual(status, 200)
        self.assertEqual(body["airports"][0]["code"], "SFO")
        self.assertTrue(all(item["country"] == "US" for item in body["airports"]))
        self.assertEqual(self._get("/api/airports?query=Heathrow")[1]["airports"], [])
        self.assertEqual(self._get("/api/airports?query=" + "x" * 101)[0], 400)
        status, preview = self._get("/api/airports/SFO/preview")
        self.assertEqual(status, 200)
        self.assertEqual(preview["forecast"]["enplanements_2030"], 30_516_426)
        self.assertFalse(preview["capabilities"]["congestion"]["available"])
        self.assertEqual(self._get("/api/airports/LHR/preview")[0], 404)
        self.assertEqual(self._get("/api/airports/S%27%20OR%201/preview")[0], 404)

    def test_follow_up_context_survives_runtime_restart_and_clear_resets_it(self):
        cls = type(self)
        sid = "restart-session"
        status, first = self._post("/api/chat", {"session_id": sid, "message": "What share of BDL departures go at least 2,500 miles?"})
        self.assertEqual(status, 200)
        cls._stop()
        cls._start()  # new process-equivalent: new repository, provider and orchestrator
        status, second = self._post("/api/chat", {"session_id": sid, "message": "3000"})
        self.assertEqual(status, 200)
        self.assertEqual(second["follow_up_context"]["airport_codes"], ["BDL"])
        self.assertEqual(second["follow_up_context"]["threshold_miles"], 3000)
        self.assertEqual(self._post("/api/session/clear", {"session_id": sid})[1], {"status": "cleared"})
        _, after_clear = self._post("/api/chat", {"session_id": sid, "message": "3000"})
        self.assertNotEqual((after_clear.get("follow_up_context") or {}).get("airport_codes"), ["BDL"])

    def test_non_json_content_type_is_rejected_before_any_work(self):
        status, body = self._post("/api/chat", {"session_id": "s", "message": "hi"}, content_type="text/plain")
        self.assertEqual((status, body["error"]), (415, "unsupported_media_type"))

    def test_foreign_host_header_is_rejected(self):
        status, body = self._post("/api/chat", {"session_id": "s", "message": "hi"}, host="evil.example")
        self.assertEqual((status, body["error"]), (403, "invalid_host"))
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request("GET", "/health", headers={"Host": "rebind.example:8008"})
            self.assertEqual(connection.getresponse().status, 403)
        finally:
            connection.close()

    def test_malformed_requests_return_stable_client_errors(self):
        for payload, code in (
            ({"session_id": "bad id!", "message": "x"}, "invalid_session_id"),
            ({"session_id": "s", "message": 5}, "invalid_message_type"),
        ):
            status, body = self._post("/api/chat", payload)
            self.assertEqual((status, body["error"]), (400, code))
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request("POST", "/api/chat", body=b"[1]", headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual((response.status, json.loads(response.read())["error"]), (400, "non_object_json"))
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()

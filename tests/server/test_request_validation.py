"""Socket-free unit tests for the HTTP input boundary (``server.request_validation``)."""

import io
import json
import unittest

from airport_intel.server.request_validation import (
    LOCAL_HOSTS, MAX_BODY_BYTES, ClientError, host_allowed, message_from, read_json_object,
    require_json_content_type, search_query_from, session_id_from,
)


def _body(payload) -> tuple[dict, io.BytesIO]:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    return {"Content-Length": str(len(raw))}, io.BytesIO(raw)


class HostAllowlistTests(unittest.TestCase):
    def test_localhost_names_with_or_without_port_are_allowed(self):
        for header in ("127.0.0.1", "127.0.0.1:8008", "localhost:8008", "LOCALHOST", "[::1]:8008", "[::1]"):
            with self.subTest(header=header):
                self.assertTrue(host_allowed(header, LOCAL_HOSTS))

    def test_foreign_missing_and_lookalike_hosts_are_rejected(self):
        for header in (None, "", "evil.example", "rebind.example:8008", "localhost.evil.example",
                       "127.0.0.1.nip.io", "[::2]:8008"):
            with self.subTest(header=header):
                self.assertFalse(host_allowed(header, LOCAL_HOSTS))


class BodyValidationTests(unittest.TestCase):
    def _error(self, func, *args) -> ClientError:
        with self.assertRaises(ClientError) as ctx:
            func(*args)
        return ctx.exception

    def test_only_json_content_type_is_accepted(self):
        require_json_content_type("application/json")
        require_json_content_type("Application/JSON; charset=utf-8")
        for header in (None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
            with self.subTest(header=header):
                error = self._error(require_json_content_type, header)
                self.assertEqual((error.status, error.code), (415, "unsupported_media_type"))

    def test_content_length_is_validated_before_reading(self):
        body = io.BytesIO(b"{}")
        cases = [({"Content-Length": "abc"}, "invalid_content_length"),
                 ({}, "bad_request_size"),
                 ({"Content-Length": "0"}, "bad_request_size"),
                 ({"Content-Length": "-5"}, "bad_request_size"),
                 ({"Content-Length": str(MAX_BODY_BYTES + 1)}, "bad_request_size")]
        for headers, code in cases:
            with self.subTest(headers=headers):
                error = self._error(read_json_object, headers, body)
                self.assertEqual((error.status, error.code), (400, code))
        self.assertEqual(body.tell(), 0)  # nothing was read for any rejected size

    def test_body_must_be_utf8_json_object(self):
        for raw, code in ((b"{not json", "invalid_json"), (b"\xff\xfe", "invalid_json"),
                          (b"[1]", "non_object_json"), (b'"text"', "non_object_json")):
            with self.subTest(raw=raw):
                error = self._error(read_json_object, *_body(raw))
                self.assertEqual((error.status, error.code), (400, code))
        self.assertEqual(read_json_object(*_body({"message": "hi"})), {"message": "hi"})

    def test_session_id_shape_is_enforced_with_public_detail(self):
        self.assertEqual(session_id_from({}), "default")
        self.assertEqual(session_id_from({"session_id": "abc_DEF-123"}), "abc_DEF-123")
        for value in ("bad id!", "", "x" * 65, 5, None, ["s"]):
            with self.subTest(value=value):
                error = self._error(session_id_from, {"session_id": value})
                self.assertEqual(error.code, "invalid_session_id")
                self.assertIn("detail", error.payload)

    def test_message_must_be_a_string(self):
        self.assertEqual(message_from({}), "")
        error = self._error(message_from, {"message": 5})
        self.assertEqual(error.payload, {"error": "invalid_message_type", "detail": "message must be a string."})

    def test_search_query_is_bounded(self):
        self.assertEqual(search_query_from("SFO"), "SFO")
        self.assertEqual(self._error(search_query_from, "x" * 101).payload, {"error": "invalid_query"})


if __name__ == "__main__":
    unittest.main()

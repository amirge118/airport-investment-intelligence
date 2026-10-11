"""Validation of untrusted HTTP input, independent of sockets and routing.

Each check either returns a typed value or raises ``ClientError`` carrying a
stable status and public error code, so the request handler never turns a
malformed request into a 500 or silently coerces it. Checks are ordered by
the handler to reject cheaply first (host, content type, size) before any
body is read or parsed.
"""

from __future__ import annotations

import json
import re
from typing import BinaryIO, Mapping

MAX_BODY_BYTES = 8192
MAX_QUERY_CHARS = 100
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ClientError(Exception):
    """A request the client must fix; ``payload`` is safe to return verbatim."""

    def __init__(self, status: int, code: str, detail: str | None = None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.detail = detail

    @property
    def payload(self) -> dict:
        return {"error": self.code} if self.detail is None else {"error": self.code, "detail": self.detail}


def host_allowed(header: str | None, allowed: frozenset[str]) -> bool:
    """Host-header allowlist: blocks DNS-rebinding reads of the local API."""
    if not header:
        return False
    host = header.strip().lower()
    if host.startswith("["):
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.split(":", 1)[0]
    return host in allowed


def require_allowed_host(header: str | None, allowed: frozenset[str]) -> None:
    if not host_allowed(header, allowed):
        raise ClientError(403, "invalid_host")


def require_json_content_type(header: str | None) -> None:
    # A cross-site page can only send "simple" content types without a CORS
    # preflight, which this server never answers. Requiring JSON therefore
    # blocks drive-by POSTs (and Gemini quota burn).
    if (header or "").split(";")[0].strip().lower() != "application/json":
        raise ClientError(415, "unsupported_media_type")


def read_json_object(headers: Mapping[str, str], body: BinaryIO) -> dict:
    """Read a bounded body and require a JSON object; rejects before parsing when oversized."""
    raw_length = headers.get("Content-Length")
    try:
        length = int(raw_length) if raw_length is not None else 0
    except ValueError:
        raise ClientError(400, "invalid_content_length") from None
    if length <= 0 or length > MAX_BODY_BYTES:
        raise ClientError(400, "bad_request_size")
    try:
        payload = json.loads(body.read(length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ClientError(400, "invalid_json") from None
    if not isinstance(payload, dict):
        raise ClientError(400, "non_object_json")
    return payload


def session_id_from(payload: dict) -> str:
    session_id = payload.get("session_id", "default")
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        raise ClientError(400, "invalid_session_id",
                          "session_id must be a 1-64 character string of letters, digits, '-', or '_'.")
    return session_id


def message_from(payload: dict) -> str:
    message = payload.get("message", "")
    if not isinstance(message, str):
        raise ClientError(400, "invalid_message_type", "message must be a string.")
    return message


def search_query_from(raw_query: str) -> str:
    query = raw_query
    if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
        raise ClientError(400, "invalid_query")
    return query

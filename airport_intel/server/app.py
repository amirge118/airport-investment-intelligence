"""Localhost-only demo server: HTTP transport and routing. Stdlib only.

Routes:
  GET  /                 -> web/index.html
  GET  /app.js, /*.css   -> static files from web/
  GET  /health           -> source runs, refresh state, installed coverage, model status
  GET  /api/capabilities -> coverage + active source runs
  GET  /api/airports?query=...        -> bounded US-only airport search (SQLite)
  GET  /api/airports/{code}/preview   -> US-only traffic/forecast/capability preview
  POST /api/chat         -> {session_id, message} -> Answer JSON
  POST /api/session/clear -> {session_id}

Binds 127.0.0.1 only; Host header must be a localhost name and POST bodies must
be application/json. No authentication/authorization/rate limiting exists; this
is an interview demo, not a deployable service.

Input validation lives in ``request_validation``; object wiring in ``runtime``;
the CLI in ``__main__``.
This module only maps validated requests to those collaborators and writes
JSON/static responses with structured, body-free request logs.
"""

from __future__ import annotations

import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from ..analytics.coverage import airport_preview, coverage_report
from ..planning import llm
from .request_validation import (
    LOCAL_HOSTS, ClientError, message_from, read_json_object, require_allowed_host,
    require_json_content_type, search_query_from, session_id_from,
)
from ..conversation.orchestrator import Orchestrator
from ..ingestion.refresh import RefreshCoordinator
from .runtime import (
    DATABASE_PATH, PUBLIC_ARTIFACT_DIR, ROOT, SNAPSHOT_PATH, Runtime, build_runtime,
)
from ..storage import FactReader

WEB_DIR = ROOT / "web"
STATIC_ROUTES = {"/": "index.html", "/index.html": "index.html",
                 "/app.js": "app.js", "/style.css": "style.css", "/decision.css": "decision.css"}
_STATIC_CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                         ".css": "text/css; charset=utf-8"}


class RequestMetrics:
    """Process-local request/error counters reported by /health."""

    def __init__(self):
        self.requests = 0
        self.errors = 0
        self.started_at = time.time()


def model_status() -> str:
    return llm.label() or "keyless_fallback"


def build_handler(orchestrator: Orchestrator, coverage_facts: dict | Callable[[], dict],
                  metrics: RequestMetrics, reader: FactReader | None = None,
                  refresh: RefreshCoordinator | None = None,
                  allowed_hosts: frozenset[str] = LOCAL_HOSTS):
    """Request handler class bound to its collaborators.

    ``coverage_facts`` is the national fact view used only for coverage
    counts (a dict for fixtures, ``FactProvider.for_coverage`` at runtime).
    Without a ``reader`` (fixture mode) the SQLite-only explorer routes 404.
    """
    coverage_getter = coverage_facts if callable(coverage_facts) else lambda: coverage_facts

    class Handler(BaseHTTPRequestHandler):
        server_version = "AirportIntel/1.0"

        def log_message(self, fmt, *args):
            # Request id, path, status, duration -- never the request body or a key.
            pass

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, rel_path: str) -> None:
            path = (WEB_DIR / rel_path.lstrip("/")).resolve()
            if WEB_DIR not in path.parents and path != WEB_DIR:
                self.send_error(404)
                return
            if not path.is_file():
                self.send_error(404)
                return
            content_type = _STATIC_CONTENT_TYPES.get(path.suffix, "application/octet-stream")
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # -- GET ---------------------------------------------------------------

        def _health(self) -> dict:
            facts = coverage_getter()
            return {
                "status": "ok",
                "storage": "sqlite" if reader else "fixture",
                "data_version": facts.get("version"),
                "data_retrieved_at": facts.get("retrieved_at"),
                "model_status": model_status(),
                "coverage": coverage_report(facts),
                "source_runs": reader.source_status() if reader else {},
                "refresh": refresh.status() if refresh else {},
                "requests_served": metrics.requests,
                "uptime_seconds": round(time.time() - metrics.started_at, 1),
            }

        def _capabilities(self) -> dict:
            facts = coverage_getter()
            return {
                "coverage": coverage_report(facts),
                "sources": reader.source_status() if reader else facts.get("sources", {}),
            }

        def _route_get(self, path: str, query: str) -> None:
            if path in STATIC_ROUTES:
                self._send_static(STATIC_ROUTES[path])
            elif path == "/health":
                self._send_json(200, self._health())
            elif path == "/api/capabilities":
                self._send_json(200, self._capabilities())
            elif path == "/api/airports" and reader:
                search = search_query_from(parse_qs(query).get("query", [""])[0])
                self._send_json(200, {"airports": reader.search_airports(search, limit=10)})
            elif path.startswith("/api/airports/") and path.endswith("/preview") and reader:
                parts = path.strip("/").split("/")
                preview = airport_preview(orchestrator.facts, parts[2] if len(parts) == 4 else "")
                if preview is None:
                    self._send_json(404, {"error": "airport_not_found"})
                else:
                    self._send_json(200, preview)
            else:
                self.send_error(404)

        def do_GET(self):
            request_id = uuid.uuid4().hex[:12]
            started = time.time()
            metrics.requests += 1
            try:
                require_allowed_host(self.headers.get("Host"), allowed_hosts)
                parsed = urlsplit(self.path)
                self._route_get(parsed.path, parsed.query)
            except ClientError as error:
                self._send_json(error.status, error.payload)
            except Exception:
                metrics.errors += 1
                self._send_json(500, {"error": "internal_error", "request_id": request_id})
            finally:
                duration_ms = round((time.time() - started) * 1000, 1)
                print(f"request_id={request_id} path={self.path} duration_ms={duration_ms}")

        # -- POST --------------------------------------------------------------

        def do_POST(self):
            request_id = uuid.uuid4().hex[:12]
            started = time.time()
            metrics.requests += 1
            status_code = 200
            error_code = None
            answer = None
            try:
                require_allowed_host(self.headers.get("Host"), allowed_hosts)
                if self.path not in ("/api/chat", "/api/session/clear"):
                    self.send_error(404)
                    return
                require_json_content_type(self.headers.get("Content-Type"))
                payload = read_json_object(self.headers, self.rfile)
                session_id = session_id_from(payload)

                if self.path == "/api/session/clear":
                    orchestrator.sessions.clear(session_id)
                    self._send_json(200, {"status": "cleared"})
                    return

                message = message_from(payload)
                try:
                    answer = orchestrator.handle(session_id, message)
                except ValueError as exc:
                    raise ClientError(422, "invalid_request", str(exc)) from None
                self._send_json(200, {**answer, "request_id": request_id, "model_status": model_status()})
            except ClientError as error:
                status_code, error_code = error.status, error.code
                self._send_json(error.status, error.payload)
            except Exception:
                metrics.errors += 1
                status_code, error_code = 500, "internal_error"
                self._send_json(500, {"error": error_code, "request_id": request_id})
            finally:
                duration_ms = round((time.time() - started) * 1000, 1)
                trace = (answer or {}).get("analysis_trace", {})
                # Request id, selected operation, resolved scope, model/fallback status,
                # duration, and error code only -- never the raw question or an API key.
                print(f"request_id={request_id} path={self.path} status={status_code} "
                      f"error={error_code} operation={trace.get('selected_tools')} "
                      f"resolved_scope={(answer or {}).get('resolved_scope')} "
                      f"result_state={trace.get('result_state')} "
                      f"planner={trace.get('planner_source')} duration_ms={duration_ms}")

    return Handler


def runtime_handler(runtime: Runtime, metrics: RequestMetrics,
                    allowed_hosts: frozenset[str] = LOCAL_HOSTS):
    """``build_handler`` wired to a SQLite ``Runtime``."""
    return build_handler(runtime.orchestrator, runtime.facts.for_coverage, metrics,
                         runtime.reader, runtime.refresh, allowed_hosts=allowed_hosts)


def run(host: str = "127.0.0.1", port: int = 8008, offline: bool = False,
        database_path: str | Path = DATABASE_PATH) -> None:
    runtime = build_runtime(
        database_path=database_path, snapshot_path=SNAPSHOT_PATH, offline=offline,
        artifact_dir=PUBLIC_ARTIFACT_DIR,
    )
    handler = runtime_handler(runtime, RequestMetrics(), allowed_hosts=LOCAL_HOSTS | {host.lower()})
    server = ThreadingHTTPServer((host, port), handler)
    facts = runtime.facts.for_coverage()
    print(f"Airport Investment Intelligence demo listening on http://{host}:{port} "
          f"(storage=sqlite, data {facts.get('version')}, refresh={'offline' if offline else 'background'}, "
          f"model_status={model_status()})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

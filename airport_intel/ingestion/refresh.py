"""Startup refresh of the three allowlisted public sources.

Each ``refresh_*`` job fetches one source through ``live_sources``,
attaches a manifest, and hands the complete candidate to the validated
``SourceRunPublisher``. ``RefreshCoordinator`` runs the jobs once in a
background thread and keeps a per-source status for ``/health``; a failure
is isolated to its source and the last-good run stays active.
"""

from __future__ import annotations

import json
import threading
from typing import Callable

from . import live_sources
from ..storage.database import utc_now
from ..storage.fact_reader import FactReader
from ..storage.source_runs import SourceRunPublisher

REFRESHED_SOURCES = ("airport", "airport_registry", "faa_taf")


def refresh_registry(publisher: SourceRunPublisher) -> None:
    rows = live_sources.fetch_global_registry()
    publisher.publish_airports(rows, {
        "url": live_sources.REGISTRY_INFO_PAGE, "download": live_sources.REGISTRY_SOURCE,
        "retrieved_at": utc_now(),
        "scope": "Global airport identity and geography only",
    })


def refresh_traffic(publisher: SourceRunPublisher) -> None:
    rows = []
    for year in (2023, 2024, 2025):
        rows.extend(live_sources.fetch_bts_airport_year(year))
    publisher.publish_traffic(rows, {
        "url": live_sources.BTS_SOURCE_PAGE, "api": live_sources.BTS_DATASET,
        "retrieved_at": utc_now(), "period": "2023-01/2025-12",
        "scope": "T-100 airport-level all-carrier aggregates",
    })


def refresh_taf(publisher: SourceRunPublisher, reader: FactReader) -> None:
    """FAA TAF is filtered to the *currently active* US registry."""
    us_codes = reader.active_us_codes()
    values, counts, raw = live_sources.fetch_and_build_faa_taf(us_codes)
    publisher.publish_taf(values, {
        "url": live_sources.TAF_SOURCE_PAGE, "download": live_sources.TAF_SOURCE_URL,
        "retrieved_at": utc_now(), "sha256": live_sources.taf_source_hash(raw),
        "fiscal_period": live_sources.TAF_FISCAL_PERIOD,
        "published_airports": len(values), **counts,
    })


class RefreshCoordinator:
    """Bounded background refresh with visible per-source status."""

    def __init__(self, publisher: SourceRunPublisher, reader: FactReader):
        self.publisher = publisher
        self.reader = reader
        self._lock = threading.Lock()
        self._status: dict[str, dict] = {
            name: {"state": "last_good", "error": None} for name in REFRESHED_SOURCES
        }
        self._thread: threading.Thread | None = None

    def status(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._status))

    def start(self, offline: bool = False) -> None:
        if offline:
            with self._lock:
                for item in self._status.values():
                    item.update({"state": "offline", "error": None})
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            for item in self._status.values():
                item.update({"state": "queued", "error": None})
            self._thread = threading.Thread(target=self._run, name="source-refresh", daemon=True)
            self._thread.start()

    def wait(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if not thread:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _set(self, source: str, state: str, error: str | None = None) -> None:
        with self._lock:
            self._status[source] = {"state": state, "error": error, "updated_at": utc_now()}

    def _attempt(self, source: str, job: Callable[[], None]) -> None:
        self._set(source, "refreshing")
        try:
            job()
        except Exception as exc:  # dependency failure is isolated per source
            self._set(source, "failed_last_good", type(exc).__name__)
        else:
            self._set(source, "fresh")

    def _run(self) -> None:
        # Registry first: the TAF job filters by the registry it just published.
        self._attempt("airport_registry", lambda: refresh_registry(self.publisher))
        self._attempt("airport", lambda: refresh_traffic(self.publisher))
        self._attempt("faa_taf", lambda: refresh_taf(self.publisher, self.reader))

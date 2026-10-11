"""Composition root: wire storage, refresh and the question pipeline once.

``build_runtime`` is the single place that knows how the pieces fit:
open/seed the SQLite file, create the read and write sides, the durable
session store, the background refresh, and the ``Orchestrator``. The HTTP
server, the evaluation runner and integration tests all start here, so they
exercise the same object graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..conversation.orchestrator import Orchestrator
from ..ingestion.install import install_bundled_public_data
from ..ingestion.refresh import RefreshCoordinator
from ..storage import (
    FactProvider, FactReader, SQLiteDatabase, SQLiteSessionStore, SourceRunPublisher, seed_from_snapshot,
)

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = ROOT / "data" / "snapshot.json"
DATABASE_PATH = ROOT / "data" / "airport_intel.sqlite3"
PUBLIC_ARTIFACT_DIR = ROOT / "data" / "source_artifacts"


@dataclass(frozen=True)
class Runtime:
    database: SQLiteDatabase
    publisher: SourceRunPublisher
    reader: FactReader
    facts: FactProvider
    sessions: SQLiteSessionStore
    refresh: RefreshCoordinator
    orchestrator: Orchestrator


def build_runtime(database_path: str | Path = DATABASE_PATH,
                  snapshot_path: str | Path = SNAPSHOT_PATH,
                  offline: bool = False,
                  artifact_dir: str | Path | None = None) -> Runtime:
    """Open (and on first boot seed) the database, then start the refresh.

    When ``artifact_dir`` is supplied, missing bundled one-time sources are
    installed before serving. Last-good rows are then servable before this
    returns; the live refresh runs in a background thread unless ``offline``
    is set, in which case no network call is made.
    """
    database = SQLiteDatabase(database_path)
    publisher = SourceRunPublisher(database)
    seed_from_snapshot(publisher, snapshot_path)
    reader = FactReader(database)
    if artifact_dir is not None:
        install_bundled_public_data(publisher, reader, artifact_dir)
    facts = FactProvider(reader)
    sessions = SQLiteSessionStore(database)
    refresh = RefreshCoordinator(publisher, reader)
    orchestrator = Orchestrator(facts, sessions=sessions)
    refresh.start(offline=offline)
    return Runtime(database, publisher, reader, facts, sessions, refresh, orchestrator)

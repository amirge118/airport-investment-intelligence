"""Shared real-SQLite test base: a throw-away database seeded from the bundled snapshot."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from airport_intel.storage import FactReader, SQLiteDatabase, SourceRunPublisher, seed_from_snapshot

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH = ROOT / "data" / "snapshot.json"


class SeededDatabaseTestCase(unittest.TestCase):
    """Gives each test a freshly seeded ``database``/``publisher``/``reader``."""

    @classmethod
    def setUpClass(cls):
        cls.source_snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.database = SQLiteDatabase(Path(self.tempdir.name) / "airport.sqlite3")
        self.publisher = SourceRunPublisher(self.database)
        self.reader = FactReader(self.database)
        self.assertTrue(seed_from_snapshot(self.publisher, SNAPSHOT_PATH))

    def open_database(self, name: str) -> tuple[SQLiteDatabase, SourceRunPublisher, FactReader]:
        """An additional, unseeded database in this test's temp directory."""
        database = SQLiteDatabase(Path(self.tempdir.name) / name)
        return database, SourceRunPublisher(database), FactReader(database)

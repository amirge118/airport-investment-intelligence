"""SQLite persistence, split by responsibility.

``database``     one SQLite file: connections, schema, additive migration, views
``source_runs``  write side: validate and atomically publish versioned source runs
``bootstrap``    first-boot seeding from ``data/snapshot.json`` and legacy repair
``fact_reader``  read side: plan-scoped fact bundles, search, status, version token
``sessions``     expiring typed follow-up context

The database is the runtime source of truth. ``data/snapshot.json`` is used
only to bootstrap an empty database and remains a reproducible recovery
artifact. Public-source refreshes build a complete candidate run and switch
the active pointer in one transaction; failed refreshes leave the last-good
run readable.
"""

from .bootstrap import seed_from_snapshot
from .database import SQLiteDatabase
from .fact_reader import FactProvider, FactReader
from .sessions import SQLiteSessionStore
from .source_runs import SourceRunPublisher

__all__ = [
    "FactProvider", "FactReader", "SQLiteDatabase", "SQLiteSessionStore",
    "SourceRunPublisher", "seed_from_snapshot",
]

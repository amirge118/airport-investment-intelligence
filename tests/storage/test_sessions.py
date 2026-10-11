"""Durable typed follow-up context (``storage.SQLiteSessionStore``) on real SQLite."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from airport_intel.conversation.context import InMemorySessionStore, TurnContext
from airport_intel.storage import sessions as sessions_module
from airport_intel.storage import SQLiteDatabase, SQLiteSessionStore
from tests.sqlite_fixture import SeededDatabaseTestCase


class SQLiteSessionStoreTests(SeededDatabaseTestCase):
    def test_typed_session_context_survives_store_recreation_and_can_clear(self):
        first = SQLiteSessionStore(self.database)
        first.set("session-1", TurnContext(airport_codes=["SFO"], operations=["assess_demand_pressure"]))
        second = SQLiteSessionStore(SQLiteDatabase(self.database.path))
        self.assertEqual(second.get("session-1").airport_codes, ["SFO"])
        second.clear("session-1")
        self.assertEqual(first.get("session-1"), TurnContext())

    def test_expired_session_is_dropped_and_capacity_is_bounded(self):
        store = SQLiteSessionStore(self.database)
        store.set("old", TurnContext(airport_codes=["SFO"]))
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        with self.database.connect() as connection:
            connection.execute("UPDATE session_context SET expires_at=? WHERE session_id='old'", (past,))
        self.assertEqual(store.get("old"), TurnContext())
        with self.database.connect() as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM session_context WHERE session_id='old'").fetchone())
        with mock.patch.object(sessions_module, "MAX_SESSION_ROWS", 3):
            for index in range(6):
                store.set(f"s{index}", TurnContext(airport_codes=["BOS"]))
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM session_context").fetchone()[0], 3)

    def test_corrupt_stored_context_degrades_to_empty_context(self):
        store = SQLiteSessionStore(self.database)
        store.set("bad", TurnContext(airport_codes=["SFO"]))
        with self.database.connect() as connection:
            connection.execute("UPDATE session_context SET context_json='{\"unexpected\": 1}' WHERE session_id='bad'")
        self.assertEqual(store.get("bad"), TurnContext())

    def test_sqlite_and_in_memory_stores_share_one_contract(self):
        context = TurnContext(airport_codes=["BDL"], threshold_miles=2500, operations=["longhaul_share"])
        for store in (SQLiteSessionStore(self.database), InMemorySessionStore()):
            with self.subTest(store=type(store).__name__):
                self.assertEqual(store.get("contract"), TurnContext())
                store.set("contract", context)
                self.assertEqual(store.get("contract"), context)
                store.clear("contract")
                self.assertEqual(store.get("contract"), TurnContext())


if __name__ == "__main__":
    unittest.main()

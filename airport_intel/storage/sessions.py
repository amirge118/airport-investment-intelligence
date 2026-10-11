"""Durable typed follow-up context with expiry and a row bound.

Only validated ``TurnContext`` slots are stored, never message text or
computed answers. Rows expire after ``SESSION_TTL_HOURS``; the table is
capped at ``MAX_SESSION_ROWS`` most-recently-updated sessions. Implements the
same ``get``/``set``/``clear`` contract as ``context.InMemorySessionStore``.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from ..conversation.context import TurnContext
from .database import SQLiteDatabase, utc_now

SESSION_TTL_HOURS = 24
MAX_SESSION_ROWS = 1000


class SQLiteSessionStore:
    """Session id -> ``TurnContext`` persisted in the ``session_context`` table."""

    def __init__(self, database: SQLiteDatabase):
        self.database = database

    def get(self, session_id: str) -> TurnContext:
        now = utc_now()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT context_json,expires_at FROM session_context WHERE session_id=?", (session_id,)
            ).fetchone()
            if not row or row["expires_at"] <= now:
                if row:
                    connection.execute("DELETE FROM session_context WHERE session_id=?", (session_id,))
                return TurnContext()
        try:
            return TurnContext(**json.loads(row["context_json"]))
        except (TypeError, ValueError):
            # Unreadable/foreign-shaped context is dropped, never a 500.
            self.clear(session_id)
            return TurnContext()

    def set(self, session_id: str, context: TurnContext) -> None:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=SESSION_TTL_HOURS)
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO session_context(session_id,context_json,updated_at,expires_at)
                   VALUES(?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET
                   context_json=excluded.context_json,updated_at=excluded.updated_at,
                   expires_at=excluded.expires_at""",
                (session_id, json.dumps(asdict(context), sort_keys=True), now.isoformat(), expires.isoformat()),
            )
            connection.execute("DELETE FROM session_context WHERE expires_at <= ?", (now.isoformat(),))
            connection.execute(
                """DELETE FROM session_context WHERE session_id IN (
                       SELECT session_id FROM session_context
                       ORDER BY updated_at DESC LIMIT -1 OFFSET ?
                   )""",
                (MAX_SESSION_ROWS,),
            )

    def clear(self, session_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute("DELETE FROM session_context WHERE session_id=?", (session_id,))

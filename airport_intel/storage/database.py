"""One SQLite file: connection policy, schema, additive migration and views.

This module owns *how* the application talks to SQLite (pragmas, closing
connections, single-snapshot read transactions) and *what* tables exist. It
knows nothing about which rows are valid or which facts a question needs;
that belongs to ``source_runs`` (writes) and ``fact_reader`` (reads).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

SCHEMA_VERSION = 3


class _ClosingConnection(sqlite3.Connection):
    """Make `with database.connect()` close as well as commit/rollback."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_hash(value: object) -> str:
    """Stable content hash used as a source run's identity."""
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(payload).hexdigest()


class SQLiteDatabase:
    """A local SQLite file with the application schema guaranteed present.

    Thread-safe by connection: every caller opens its own short-lived
    connection, and WAL mode lets readers proceed during a publish.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self.ensure_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, factory=_ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def read_transaction(self) -> sqlite3.Connection:
        """Connection inside one read transaction (a single WAL snapshot), so a
        multi-statement fact bundle never mixes rows from before and after a
        concurrent source publish."""
        connection = self.connect()
        connection.execute("BEGIN")
        return connection

    def query_plan(self, sql: str, parameters: tuple = ()) -> list[str]:
        """``EXPLAIN QUERY PLAN`` details, for index evidence in tests."""
        with self.connect() as connection:
            return [row["detail"] for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters)]

    def ensure_schema(self) -> None:
        with self._schema_lock, self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('candidate','active','superseded','failed')),
                    retrieved_at TEXT NOT NULL,
                    last_checked_at TEXT NOT NULL,
                    period TEXT,
                    url TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    row_count INTEGER NOT NULL CHECK(row_count >= 0),
                    manifest_json TEXT NOT NULL,
                    error_code TEXT,
                    UNIQUE(source, checksum)
                );
                CREATE TABLE IF NOT EXISTS active_source_runs (
                    source TEXT PRIMARY KEY,
                    run_id INTEGER NOT NULL REFERENCES source_runs(id)
                );
                CREATE TABLE IF NOT EXISTS airports (
                    run_id INTEGER NOT NULL REFERENCES source_runs(id) ON DELETE CASCADE,
                    code TEXT NOT NULL,
                    icao TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    city TEXT NOT NULL DEFAULT '',
                    country TEXT NOT NULL,
                    region TEXT NOT NULL DEFAULT '',
                    type TEXT NOT NULL DEFAULT '',
                    scheduled INTEGER NOT NULL CHECK(scheduled IN (0,1)),
                    PRIMARY KEY(run_id, code)
                );
                CREATE INDEX IF NOT EXISTS idx_airports_run_country_code
                    ON airports(run_id, country, code);
                CREATE INDEX IF NOT EXISTS idx_airports_run_city
                    ON airports(run_id, city);
                CREATE TABLE IF NOT EXISTS airport_traffic_year (
                    run_id INTEGER NOT NULL REFERENCES source_runs(id) ON DELETE CASCADE,
                    code TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    city TEXT NOT NULL DEFAULT '',
                    months INTEGER NOT NULL,
                    passengers INTEGER NOT NULL,
                    seats INTEGER NOT NULL,
                    domestic_passengers INTEGER NOT NULL,
                    domestic_seats INTEGER NOT NULL,
                    domestic_departures INTEGER NOT NULL,
                    international_departures INTEGER NOT NULL,
                    PRIMARY KEY(run_id, code, year),
                    CHECK(months BETWEEN 0 AND 12),
                    CHECK(passengers >= 0 AND seats >= 0),
                    CHECK(domestic_passengers >= 0 AND domestic_seats >= 0),
                    CHECK(domestic_departures >= 0 AND international_departures >= 0)
                );
                CREATE INDEX IF NOT EXISTS idx_traffic_run_year_code
                    ON airport_traffic_year(run_id, year, code);
                CREATE TABLE IF NOT EXISTS taf_enplanements (
                    run_id INTEGER NOT NULL REFERENCES source_runs(id) ON DELETE CASCADE,
                    code TEXT NOT NULL,
                    fiscal_year INTEGER NOT NULL,
                    scenario INTEGER NOT NULL DEFAULT 1,
                    enplanements INTEGER NOT NULL CHECK(enplanements > 0),
                    PRIMARY KEY(run_id, code, fiscal_year, scenario)
                );
                CREATE INDEX IF NOT EXISTS idx_taf_run_code_year
                    ON taf_enplanements(run_id, code, fiscal_year);
                CREATE TABLE IF NOT EXISTS route_segment_year (
                    run_id INTEGER NOT NULL REFERENCES source_runs(id) ON DELETE CASCADE,
                    origin TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    service_class TEXT NOT NULL,
                    data_source TEXT NOT NULL,
                    distance_miles INTEGER NOT NULL CHECK(distance_miles > 0),
                    departures INTEGER NOT NULL CHECK(departures >= 0),
                    seats INTEGER NOT NULL CHECK(seats >= 0),
                    passengers INTEGER NOT NULL CHECK(passengers >= 0),
                    freight_pounds INTEGER NOT NULL CHECK(freight_pounds >= 0),
                    PRIMARY KEY(run_id, origin, destination, year, service_class, data_source, distance_miles)
                );
                CREATE INDEX IF NOT EXISTS idx_route_run_origin_year
                    ON route_segment_year(run_id, origin, year);
                CREATE TABLE IF NOT EXISTS airport_delay_month (
                    run_id INTEGER NOT NULL REFERENCES source_runs(id) ON DELETE CASCADE,
                    code TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
                    flights INTEGER NOT NULL CHECK(flights >= 0),
                    delayed INTEGER NOT NULL CHECK(delayed >= 0),
                    cancelled INTEGER NOT NULL CHECK(cancelled >= 0),
                    diverted INTEGER NOT NULL CHECK(diverted >= 0),
                    arrival_delay_minutes INTEGER NOT NULL CHECK(arrival_delay_minutes >= 0),
                    carrier_delay_minutes INTEGER NOT NULL CHECK(carrier_delay_minutes >= 0),
                    weather_delay_minutes INTEGER NOT NULL CHECK(weather_delay_minutes >= 0),
                    nas_delay_minutes INTEGER NOT NULL CHECK(nas_delay_minutes >= 0),
                    security_delay_minutes INTEGER NOT NULL CHECK(security_delay_minutes >= 0),
                    late_aircraft_delay_minutes INTEGER NOT NULL CHECK(late_aircraft_delay_minutes >= 0),
                    PRIMARY KEY(run_id, code, year, month),
                    CHECK(delayed + cancelled + diverted <= flights)
                );
                CREATE INDEX IF NOT EXISTS idx_delay_run_code_year
                    ON airport_delay_month(run_id, code, year);
                CREATE TABLE IF NOT EXISTS airport_development_plan (
                    run_id INTEGER NOT NULL REFERENCES source_runs(id) ON DELETE CASCADE,
                    locid TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT '',
                    city TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL DEFAULT '',
                    ownership TEXT NOT NULL DEFAULT '',
                    service_level TEXT NOT NULL DEFAULT '',
                    hub_size TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT '',
                    enplanements INTEGER NOT NULL CHECK(enplanements >= 0),
                    based_aircraft INTEGER NOT NULL CHECK(based_aircraft >= 0),
                    development_estimate INTEGER NOT NULL CHECK(development_estimate >= 0),
                    PRIMARY KEY(run_id, locid)
                );
                CREATE INDEX IF NOT EXISTS idx_development_run_locid
                    ON airport_development_plan(run_id, locid);
                CREATE TABLE IF NOT EXISTS snapshot_extras (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_context (
                    session_id TEXT PRIMARY KEY,
                    context_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_session_expiry ON session_context(expires_at);
                """
            )
            # Schema v1 existed only during local development. Keep this
            # additive migration so an already-created reviewer DB remains
            # usable instead of requiring a destructive rebuild.
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(source_runs)")}
            if "last_checked_at" not in columns:
                connection.execute("ALTER TABLE source_runs ADD COLUMN last_checked_at TEXT")
                connection.execute(
                    "UPDATE source_runs SET last_checked_at=retrieved_at WHERE last_checked_at IS NULL"
                )
            connection.execute(
                "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            self._create_views(connection)

    @staticmethod
    def _create_views(connection: sqlite3.Connection) -> None:
        """Read-only ``active_*`` views hide superseded runs from every query."""
        connection.executescript(
            """
            DROP VIEW IF EXISTS active_airports;
            CREATE VIEW active_airports AS
            SELECT a.* FROM airports a
            JOIN active_source_runs r ON r.source='airport_registry' AND r.run_id=a.run_id;
            DROP VIEW IF EXISTS active_airport_traffic_year;
            CREATE VIEW active_airport_traffic_year AS
            SELECT t.* FROM airport_traffic_year t
            JOIN active_source_runs r ON r.source='airport' AND r.run_id=t.run_id;
            DROP VIEW IF EXISTS active_taf_enplanements;
            CREATE VIEW active_taf_enplanements AS
            SELECT t.* FROM taf_enplanements t
            JOIN active_source_runs r ON r.source='faa_taf' AND r.run_id=t.run_id;
            DROP VIEW IF EXISTS active_route_segment_year;
            CREATE VIEW active_route_segment_year AS
            SELECT t.* FROM route_segment_year t
            JOIN active_source_runs r ON r.source='bts_t100_segment' AND r.run_id=t.run_id;
            DROP VIEW IF EXISTS active_airport_delay_month;
            CREATE VIEW active_airport_delay_month AS
            SELECT d.* FROM airport_delay_month d
            JOIN active_source_runs r ON r.source='bts_delay_causes' AND r.run_id=d.run_id;
            DROP VIEW IF EXISTS active_airport_development_plan;
            CREATE VIEW active_airport_development_plan AS
            SELECT d.* FROM airport_development_plan d
            JOIN active_source_runs r ON r.source='faa_npias' AND r.run_id=d.run_id;
            """
        )

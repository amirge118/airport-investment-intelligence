"""Write side: validate a complete source candidate, then publish it atomically.

Every source is
stored as an immutable, content-addressed *run*. Publishing inserts the run's
rows and moves that source's active pointer in the same transaction, so a
reader sees the old complete version or the new one, never a mix. Identical
content reuses the existing run and only bumps ``last_checked_at``. Rejected
candidates raise ``ValueError`` before anything is written.
"""

from __future__ import annotations

import json
import sqlite3

from ..ingestion.live_sources import MIN_BTS_AIRPORTS_PER_YEAR, MIN_REGISTRY_ROWS
from .database import SQLiteDatabase, canonical_hash, utc_now

# Superseded runs kept per source for inspection/rollback; older ones are
# deleted (rows cascade) so routine startup refreshes cannot grow the file
# without bound.
KEEP_SUPERSEDED_RUNS = 1
REQUIRED_SOURCES = frozenset({"airport", "airport_registry", "faa_taf"})
TRAFFIC_FIELDS = ("passengers", "seats", "domestic_passengers", "domestic_seats",
                  "domestic_departures", "international_departures")
MIN_TAF_AIRPORTS = 500
ROUTE_VALUE_FIELDS = ("departures", "seats", "passengers", "freight_pounds")
DELAY_VALUE_FIELDS = (
    "flights", "delayed", "cancelled", "diverted", "arrival_delay_minutes",
    "carrier_delay_minutes", "weather_delay_minutes", "nas_delay_minutes",
    "security_delay_minutes", "late_aircraft_delay_minutes",
)


def _validated_registry_rows(rows: list[dict]) -> list[dict]:
    if len(rows) < MIN_REGISTRY_ROWS:
        raise ValueError(f"Global registry unexpectedly small: {len(rows)}")
    seen: set[str] = set()
    for row in rows:
        code = str(row.get("code", "")).upper()
        if len(code) != 3 or not code.isalpha() or code in seen:
            raise ValueError(f"Invalid or duplicate airport code: {code!r}")
        seen.add(code)
    return list(rows)


def _validated_traffic_rows(rows: list[dict]) -> list[dict]:
    """Reject bad keys/values and thin coverage; return rows in canonical order."""
    keys: set[tuple[str, int]] = set()
    counts: dict[int, int] = {}
    for row in rows:
        code, year = str(row.get("code", "")).upper(), int(row.get("year", 0))
        key = (code, year)
        if not (2 <= len(code) <= 4 and code.isalpha()) or key in keys:
            raise ValueError(f"Invalid or duplicate traffic key: {key!r}")
        if not 0 <= int(row.get("months", -1)) <= 12:
            raise ValueError(f"Invalid reporting-month count for {code}/{year}")
        for field in TRAFFIC_FIELDS:
            if int(row.get(field, -1)) < 0:
                raise ValueError(f"Negative {field} for {code}/{year}")
        keys.add(key)
        counts[year] = counts.get(year, 0) + 1
    for year in (2023, 2024, 2025):
        if counts.get(year, 0) < MIN_BTS_AIRPORTS_PER_YEAR:
            raise ValueError(f"Insufficient BTS airport coverage for {year}: {counts.get(year, 0)}")
    return sorted(rows, key=lambda item: (str(item["code"]), int(item["year"])))


def _validate_taf_values(values: dict[str, dict]) -> None:
    if len(values) < MIN_TAF_AIRPORTS:
        raise ValueError(f"FAA TAF coverage unexpectedly small: {len(values)}")
    for code, value in values.items():
        if not (len(code) in (3, 4) and code.isalnum()):
            raise ValueError(f"Invalid TAF airport code: {code!r}")
        if int(value.get("enplanements_2025", 0)) <= 0 or int(value.get("enplanements_2030", 0)) <= 0:
            raise ValueError(f"Non-positive TAF denominator for {code}")


def _validated_route_rows(rows: list[dict]) -> list[dict]:
    if not rows:
        raise ValueError("Route-segment candidate is empty")
    normalized = []
    seen: set[tuple] = set()
    for source_row in rows:
        row = dict(source_row)
        row["origin"] = str(row.get("origin", "")).upper()
        row["destination"] = str(row.get("destination", "")).upper()
        row["service_class"] = str(row.get("service_class", "")).upper()
        row["data_source"] = str(row.get("data_source", "")).upper()
        row["year"] = int(row.get("year", 0))
        row["distance_miles"] = int(row.get("distance_miles", 0))
        for field in ROUTE_VALUE_FIELDS:
            row[field] = int(row.get(field, -1))
        key = (row["origin"], row["destination"], row["year"], row["service_class"],
               row["data_source"], row["distance_miles"])
        if key in seen:
            raise ValueError(f"Duplicate route-segment key: {key!r}")
        if any(len(row[field]) != 3 or not row[field].isalnum()
               for field in ("origin", "destination")):
            raise ValueError(f"Invalid route airport code in {key!r}")
        if row["year"] != 2025 or row["service_class"] not in {"F", "G", "L", "P"}:
            raise ValueError(f"Invalid route period or service class in {key!r}")
        if row["data_source"] not in {"DU", "IU"} or not 0 < row["distance_miles"] <= 15_000:
            raise ValueError(f"Invalid route source or distance in {key!r}")
        if any(row[field] < 0 for field in ROUTE_VALUE_FIELDS):
            raise ValueError(f"Negative route value in {key!r}")
        seen.add(key)
        normalized.append(row)
    return sorted(normalized, key=lambda row: (
        row["origin"], row["destination"], row["year"], row["service_class"],
        row["data_source"], row["distance_miles"],
    ))


def _validated_delay_rows(rows: list[dict]) -> list[dict]:
    if not rows:
        raise ValueError("Delay candidate is empty")
    normalized = []
    seen: set[tuple[str, int, int]] = set()
    for source_row in rows:
        row = dict(source_row)
        row["code"] = str(row.get("code", "")).upper()
        row["year"], row["month"] = int(row.get("year", 0)), int(row.get("month", 0))
        for field in DELAY_VALUE_FIELDS:
            row[field] = int(row.get(field, -1))
        key = (row["code"], row["year"], row["month"])
        if key in seen or len(row["code"]) != 3 or not row["code"].isalpha():
            raise ValueError(f"Invalid or duplicate delay key: {key!r}")
        if row["year"] != 2025 or not 1 <= row["month"] <= 12:
            raise ValueError(f"Invalid delay period: {key!r}")
        if any(row[field] < 0 for field in DELAY_VALUE_FIELDS):
            raise ValueError(f"Negative delay value: {key!r}")
        if row["delayed"] + row["cancelled"] + row["diverted"] > row["flights"]:
            raise ValueError(f"Delay status counts exceed flights: {key!r}")
        seen.add(key)
        normalized.append(row)
    return sorted(normalized, key=lambda row: (row["code"], row["year"], row["month"]))


def _validated_development_rows(rows: list[dict]) -> list[dict]:
    if not rows:
        raise ValueError("NPIAS candidate is empty")
    normalized = []
    seen: set[str] = set()
    for source_row in rows:
        row = dict(source_row)
        row["locid"] = str(row.get("locid", "")).upper()
        if not 3 <= len(row["locid"]) <= 5 or not row["locid"].isalnum() or row["locid"] in seen:
            raise ValueError(f"Invalid or duplicate NPIAS LocID: {row['locid']!r}")
        for field in ("enplanements", "based_aircraft", "development_estimate"):
            row[field] = int(row.get(field, -1))
            if row[field] < 0:
                raise ValueError(f"Negative NPIAS {field} for {row['locid']}")
        for field, limit in (("state", 80), ("city", 100), ("name", 160), ("ownership", 20),
                             ("service_level", 40), ("hub_size", 40), ("role", 80)):
            row[field] = str(row.get(field, ""))[:limit]
        seen.add(row["locid"])
        normalized.append(row)
    return sorted(normalized, key=lambda row: row["locid"])


class SourceRunPublisher:
    """The only writer of versioned source facts and their active pointers."""

    def __init__(self, database: SQLiteDatabase):
        self.database = database

    def is_seeded(self) -> bool:
        """True once every required source has an active run."""
        with self.database.connect() as connection:
            rows = connection.execute("SELECT source FROM active_source_runs").fetchall()
        return REQUIRED_SOURCES.issubset({row["source"] for row in rows})

    def record_snapshot_metadata(self, version: str, extras: dict) -> None:
        """Store the bootstrap snapshot's version and its non-source sections."""
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO schema_meta(key,value) VALUES('snapshot_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (version,),
            )
            for key, value in extras.items():
                connection.execute(
                    "INSERT INTO snapshot_extras(key,value_json) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                    (key, json.dumps(value, sort_keys=True)),
                )

    def publish_airports(self, rows: list[dict], manifest: dict) -> int:
        normalized = _validated_registry_rows(rows)
        checksum = canonical_hash(normalized)
        with self.database.connect() as connection:
            run_id = self._begin_run(connection, "airport_registry", manifest, checksum, len(normalized))
            exists = connection.execute("SELECT 1 FROM airports WHERE run_id=? LIMIT 1", (run_id,)).fetchone()
            if not exists:
                connection.executemany(
                    """INSERT INTO airports
                       (run_id,code,icao,name,city,country,region,type,scheduled)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    [(
                        run_id, r["code"], r.get("icao", ""), r.get("name") or r["code"],
                        r.get("city", ""), r.get("country", ""), r.get("region", ""),
                        r.get("type", ""), int(bool(r.get("scheduled"))),
                    ) for r in normalized],
                )
            self._activate(connection, "airport_registry", run_id)
        return run_id

    def publish_traffic(self, rows: list[dict], manifest: dict) -> int:
        normalized_rows = _validated_traffic_rows(rows)
        checksum = canonical_hash(normalized_rows)
        with self.database.connect() as connection:
            run_id = self._begin_run(connection, "airport", manifest, checksum, len(rows))
            exists = connection.execute(
                "SELECT 1 FROM airport_traffic_year WHERE run_id=? LIMIT 1", (run_id,)
            ).fetchone()
            if not exists:
                connection.executemany(
                    """INSERT INTO airport_traffic_year
                       (run_id,code,year,name,city,months,passengers,seats,domestic_passengers,
                        domestic_seats,domestic_departures,international_departures)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [(
                        run_id, r["code"], int(r["year"]), r.get("name") or r["code"],
                        r.get("city", ""), int(r["months"]), int(r["passengers"]), int(r["seats"]),
                        int(r["domestic_passengers"]), int(r["domestic_seats"]),
                        int(r["domestic_departures"]), int(r["international_departures"]),
                    ) for r in normalized_rows],
                )
            self._activate(connection, "airport", run_id)
        return run_id

    def publish_taf(self, values: dict[str, dict], manifest: dict) -> int:
        _validate_taf_values(values)
        # Published values depend on the archive *and* the US registry used to
        # filter it, so the archive hash alone would reuse a stale run after a
        # registry change.
        checksum = canonical_hash({"archive_sha256": manifest.get("sha256"), "values": values})
        with self.database.connect() as connection:
            run_id = self._begin_run(connection, "faa_taf", manifest, checksum, len(values) * 2)
            exists = connection.execute(
                "SELECT 1 FROM taf_enplanements WHERE run_id=? LIMIT 1", (run_id,)
            ).fetchone()
            if not exists:
                records = []
                for code, value in values.items():
                    records.extend([
                        (run_id, code, 2025, 1, int(value["enplanements_2025"])),
                        (run_id, code, 2030, 1, int(value["enplanements_2030"])),
                    ])
                connection.executemany(
                    "INSERT INTO taf_enplanements(run_id,code,fiscal_year,scenario,enplanements) "
                    "VALUES(?,?,?,?,?)", records,
                )
            self._activate(connection, "faa_taf", run_id)
        return run_id

    def publish_route_segments(self, rows: list[dict], manifest: dict) -> int:
        normalized = _validated_route_rows(rows)
        checksum = canonical_hash(normalized)
        with self.database.connect() as connection:
            run_id = self._begin_run(connection, "bts_t100_segment", manifest, checksum, len(normalized))
            exists = connection.execute(
                "SELECT 1 FROM route_segment_year WHERE run_id=? LIMIT 1", (run_id,)
            ).fetchone()
            if not exists:
                connection.executemany(
                    """INSERT INTO route_segment_year
                       (run_id,origin,destination,year,service_class,data_source,distance_miles,
                        departures,seats,passengers,freight_pounds)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    [(run_id, row["origin"], row["destination"], row["year"],
                      row["service_class"], row["data_source"], row["distance_miles"],
                      row["departures"], row["seats"], row["passengers"], row["freight_pounds"])
                     for row in normalized],
                )
            self._activate(connection, "bts_t100_segment", run_id)
        return run_id

    def publish_delay_months(self, rows: list[dict], manifest: dict) -> int:
        normalized = _validated_delay_rows(rows)
        checksum = canonical_hash(normalized)
        with self.database.connect() as connection:
            run_id = self._begin_run(connection, "bts_delay_causes", manifest, checksum, len(normalized))
            exists = connection.execute(
                "SELECT 1 FROM airport_delay_month WHERE run_id=? LIMIT 1", (run_id,)
            ).fetchone()
            if not exists:
                connection.executemany(
                    """INSERT INTO airport_delay_month
                       (run_id,code,year,month,flights,delayed,cancelled,diverted,
                        arrival_delay_minutes,carrier_delay_minutes,weather_delay_minutes,
                        nas_delay_minutes,security_delay_minutes,late_aircraft_delay_minutes)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [(run_id, row["code"], row["year"], row["month"],
                      *(row[field] for field in DELAY_VALUE_FIELDS)) for row in normalized],
                )
            self._activate(connection, "bts_delay_causes", run_id)
        return run_id

    def publish_development_plans(self, rows: list[dict], manifest: dict) -> int:
        normalized = _validated_development_rows(rows)
        checksum = canonical_hash(normalized)
        with self.database.connect() as connection:
            run_id = self._begin_run(connection, "faa_npias", manifest, checksum, len(normalized))
            exists = connection.execute(
                "SELECT 1 FROM airport_development_plan WHERE run_id=? LIMIT 1", (run_id,)
            ).fetchone()
            if not exists:
                connection.executemany(
                    """INSERT INTO airport_development_plan
                       (run_id,locid,state,city,name,ownership,service_level,hub_size,role,
                        enplanements,based_aircraft,development_estimate)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [(run_id, row["locid"], row["state"], row["city"], row["name"],
                      row["ownership"], row["service_level"], row["hub_size"], row["role"],
                      row["enplanements"], row["based_aircraft"], row["development_estimate"])
                     for row in normalized],
                )
            self._activate(connection, "faa_npias", run_id)
        return run_id

    @staticmethod
    def _begin_run(
        connection: sqlite3.Connection, source: str, manifest: dict,
        checksum: str, row_count: int,
    ) -> int:
        """Reuse the run with identical content, or insert a new candidate run."""
        existing = connection.execute(
            "SELECT id FROM source_runs WHERE source=? AND checksum=?", (source, checksum)
        ).fetchone()
        if existing:
            connection.execute(
                "UPDATE source_runs SET last_checked_at=? WHERE id=?", (utc_now(), existing["id"])
            )
            return int(existing["id"])
        cursor = connection.execute(
            """INSERT INTO source_runs
               (source,status,retrieved_at,last_checked_at,period,url,checksum,row_count,manifest_json)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                source, "candidate", manifest.get("retrieved_at") or utc_now(), utc_now(),
                manifest.get("period") or manifest.get("fiscal_period"),
                manifest.get("url") or manifest.get("download") or "bundled:snapshot.json",
                checksum, row_count, json.dumps(manifest, sort_keys=True),
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _activate(connection: sqlite3.Connection, source: str, run_id: int) -> None:
        """Point ``source`` at ``run_id``, supersede the old run, prune history."""
        previous = connection.execute(
            "SELECT run_id FROM active_source_runs WHERE source=?", (source,)
        ).fetchone()
        connection.execute(
            "INSERT INTO active_source_runs(source,run_id) VALUES(?,?) "
            "ON CONFLICT(source) DO UPDATE SET run_id=excluded.run_id",
            (source, run_id),
        )
        connection.execute("UPDATE source_runs SET status='active', error_code=NULL WHERE id=?", (run_id,))
        if previous and int(previous["run_id"]) != run_id:
            connection.execute("UPDATE source_runs SET status='superseded' WHERE id=?", (previous["run_id"],))
        connection.execute(
            """DELETE FROM source_runs WHERE source=? AND id<>? AND id NOT IN (
                   SELECT id FROM source_runs WHERE source=? AND status='superseded'
                   ORDER BY id DESC LIMIT ?)""",
            (source, run_id, source, KEEP_SUPERSEDED_RUNS),
        )

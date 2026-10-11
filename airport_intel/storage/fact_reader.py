"""Read side: typed fact bundles from the active source runs.

``FactReader`` turns SQL over the ``active_*`` views into the plain dict
shape the deterministic analytics already consume (``airport_registry``,
``airport_years``, ``faa_taf`` plus optional extras). Each bundle is read in
one transaction, so it reflects exactly one published version. User/model
text never becomes SQL structure; values are bound parameters.

``FactProvider`` is the runtime ``FactSource`` (see ``airport_intel.facts``):
it caches the small resolver catalogue and the health coverage view by active
version, and passes plan-scoped reads straight through.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import TYPE_CHECKING

from .database import SQLiteDatabase

if TYPE_CHECKING:
    from ..facts import FactBundle

ANALYSIS_OPERATIONS = frozenset({
    "rank_airports", "compare_airports", "longhaul_share", "assess_demand_pressure", "revenue_context",
})
# Legacy optional snapshot section; normalized public-source tables own the
# delay, route and NPIAS families.
_EXTRAS_BY_OPERATION = {
    "revenue_context": "cats_revenue",
}
_REGISTRY_COLUMNS = ("code", "icao", "name", "city", "country", "region", "type", "scheduled")
_TRAFFIC_COLUMNS = ("code", "year", "name", "city", "months", "passengers", "seats",
                    "domestic_passengers", "domestic_seats", "domestic_departures",
                    "international_departures")
_REGISTRY_SELECT = "SELECT " + ",".join(_REGISTRY_COLUMNS) + " FROM active_airports"
_TRAFFIC_SELECT = "SELECT " + ",".join(_TRAFFIC_COLUMNS) + " FROM active_airport_traffic_year"
_US_TRAFFIC_SELECT = (
    "SELECT " + ",".join(f"t.{column}" for column in _TRAFFIC_COLUMNS)
    + " FROM active_airport_traffic_year t JOIN active_airports a ON a.code=t.code AND a.country='US'"
)

VersionToken = tuple[tuple[str, int], ...]


def _registry_rows(connection: sqlite3.Connection, sql: str, parameters: tuple = ()) -> list[dict]:
    rows = [dict(row) for row in connection.execute(sql, parameters)]
    for row in rows:
        row["scheduled"] = bool(row["scheduled"])
    return rows


def _group_taf(rows) -> dict[str, dict]:
    taf: dict[str, dict] = {}
    for row in rows:
        taf.setdefault(row["code"], {})[f"enplanements_{row['fiscal_year']}"] = row["enplanements"]
    return taf


def _with_change_pct(taf: dict[str, dict]) -> dict[str, dict]:
    for value in taf.values():
        value["change_pct"] = round((value["enplanements_2030"] / value["enplanements_2025"] - 1) * 100, 2)
    return taf


def _placeholders(values) -> str:
    return ",".join("?" for _ in values)


class FactReader:
    """Read-only queries over the active source runs."""

    def __init__(self, database: SQLiteDatabase):
        self.database = database

    def active_version_token(self) -> VersionToken:
        """Changes whenever any source's active run changes; used as a cache key."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT source,run_id FROM active_source_runs ORDER BY source"
            ).fetchall()
        return tuple((row["source"], int(row["run_id"])) for row in rows)

    def identity_catalogue(self) -> FactBundle:
        """Load only the resolver catalogue plus 2025 traffic for tie-breaking.

        This is intentionally smaller than an analytics dataset: planning and
        entity resolution need airport identity, but not three years of every
        airport's facts. Numeric work is loaded separately after the plan is
        validated.
        """
        with self.database.read_transaction() as connection:
            registry = _registry_rows(connection, _REGISTRY_SELECT + " ORDER BY code")
            traffic = [dict(row) for row in connection.execute(
                _US_TRAFFIC_SELECT + " WHERE t.year=2025 ORDER BY t.code"
            )]
        return {"version": "sqlite-v3", "airport_registry": registry, "airport_years": traffic}

    def analysis_facts(self, operations: set[str], codes: set[str] | None = None) -> FactBundle:
        """Return the minimum typed fact bundle required by an allowlisted plan.

        Ranking needs the national population because score percentiles are
        defined against national size cohorts. Airport comparisons and demand
        questions query only the resolved codes.
        """
        if not operations or not operations.issubset(ANALYSIS_OPERATIONS):
            raise ValueError("Unsupported repository operation set")
        selected = sorted(codes or set())
        with self.database.read_transaction() as connection:
            if "rank_airports" in operations:
                registry = _registry_rows(connection, _REGISTRY_SELECT + " WHERE country='US' ORDER BY code")
                traffic = [dict(row) for row in connection.execute(_US_TRAFFIC_SELECT + " ORDER BY t.code,t.year")]
            elif selected:
                marks = _placeholders(selected)
                registry = _registry_rows(
                    connection, _REGISTRY_SELECT + f" WHERE code IN ({marks}) ORDER BY code", tuple(selected))
                traffic = [dict(row) for row in connection.execute(
                    _TRAFFIC_SELECT + f" WHERE code IN ({marks}) ORDER BY code,year", tuple(selected))]
            else:
                registry, traffic = [], []

            taf: dict[str, dict] = {}
            if selected and operations.intersection({"assess_demand_pressure", "revenue_context"}):
                taf = _with_change_pct(_group_taf(connection.execute(
                    f"SELECT code,fiscal_year,enplanements FROM active_taf_enplanements "
                    f"WHERE code IN ({_placeholders(selected)}) ORDER BY code,fiscal_year",
                    tuple(selected),
                ).fetchall()))

            delay_rows = []
            if selected and "compare_airports" in operations:
                marks = _placeholders(selected)
                delay_rows = [dict(row) for row in connection.execute(
                    f"""SELECT code,year,month,flights,delayed,cancelled,diverted,
                               arrival_delay_minutes,carrier_delay_minutes,weather_delay_minutes,
                               nas_delay_minutes,security_delay_minutes,late_aircraft_delay_minutes
                        FROM active_airport_delay_month WHERE code IN ({marks})
                        ORDER BY code,year,month""",
                    tuple(selected),
                )]

            route_segments = []
            if selected and "longhaul_share" in operations:
                marks = _placeholders(selected)
                route_segments = [dict(row) for row in connection.execute(
                    f"""SELECT origin,destination,year,service_class,data_source,distance_miles,
                               departures,seats,passengers,freight_pounds
                        FROM active_route_segment_year WHERE origin IN ({marks})
                        ORDER BY origin,destination,service_class,data_source,distance_miles""",
                    tuple(selected),
                )]

            npias_development = {}
            if selected and operations.intersection({"assess_demand_pressure", "revenue_context"}):
                marks = _placeholders(selected)
                npias_development = {
                    row["locid"]: dict(row) for row in connection.execute(
                        f"""SELECT locid,state,city,name,ownership,service_level,hub_size,role,
                                   enplanements,based_aircraft,development_estimate
                            FROM active_airport_development_plan WHERE locid IN ({marks})
                            ORDER BY locid""",
                        tuple(selected),
                    )
                }

            requested_extras = sorted({_EXTRAS_BY_OPERATION[op] for op in operations if op in _EXTRAS_BY_OPERATION})
            extras = {}
            if requested_extras:
                extras = {
                    row["key"]: json.loads(row["value_json"])
                    for row in connection.execute(
                        f"SELECT key,value_json FROM snapshot_extras WHERE key IN ({_placeholders(requested_extras)})",
                        tuple(requested_extras),
                    )
                }
        return {
            "version": "sqlite-v3", "airport_registry": registry,
            "airport_years": traffic, "faa_taf": taf, **extras,
            "delay_rows": delay_rows, "route_segments": route_segments,
            "npias_development": npias_development,
        }

    def full_snapshot(self) -> FactBundle:
        """Materialize every current validated run as one dict.

        Used for audits, fixtures and parity tests only. The runtime answer
        path never calls it: questions use ``identity_catalogue`` plus the
        plan-scoped ``analysis_facts``.
        """
        with self.database.read_transaction() as connection:
            registry = _registry_rows(connection, _REGISTRY_SELECT + " ORDER BY code")
            traffic = [dict(row) for row in connection.execute(_TRAFFIC_SELECT + " ORDER BY code,year")]
            taf_rows = connection.execute(
                "SELECT code,fiscal_year,enplanements FROM active_taf_enplanements ORDER BY code,fiscal_year"
            ).fetchall()
            manifests = {}
            for row in connection.execute(
                """SELECT r.source,r.retrieved_at,r.period,r.url,r.checksum,r.row_count,r.manifest_json
                   FROM source_runs r JOIN active_source_runs a ON a.run_id=r.id ORDER BY r.source"""
            ):
                manifest = json.loads(row["manifest_json"])
                manifest.update({
                    "retrieved_at": row["retrieved_at"], "period": row["period"],
                    "url": row["url"], "sha256": row["checksum"], "row_count": row["row_count"],
                })
                manifests[row["source"]] = manifest
            extras = {
                row["key"]: json.loads(row["value_json"])
                for row in connection.execute("SELECT key,value_json FROM snapshot_extras")
            }
            meta = connection.execute(
                "SELECT value FROM schema_meta WHERE key='snapshot_version'"
            ).fetchone()
            delay_rows = [dict(row) for row in connection.execute(
                """SELECT code,year,month,flights,delayed,cancelled,diverted,
                          arrival_delay_minutes,carrier_delay_minutes,weather_delay_minutes,
                          nas_delay_minutes,security_delay_minutes,late_aircraft_delay_minutes
                   FROM active_airport_delay_month ORDER BY code,year,month"""
            )]
            route_rows = [dict(row) for row in connection.execute(
                """SELECT origin,destination,year,service_class,data_source,distance_miles,
                          departures,seats,passengers,freight_pounds
                   FROM active_route_segment_year
                   ORDER BY origin,destination,service_class,data_source,distance_miles"""
            )]
            development = {
                row["locid"]: dict(row) for row in connection.execute(
                    """SELECT locid,state,city,name,ownership,service_level,hub_size,role,
                              enplanements,based_aircraft,development_estimate
                       FROM active_airport_development_plan ORDER BY locid"""
                )
            }
        return {
            "version": meta["value"] if meta else "sqlite-v1",
            "retrieved_at": max((m.get("retrieved_at", "") for m in manifests.values()), default=""),
            "sources": manifests, "airport_registry": registry, "airport_years": traffic,
            "faa_taf": _with_change_pct(_group_taf(taf_rows)), **extras,
            "delay_rows": delay_rows, "route_segments": route_rows,
            "npias_development": development,
        }

    def taf_outlook(self) -> dict[str, dict]:
        """All active FAA TAF pairs; small (~1.4k rows), used for coverage counts."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT code,fiscal_year,enplanements FROM active_taf_enplanements ORDER BY code,fiscal_year"
            ).fetchall()
        return {code: value for code, value in _group_taf(rows).items()
                if "enplanements_2025" in value and "enplanements_2030" in value}

    def active_us_codes(self) -> set[str]:
        with self.database.connect() as connection:
            return {row["code"] for row in connection.execute(
                "SELECT code FROM active_airports WHERE country='US'"
            )}

    def search_airports(self, query: str, limit: int = 10) -> list[dict]:
        """Bounded US-only search by code, ICAO, name or city; wildcards are literal."""
        needle = query.strip()
        if not needle or len(needle) > 100:
            return []
        limit = max(1, min(int(limit), 25))
        escaped = needle.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT code,icao,name,city,country,region,scheduled
                   FROM active_airports
                   WHERE country='US' AND (lower(code)=lower(?) OR lower(icao)=lower(?)
                      OR lower(name) LIKE ? ESCAPE '\\' OR lower(city) LIKE ? ESCAPE '\\')
                   ORDER BY CASE WHEN lower(code)=lower(?) THEN 0 ELSE 1 END,
                            scheduled DESC, code LIMIT ?""",
                (needle, needle, pattern, pattern, needle, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def source_status(self) -> dict:
        """Active run metadata per source, for /health and /api/capabilities."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT r.source,r.retrieved_at,r.last_checked_at,r.period,r.row_count,r.checksum
                   FROM source_runs r JOIN active_source_runs a ON a.run_id=r.id
                   ORDER BY r.source"""
            ).fetchall()
        return {row["source"]: dict(row) for row in rows}

    def installed_coverage(self) -> dict:
        """Small SQL aggregates for capability reporting; never loads route facts."""
        with self.database.connect() as connection:
            delay = connection.execute(
                """SELECT COUNT(DISTINCT d.code),MIN(d.year),MAX(d.year)
                   FROM active_airport_delay_month d
                   JOIN active_airports a ON a.code=d.code AND a.country='US'"""
            ).fetchone()
            routes = connection.execute(
                """SELECT COUNT(DISTINCT r.origin),MIN(r.year),MAX(r.year)
                   FROM active_route_segment_year r
                   JOIN active_airports a ON a.code=r.origin AND a.country='US'
                   WHERE r.service_class='F' AND r.departures>0 AND r.seats>0"""
            ).fetchone()
            development = connection.execute(
                """SELECT COUNT(*),SUM(d.development_estimate>0)
                   FROM active_airport_development_plan d
                   JOIN active_airports a ON a.code=d.locid AND a.country='US'"""
            ).fetchone()
        return {
            "congestion": {"covered_airports": int(delay[0]), "min_year": delay[1], "max_year": delay[2]},
            "longhaul_share": {"covered_airports": int(routes[0]), "min_year": routes[1], "max_year": routes[2]},
            "development_context": {
                "covered_airports": int(development[0]),
                "positive_development_estimates": int(development[1] or 0),
            },
        }


class FactProvider:
    """Version-aware cache over ``FactReader``; the runtime ``FactSource``.

    Only the resolver catalogue and the health coverage view are cached, and
    both are reloaded as soon as any active source pointer changes. Answer
    facts are never cached: each request reads its own scoped bundle.
    """

    def __init__(self, reader: FactReader):
        self.reader = reader
        self._lock = threading.Lock()
        self._identity_token: VersionToken | None = None
        self._identity: dict | None = None
        self._coverage_token: VersionToken | None = None
        self._coverage: dict | None = None

    def identity_catalogue(self) -> FactBundle:
        token = self.reader.active_version_token()
        with self._lock:
            if self._identity is None or token != self._identity_token:
                self._identity = self.reader.identity_catalogue()
                self._identity_token = token
            return self._identity

    def for_analysis(self, operations: set[str], codes: set[str] | None = None) -> FactBundle:
        return self.reader.analysis_facts(operations, codes)

    def for_coverage(self) -> FactBundle:
        """Cached national facts used only by health/capability reporting."""
        token = self.reader.active_version_token()
        with self._lock:
            if self._coverage is None or token != self._coverage_token:
                self._coverage = {
                    **self.reader.analysis_facts({"rank_airports"}),
                    "faa_taf": self.reader.taf_outlook(),
                    "installed_coverage": self.reader.installed_coverage(),
                }
                self._coverage_token = token
            return self._coverage

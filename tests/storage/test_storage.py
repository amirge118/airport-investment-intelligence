"""Real-SQLite tests for the storage package: schema, bootstrap, publish, read.

Write side (``SourceRunPublisher``), read side (``FactReader``/``FactProvider``)
and first-boot seeding (``bootstrap``) are exercised against one real file.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
import unittest
from pathlib import Path
from unittest import mock

from airport_intel.analytics.coverage import airport_preview, coverage_report
from airport_intel.storage import (
    FactProvider, SQLiteDatabase, SourceRunPublisher, seed_from_snapshot,
)
from airport_intel.storage import bootstrap
from tests.sqlite_fixture import SNAPSHOT_PATH, SeededDatabaseTestCase


class SeedAndReadTests(SeededDatabaseTestCase):
    def test_first_boot_reconciles_source_counts_and_scopes_reads(self):
        full = self.reader.full_snapshot()
        self.assertEqual(len(full["airport_registry"]), 5_332)
        self.assertEqual(len(full["airport_years"]), 3_630)
        self.assertEqual(len(full["faa_taf"]), 712)

        identity = self.reader.identity_catalogue()
        self.assertEqual(len(identity["airport_registry"]), 5_332)
        self.assertTrue(identity["airport_years"])
        self.assertTrue(all(row["year"] == 2025 for row in identity["airport_years"]))
        self.assertLess(len(identity["airport_years"]), len(full["airport_years"]))

        selected = self.reader.analysis_facts({"assess_demand_pressure"}, {"SFO"})
        self.assertEqual({row["code"] for row in selected["airport_years"]}, {"SFO"})
        self.assertEqual({row["year"] for row in selected["airport_years"]}, {2023, 2024, 2025})
        self.assertEqual(selected["faa_taf"]["SFO"]["enplanements_2030"], 30_516_426)

        ranking = self.reader.analysis_facts({"rank_airports"})
        us_codes = {row["code"] for row in full["airport_registry"] if row["country"] == "US"}
        expected_us_traffic = sum(row["code"] in us_codes for row in full["airport_years"])
        self.assertEqual(len(ranking["airport_years"]), expected_us_traffic)
        self.assertTrue(all(row["country"] == "US" for row in ranking["airport_registry"]))

    def test_unknown_operation_set_is_rejected_before_any_query(self):
        with self.assertRaises(ValueError):
            self.reader.analysis_facts({"drop_table"})
        with self.assertRaises(ValueError):
            self.reader.analysis_facts(set())

    def test_repeated_seed_is_idempotent(self):
        token = self.reader.active_version_token()
        self.assertFalse(seed_from_snapshot(self.publisher, SNAPSHOT_PATH))
        self.assertEqual(self.reader.active_version_token(), token)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0], 3)

    def test_validated_new_run_becomes_visible_without_restart(self):
        provider = FactProvider(self.reader)
        before = provider.for_analysis({"assess_demand_pressure"}, {"SFO"})
        rows = copy.deepcopy(self.source_snapshot["airport_years"])
        target = next(row for row in rows if row["code"] == "SFO" and row["year"] == 2025)
        target["passengers"] += 1_000
        self.publisher.publish_traffic(rows, {
            "url": "https://data.bts.gov/d/r495-tyji",
            "retrieved_at": "2026-10-11T12:00:00+00:00",
            "period": "2023-2025",
        })
        after = provider.for_analysis({"assess_demand_pressure"}, {"SFO"})
        before_value = next(row["passengers"] for row in before["airport_years"] if row["year"] == 2025)
        after_value = next(row["passengers"] for row in after["airport_years"] if row["year"] == 2025)
        self.assertEqual(after_value, before_value + 1_000)

    def test_identity_cache_reloads_only_when_active_version_changes(self):
        provider = FactProvider(self.reader)
        first = provider.identity_catalogue()
        self.assertIs(provider.identity_catalogue(), first)
        rows = copy.deepcopy(self.source_snapshot["airport_years"])
        next(row for row in rows if row["code"] == "SFO" and row["year"] == 2025)["passengers"] += 1
        self.publisher.publish_traffic(rows, {"url": "https://data.bts.gov/d/r495-tyji"})
        self.assertIsNot(provider.identity_catalogue(), first)

    def test_rejected_candidate_keeps_last_good_active_version(self):
        token = self.reader.active_version_token()
        with self.assertRaises(ValueError):
            self.publisher.publish_traffic(
                self.source_snapshot["airport_years"][:10],
                {"url": "https://data.bts.gov/d/r495-tyji"},
            )
        self.assertEqual(self.reader.active_version_token(), token)

    def test_reader_sees_old_or_new_complete_version_during_publish(self):
        provider = FactProvider(self.reader)
        old_value = next(
            row["passengers"]
            for row in provider.for_analysis({"assess_demand_pressure"}, {"SFO"})["airport_years"]
            if row["year"] == 2025
        )
        rows = copy.deepcopy(self.source_snapshot["airport_years"])
        next(row for row in rows if row["code"] == "SFO" and row["year"] == 2025)["passengers"] += 2_000
        entered, release = threading.Event(), threading.Event()
        original_activate = SourceRunPublisher._activate

        def pause_then_activate(connection, source, run_id):
            entered.set()
            release.wait(5)
            return original_activate(connection, source, run_id)

        with mock.patch.object(SourceRunPublisher, "_activate", side_effect=pause_then_activate):
            worker = threading.Thread(
                target=self.publisher.publish_traffic,
                args=(rows, {"url": "https://data.bts.gov/d/r495-tyji", "period": "2023-2025"}),
            )
            worker.start()
            self.assertTrue(entered.wait(5))
            during = provider.for_analysis({"assess_demand_pressure"}, {"SFO"})
            during_value = next(row["passengers"] for row in during["airport_years"] if row["year"] == 2025)
            self.assertEqual(during_value, old_value)
            release.set()
            worker.join(10)
            self.assertFalse(worker.is_alive())
        after = provider.for_analysis({"assess_demand_pressure"}, {"SFO"})
        after_value = next(row["passengers"] for row in after["airport_years"] if row["year"] == 2025)
        self.assertEqual(after_value, old_value + 2_000)

    def test_search_is_parameterized_and_query_plan_uses_key_index(self):
        self.assertEqual(self.reader.search_airports("SFO' OR 1=1 --"), [])
        self.assertEqual(self.reader.search_airports("SFO")[0]["code"], "SFO")
        with self.database.connect() as connection:
            run_id = connection.execute(
                "SELECT run_id FROM active_source_runs WHERE source='airport'"
            ).fetchone()[0]
        plan = self.database.query_plan(
            "SELECT passengers FROM airport_traffic_year WHERE run_id=? AND code=? AND year=?",
            (run_id, "SFO", 2025),
        )
        self.assertTrue(any("INDEX" in line.upper() for line in plan), plan)

    def test_airport_preview_is_us_only_and_reports_real_coverage(self):
        facts = FactProvider(self.reader)
        preview = airport_preview(facts, "SFO")
        self.assertEqual(preview["airport"]["code"], "SFO")
        self.assertEqual([row["year"] for row in preview["traffic"]], [2023, 2024, 2025])
        self.assertTrue(preview["capabilities"]["traffic_growth"]["available"])
        self.assertTrue(preview["capabilities"]["forward_outlook"]["available"])
        self.assertFalse(preview["capabilities"]["congestion"]["available"])
        self.assertIsNone(airport_preview(facts, "LHR"))
        self.assertIsNone(airport_preview(facts, "SFO' OR 1=1 --"))

    def test_search_treats_like_wildcards_as_literal_text(self):
        self.assertEqual(self.reader.search_airports("%"), [])
        self.assertEqual(self.reader.search_airports("_"), [])
        self.assertTrue(self.reader.search_airports("San Francisco"))

    def test_coverage_counts_faa_forecast_airports_from_database(self):
        report = coverage_report(FactProvider(self.reader).for_coverage())
        self.assertEqual(report["demand_pressure"]["forecast_covered_airports"], 712)
        self.assertEqual(report["opportunity_score"]["covered_airports"], 226)

    # -- first-boot recovery and freshness --

    def test_bundled_sources_keep_snapshot_retrieval_time_not_first_boot_time(self):
        status = self.reader.source_status()
        for source in ("airport", "airport_registry"):
            self.assertEqual(status[source]["retrieved_at"], self.source_snapshot["retrieved_at"], source)
        self.assertEqual(status["faa_taf"]["retrieved_at"], self.source_snapshot["sources"]["faa_taf"]["retrieved_at"])

    def test_partial_first_boot_recovers_without_missing_extras(self):
        database, publisher, reader = self.open_database("partial.sqlite3")
        with mock.patch.object(SourceRunPublisher, "publish_taf", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                seed_from_snapshot(publisher, SNAPSHOT_PATH)
        self.assertFalse(publisher.is_seeded())
        self.assertTrue(seed_from_snapshot(publisher, SNAPSHOT_PATH))
        self.assertTrue(publisher.is_seeded())
        self.assertEqual(len(reader.full_snapshot()["faa_taf"]), 712)
        with database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0], 3)

    def test_crash_after_last_source_keeps_snapshot_metadata_and_extras(self):
        _, publisher, reader = self.open_database("late-crash.sqlite3")
        original = SourceRunPublisher.publish_taf

        def publish_then_crash(self_, values, manifest):
            original(self_, values, manifest)
            raise RuntimeError("crash after last source activation")

        with mock.patch.object(SourceRunPublisher, "publish_taf", publish_then_crash):
            with self.assertRaises(RuntimeError):
                seed_from_snapshot(publisher, SNAPSHOT_PATH)
        # Sources are active, so the DB is servable; metadata was written first.
        self.assertTrue(publisher.is_seeded())
        self.assertEqual(reader.full_snapshot()["version"], self.source_snapshot["version"])

    def test_v1_development_database_is_migrated_in_place(self):
        path = Path(self.tempdir.name) / "v1.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute(
            """CREATE TABLE source_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, status TEXT NOT NULL,
                retrieved_at TEXT NOT NULL, period TEXT, url TEXT NOT NULL, checksum TEXT NOT NULL,
                row_count INTEGER NOT NULL, manifest_json TEXT NOT NULL, error_code TEXT,
                UNIQUE(source, checksum))"""
        )
        connection.execute(
            "INSERT INTO source_runs(source,status,retrieved_at,url,checksum,row_count,manifest_json) "
            "VALUES('airport','active','2026-01-01T00:00:00+00:00','u','c',1,'{}')"
        )
        connection.commit()
        connection.close()
        database = SQLiteDatabase(path)
        with database.connect() as conn:
            row = conn.execute("SELECT last_checked_at FROM source_runs").fetchone()
        self.assertEqual(row[0], "2026-01-01T00:00:00+00:00")
        self.assertTrue(seed_from_snapshot(SourceRunPublisher(database), SNAPSHOT_PATH))

    # -- version history, read consistency, legacy timestamps --

    def _sfo_2025(self, bundle: dict) -> int:
        return next(r["passengers"] for r in bundle["airport_years"] if r["code"] == "SFO" and r["year"] == 2025)

    def test_superseded_runs_are_pruned_without_touching_active_data(self):
        for bump in (1, 2, 3):
            rows = copy.deepcopy(self.source_snapshot["airport_years"])
            next(r for r in rows if r["code"] == "SFO" and r["year"] == 2025)["passengers"] += bump
            self.publisher.publish_traffic(rows, {"url": "https://data.bts.gov/d/r495-tyji"})
        with self.database.connect() as connection:
            statuses = [row[0] for row in connection.execute(
                "SELECT status FROM source_runs WHERE source='airport' ORDER BY id")]
            traffic_rows = connection.execute("SELECT COUNT(*) FROM airport_traffic_year").fetchone()[0]
        self.assertEqual(statuses, ["superseded", "active"])
        self.assertEqual(traffic_rows, 2 * len(self.source_snapshot["airport_years"]))
        base = next(r["passengers"] for r in self.source_snapshot["airport_years"]
                    if r["code"] == "SFO" and r["year"] == 2025)
        bundle = self.reader.analysis_facts({"assess_demand_pressure"}, {"SFO"})
        self.assertEqual(self._sfo_2025(bundle), base + 3)

    def test_taf_run_identity_covers_published_values_not_only_archive_hash(self):
        values = copy.deepcopy(self.source_snapshot["faa_taf"])
        archive = self.source_snapshot["sources"]["faa_taf"]["sha256"]
        values["SFO"]["enplanements_2030"] += 10
        self.publisher.publish_taf(values, {"url": "https://www.faa.gov/data_research/aviation/taf",
                                            "sha256": archive, "fiscal_period": "FY2025-FY2030"})
        bundle = self.reader.analysis_facts({"assess_demand_pressure"}, {"SFO"})
        self.assertEqual(bundle["faa_taf"]["SFO"]["enplanements_2030"], 30_516_436)

    def test_fact_bundle_reads_one_version_even_if_a_publish_lands_mid_read(self):
        before = self._sfo_2025(self.reader.analysis_facts({"assess_demand_pressure"}, {"SFO"}))
        rows = copy.deepcopy(self.source_snapshot["airport_years"])
        next(r for r in rows if r["code"] == "SFO" and r["year"] == 2025)["passengers"] += 5_000
        publisher, original_connect, state = self.publisher, SQLiteDatabase.connect, {"done": False}

        def connect_with_mid_read_publish(self_):
            connection = original_connect(self_)
            real_execute = connection.execute

            def execute(sql, *args):
                cursor = real_execute(sql, *args)
                if not state["done"] and "FROM active_airports" in sql:
                    state["done"] = True  # publish after the registry read, before traffic
                    worker = threading.Thread(target=publisher.publish_traffic,
                                              args=(rows, {"url": "https://data.bts.gov/d/r495-tyji"}))
                    worker.start()
                    worker.join(10)
                return cursor

            connection.execute = execute
            return connection

        with mock.patch.object(SQLiteDatabase, "connect", connect_with_mid_read_publish):
            during = self.reader.analysis_facts({"assess_demand_pressure"}, {"SFO"})
        self.assertTrue(state["done"])
        self.assertEqual(self._sfo_2025(during), before)
        after = self.reader.analysis_facts({"assess_demand_pressure"}, {"SFO"})
        self.assertEqual(self._sfo_2025(after), before + 5_000)

    def test_legacy_first_boot_timestamps_are_repaired_on_next_start(self):
        with self.database.connect() as connection:
            for source in ("airport", "airport_registry"):
                row = connection.execute(
                    "SELECT r.id, r.manifest_json FROM source_runs r JOIN active_source_runs a "
                    "ON a.run_id=r.id WHERE r.source=?", (source,)).fetchone()
                legacy = {k: v for k, v in json.loads(row[1]).items()
                          if k not in ("retrieved_at", "bootstrap", "retrieved_at_basis")}
                connection.execute("UPDATE source_runs SET retrieved_at=?, manifest_json=? WHERE id=?",
                                   ("2026-10-11T12:08:25+00:00", json.dumps(legacy), row[0]))
        self.assertFalse(seed_from_snapshot(self.publisher, SNAPSHOT_PATH))
        status = self.reader.source_status()
        for source in ("airport", "airport_registry"):
            self.assertEqual(status[source]["retrieved_at"], self.source_snapshot["retrieved_at"])
        manifest = self.reader.full_snapshot()["sources"]["airport_registry"]
        self.assertEqual(manifest["retrieved_at_basis"], bootstrap.BUNDLED_RETRIEVAL_BASIS)
        # A run whose manifest records its own retrieval time is never rewritten.
        self.assertEqual(status["faa_taf"]["retrieved_at"],
                         self.source_snapshot["sources"]["faa_taf"]["retrieved_at"])


if __name__ == "__main__":
    unittest.main()

"""Startup source refresh (``airport_intel.ingestion.refresh``) with mocked public sources."""

from __future__ import annotations

import copy
import json
import unittest
from unittest import mock

from airport_intel.ingestion import live_sources
from airport_intel.ingestion.refresh import RefreshCoordinator
from tests.sqlite_fixture import SeededDatabaseTestCase


class RefreshCoordinatorTests(SeededDatabaseTestCase):
    def _coordinator(self) -> RefreshCoordinator:
        return RefreshCoordinator(self.publisher, self.reader)

    def test_offline_mode_never_calls_public_sources(self):
        coordinator = self._coordinator()
        with mock.patch.object(live_sources, "fetch_global_registry") as registry_fetch, \
             mock.patch.object(live_sources, "fetch_bts_airport_year") as traffic_fetch, \
             mock.patch.object(live_sources, "fetch_and_build_faa_taf") as taf_fetch:
            coordinator.start(offline=True)
        registry_fetch.assert_not_called()
        traffic_fetch.assert_not_called()
        taf_fetch.assert_not_called()
        self.assertTrue(all(item["state"] == "offline" for item in coordinator.status().values()))

    def test_background_refresh_success_publishes_new_run_and_marks_fresh(self):
        before = self.reader.active_version_token()
        traffic = copy.deepcopy(self.source_snapshot["airport_years"])
        next(row for row in traffic if row["code"] == "BOS" and row["year"] == 2025)["passengers"] += 7
        registry = copy.deepcopy(self.source_snapshot["airport_registry"])
        taf = copy.deepcopy(self.source_snapshot["faa_taf"])
        us_codes_seen = []

        def fake_taf(us_codes):
            us_codes_seen.append(len(us_codes))
            return taf, {"registry_pairs": 795, "positive_pairs": 712}, b"raw"

        coordinator = self._coordinator()
        with mock.patch.object(live_sources, "fetch_global_registry", return_value=registry), \
             mock.patch.object(live_sources, "fetch_bts_airport_year",
                               side_effect=lambda year: [r for r in traffic if r["year"] == year]), \
             mock.patch.object(live_sources, "fetch_and_build_faa_taf", side_effect=fake_taf):
            coordinator.start(offline=False)
            self.assertTrue(coordinator.wait(30))
        self.assertEqual(us_codes_seen, [1005])
        self.assertTrue(all(item["state"] == "fresh" for item in coordinator.status().values()), coordinator.status())
        after = self.reader.active_version_token()
        self.assertNotEqual(dict(before)["airport"], dict(after)["airport"])
        self.assertEqual(dict(before)["airport_registry"], dict(after)["airport_registry"])  # identical content reused
        bos = self.reader.analysis_facts({"assess_demand_pressure"}, {"BOS"})
        self.assertEqual(next(r["passengers"] for r in bos["airport_years"] if r["year"] == 2025),
                         next(r["passengers"] for r in traffic if r["code"] == "BOS" and r["year"] == 2025))

    def test_failed_and_malformed_refresh_keeps_last_good_and_reports_failure(self):
        before = self.reader.active_version_token()
        coordinator = self._coordinator()
        with mock.patch.object(live_sources, "fetch_global_registry", side_effect=TimeoutError("slow")), \
             mock.patch.object(live_sources, "fetch_bts_airport_year", return_value=[{"code": "BAD", "year": 2025}]), \
             mock.patch.object(live_sources, "fetch_and_build_faa_taf", side_effect=ValueError("bad zip")):
            coordinator.start(offline=False)
            self.assertTrue(coordinator.wait(30))
        status = coordinator.status()
        self.assertTrue(all(item["state"] == "failed_last_good" for item in status.values()), status)
        self.assertNotIn("slow", json.dumps(status))  # only exception class, no raw message
        self.assertEqual(self.reader.active_version_token(), before)
        self.assertEqual(len(self.reader.full_snapshot()["faa_taf"]), 712)

    def test_one_failing_source_does_not_block_the_others(self):
        traffic = copy.deepcopy(self.source_snapshot["airport_years"])
        coordinator = self._coordinator()
        with mock.patch.object(live_sources, "fetch_global_registry", side_effect=TimeoutError("slow")), \
             mock.patch.object(live_sources, "fetch_bts_airport_year",
                               side_effect=lambda year: [r for r in traffic if r["year"] == year]), \
             mock.patch.object(live_sources, "fetch_and_build_faa_taf", side_effect=ValueError("bad zip")):
            coordinator.start(offline=False)
            self.assertTrue(coordinator.wait(30))
        status = coordinator.status()
        self.assertEqual(status["airport_registry"]["state"], "failed_last_good")
        self.assertEqual(status["airport"]["state"], "fresh")
        self.assertEqual(status["faa_taf"]["state"], "failed_last_good")


if __name__ == "__main__":
    unittest.main()

"""Parsers and SQLite publication for one-time official public extracts."""

from __future__ import annotations

import csv
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from airport_intel.analytics import calculations
from airport_intel.ingestion.install import install_bundled_public_data
from airport_intel.ingestion.bundled_exports import (
    parse_delay_causes_zip,
    parse_npias_xlsx,
    parse_t100_segments_zip,
)
from airport_intel.analytics.coverage import airport_preview, coverage_report
from airport_intel.storage import FactProvider
from tests.sqlite_fixture import SeededDatabaseTestCase


def _csv_zip(path: Path, filename: str, fieldnames: list[str], rows: list[dict]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, buffer.getvalue())


def _tiny_npias_xlsx(path: Path) -> None:
    strings = [
        "State", "City", "Airport", "LocID", "Owner-ship", "Svc Lvl (FY25)",
        "Hub (FY25)", "Role (FY25)", "Enplaned (CY23/FY25)",
        "Based Aircraft (FY25)", "Development Estimate 2025-2029",
        "California", "San Francisco", "San Francisco International", "SFO",
        "PU", "P", "Large", "Commercial Service",
    ]
    shared = "".join(f"<si><t>{value}</t></si>" for value in strings)
    header = "".join(
        f'<c r="{column}1" t="s"><v>{index}</v></c>'
        for index, column in enumerate("ABCDEFGHIJK")
    )
    data = "".join([
        '<c r="A2" t="s"><v>11</v></c>', '<c r="B2" t="s"><v>12</v></c>',
        '<c r="C2" t="s"><v>13</v></c>', '<c r="D2" t="s"><v>14</v></c>',
        '<c r="E2" t="s"><v>15</v></c>', '<c r="F2" t="s"><v>16</v></c>',
        '<c r="G2" t="s"><v>17</v></c>', '<c r="H2" t="s"><v>18</v></c>',
        '<c r="I2"><v>24000000</v></c>', '<c r="J2"><v>12</v></c>',
        '<c r="K2"><v>125000000</v></c>',
    ])
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData><row r="1">{header}</row><row r="2">{data}</row></sheetData></worksheet>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"{shared}</sst>",
        )
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)


class PublicDataParserTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_t100_aggregates_hidden_dimensions_and_keeps_cargo_context(self):
        path = self.root / "t100.zip"
        fields = [
            "DEPARTURES_PERFORMED", "SEATS", "PASSENGERS", "FREIGHT", "DISTANCE",
            "ORIGIN", "ORIGIN_COUNTRY", "DEST", "DEST_COUNTRY", "YEAR", "CLASS", "DATA_SOURCE",
        ]
        base = {"DISTANCE": "3100", "ORIGIN": "ANC", "ORIGIN_COUNTRY": "US",
                "DEST": "JFK", "DEST_COUNTRY": "US", "YEAR": "2025", "CLASS": "F",
                "DATA_SOURCE": "DU"}
        rows = [
            {**base, "DEPARTURES_PERFORMED": "10", "SEATS": "1000", "PASSENGERS": "800", "FREIGHT": "50"},
            {**base, "DEPARTURES_PERFORMED": "5", "SEATS": "500", "PASSENGERS": "400", "FREIGHT": "25"},
            {**base, "CLASS": "G", "DEPARTURES_PERFORMED": "7", "SEATS": "0", "PASSENGERS": "0", "FREIGHT": "9000"},
            {**base, "ORIGIN": "YYZ", "ORIGIN_COUNTRY": "CA", "DEPARTURES_PERFORMED": "3",
             "SEATS": "300", "PASSENGERS": "200", "FREIGHT": "0"},
        ]
        _csv_zip(path, "t100.csv", fields, rows)

        parsed, stats = parse_t100_segments_zip(path, enforce_coverage=False)

        self.assertEqual(len(parsed), 2)
        passenger = next(row for row in parsed if row["service_class"] == "F")
        self.assertEqual(passenger["departures"], 15)
        self.assertEqual(passenger["seats"], 1500)
        self.assertEqual(passenger["passengers"], 1200)
        self.assertEqual(stats["covered_origins"], 1)

    def test_delay_export_aggregates_carriers_at_airport_month(self):
        path = self.root / "delay.zip"
        fields = ["year", "month", "carrier", "airport", "arr_flights", "arr_del15",
                  "arr_cancelled", "arr_diverted", "arr_delay", "carrier_delay",
                  "weather_delay", "nas_delay", "security_delay", "late_aircraft_delay"]
        rows = [
            {"year": "2025", "month": "1", "carrier": "AA", "airport": "SFO",
             "arr_flights": "100.00", "arr_del15": "20.00", "arr_cancelled": "2.00",
             "arr_diverted": "1.00", "arr_delay": "1200.00", "carrier_delay": "300.00",
             "weather_delay": "100.00", "nas_delay": "500.00", "security_delay": "0.00",
             "late_aircraft_delay": "300.00"},
            {"year": "2025", "month": "1", "carrier": "UA", "airport": "SFO",
             "arr_flights": "50.00", "arr_del15": "10.00", "arr_cancelled": "1.00",
             "arr_diverted": "0.00", "arr_delay": "600.00", "carrier_delay": "200.00",
             "weather_delay": "0.00", "nas_delay": "200.00", "security_delay": "0.00",
             "late_aircraft_delay": "200.00"},
        ]
        _csv_zip(path, "Airline_Delay_Cause.csv", fields, rows)

        parsed, stats = parse_delay_causes_zip(path, enforce_coverage=False)

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["flights"], 150)
        self.assertEqual(parsed[0]["delayed"], 30)
        self.assertEqual(parsed[0]["nas_delay_minutes"], 700)
        self.assertEqual(stats["covered_airports"], 1)

    def test_npias_reads_business_context_without_third_party_library(self):
        path = self.root / "npias.xlsx"
        _tiny_npias_xlsx(path)

        parsed, stats = parse_npias_xlsx(path, enforce_coverage=False)

        self.assertEqual(parsed[0]["locid"], "SFO")
        self.assertEqual(parsed[0]["development_estimate"], 125_000_000)
        self.assertEqual(parsed[0]["hub_size"], "Large")
        self.assertEqual(stats["covered_airports"], 1)


class PublicDataStorageTests(SeededDatabaseTestCase):
    def test_normalized_sources_are_scoped_and_visible_in_coverage_and_preview(self):
        self.publisher.publish_route_segments([
            {"origin": "SFO", "destination": "JFK", "year": 2025, "service_class": "F",
             "data_source": "DU", "distance_miles": 2586, "departures": 100,
             "seats": 20000, "passengers": 17000, "freight_pounds": 5000},
            {"origin": "SFO", "destination": "LHR", "year": 2025, "service_class": "F",
             "data_source": "IU", "distance_miles": 5354, "departures": 50,
             "seats": 15000, "passengers": 12000, "freight_pounds": 2000},
        ], {"url": "https://www.transtats.bts.gov/", "period": "2025"})
        self.publisher.publish_delay_months([
            {"code": "SFO", "year": 2025, "month": 1, "flights": 100,
             "delayed": 20, "cancelled": 2, "diverted": 1,
             "arrival_delay_minutes": 1200, "carrier_delay_minutes": 300,
             "weather_delay_minutes": 100, "nas_delay_minutes": 500,
             "security_delay_minutes": 0, "late_aircraft_delay_minutes": 300},
        ], {"url": "https://www.transtats.bts.gov/OT_Delay/", "period": "2025"})
        self.publisher.publish_development_plans([
            {"locid": "SFO", "state": "California", "city": "San Francisco",
             "name": "San Francisco International", "ownership": "PU", "service_level": "P",
             "hub_size": "Large", "role": "Commercial Service", "enplanements": 24_000_000,
             "based_aircraft": 12, "development_estimate": 125_000_000},
        ], {"url": "https://www.faa.gov/airports/planning_capacity/npias/current",
            "period": "2025-2029"})

        longhaul = self.reader.analysis_facts({"longhaul_share"}, {"SFO"})
        self.assertEqual({row["origin"] for row in longhaul["route_segments"]}, {"SFO"})
        comparison = self.reader.analysis_facts({"compare_airports"}, {"SFO"})
        summary = calculations.summarize_delay_rows(comparison["delay_rows"])
        self.assertAlmostEqual(summary["SFO"]["delayed_pct"], 20.0)
        demand = self.reader.analysis_facts({"assess_demand_pressure"}, {"SFO"})
        self.assertEqual(demand["npias_development"]["SFO"]["development_estimate"], 125_000_000)

        provider = FactProvider(self.reader)
        report = coverage_report(provider.for_coverage())
        self.assertEqual(report["congestion"]["covered_airports"], 1)
        self.assertEqual(report["longhaul_share"]["covered_airports"], 1)
        self.assertEqual(report["development_context"]["covered_airports"], 1)
        preview = airport_preview(provider, "SFO")
        self.assertTrue(preview["capabilities"]["congestion"]["available"])
        self.assertTrue(preview["capabilities"]["longhaul_share"]["available"])
        self.assertTrue(preview["capabilities"]["development_context"]["available"])

    def test_invalid_delay_candidate_never_activates(self):
        before = self.reader.active_version_token()
        with self.assertRaises(ValueError):
            self.publisher.publish_delay_months([
                {"code": "SFO", "year": 2025, "month": 13, "flights": 100,
                 "delayed": 20, "cancelled": 0, "diverted": 0,
                 "arrival_delay_minutes": 100, "carrier_delay_minutes": 20,
                 "weather_delay_minutes": 10, "nas_delay_minutes": 30,
                 "security_delay_minutes": 0, "late_aircraft_delay_minutes": 40},
            ], {"url": "https://www.transtats.bts.gov/OT_Delay/"})
        self.assertEqual(self.reader.active_version_token(), before)


class BundledPublicDataIntegrationTests(SeededDatabaseTestCase):
    def test_real_bundled_sources_install_once_and_reconcile_known_values(self):
        artifact_dir = Path(__file__).resolve().parents[2] / "data" / "source_artifacts"
        first = install_bundled_public_data(self.publisher, self.reader, artifact_dir)
        second = install_bundled_public_data(self.publisher, self.reader, artifact_dir)

        self.assertEqual(set(first["installed"]), {
            "bts_t100_segment", "bts_delay_causes", "faa_npias",
        })
        self.assertEqual(second["installed"], {})
        self.assertEqual(set(second["skipped_active"]), {
            "bts_t100_segment", "bts_delay_causes", "faa_npias",
        })
        provider = FactProvider(self.reader)
        report = coverage_report(provider.for_coverage())
        self.assertEqual(report["congestion"]["covered_airports"], 354)
        self.assertEqual(report["longhaul_share"]["covered_airports"], 635)
        self.assertEqual(report["development_context"]["covered_airports"], 794)

        anc = self.reader.analysis_facts({"longhaul_share"}, {"ANC"})
        eligible = [row for row in anc["route_segments"]
                    if row["service_class"] == "F" and row["departures"] > 0 and row["seats"] > 0]
        self.assertEqual(sum(row["departures"] for row in eligible), 35_778)
        self.assertEqual(sum(row["departures"] for row in eligible
                             if row["distance_miles"] >= 3000), 919)

        delays = self.reader.analysis_facts({"compare_airports"}, {"LAX", "SNA"})
        summary = calculations.summarize_delay_rows(delays["delay_rows"])
        self.assertEqual(summary["LAX"]["delayed_pct"], 18.67)
        self.assertEqual(summary["SNA"]["delayed_pct"], 18.24)
        development = self.reader.analysis_facts({"assess_demand_pressure"}, {"SFO"})
        self.assertEqual(
            development["npias_development"]["SFO"]["development_estimate"], 683_792_071,
        )


if __name__ == "__main__":
    unittest.main()

"""Airport Explorer headline figures: arithmetic, scoping, and gaps stay gaps."""

import unittest

from airport_intel.analytics.highlights import preview_highlights


def year(y, passengers, seats=None, dom=100, intl=25, months=12):
    return {"code": "AAA", "year": y, "months": months, "passengers": passengers,
            "domestic_passengers": int(passengers * 0.75),
            "seats": seats or passengers * 2, "domestic_departures": dom, "international_departures": intl}


def segment(dest, miles, departures, passengers, origin="AAA", service="F", seats=100):
    return {"origin": origin, "destination": dest, "distance_miles": miles, "departures": departures,
            "passengers": passengers, "service_class": service, "seats": seats}


class PreviewHighlightTests(unittest.TestCase):
    def test_full_bundle(self):
        traffic = [year(2023, 1_000_000), year(2024, 1_100_000), year(2025, 1_210_000, seats=1_512_500)]
        bundle = {
            "delay_rows": [{
                "code": "AAA", "year": 2025, "month": 1, "flights": 1000, "delayed": 214,
                "cancelled": 10, "diverted": 0, "arrival_delay_minutes": 14_058,
                "carrier_delay_minutes": 600, "weather_delay_minutes": 50,
                "nas_delay_minutes": 250, "security_delay_minutes": 0,
                "late_aircraft_delay_minutes": 100,
            }],
            "route_segments": [
                segment("BBB", 400, 90, 9_000), segment("CCC", 3_200, 10, 4_000),
                segment("CCC", 3_200, 5, 1_000),                   # same destination, second carrier
                segment("DDD", 5_000, 50, 0, service="P", seats=0),  # cargo: excluded
                segment("EEE", 500, 99, 99_999, origin="ZZZ"),      # other origin: excluded
            ],
            "npias_development": {"AAA": {"hub_size": "M", "ownership": "PU", "service_level": "P",
                                           "based_aircraft": 12, "development_estimate": 250_000_000}},
        }
        h = preview_highlights(bundle, "AAA", traffic)
        self.assertEqual(h["year"], 2025)
        self.assertEqual(h["passengers"], 1_210_000)
        self.assertEqual(h["two_year_cagr_pct"], 10.0)
        self.assertEqual(h["seat_utilization_pct"], 80.0)
        self.assertEqual(h["international_departure_share_pct"], 20.0)
        self.assertEqual(h["delayed_arrivals_pct"], 21.4)
        self.assertEqual(h["longhaul_share_pct"], 14.29)  # 15 of 105 passenger departures >= 3,000 mi
        self.assertEqual(h["nonstop_destinations"], 2)
        self.assertEqual(h["top_route"], {"destination": "BBB", "passengers": 9_000})
        self.assertEqual(h["hub_size"], "Medium hub")
        self.assertEqual(h["development_estimate_usd"], 250_000_000)
        self.assertEqual([y["year"] for y in h["yearly"]], [2023, 2024, 2025])
        self.assertIsNone(h["yearly"][0]["change_pct"])
        self.assertEqual(h["yearly"][1]["change_pct"], 10.0)
        self.assertEqual(h["yearly"][2]["seat_utilization_pct"], 80.0)
        self.assertEqual(h["top_routes"], [{"destination": "BBB", "passengers": 9_000},
                                           {"destination": "CCC", "passengers": 5_000}])
        self.assertEqual(h["international_passenger_share_pct"], 25.0)
        self.assertEqual(h["ownership"], "Public")
        self.assertEqual(h["service_level"], "Primary commercial service")
        self.assertEqual(h["based_aircraft"], 12)

    def test_missing_sources_are_none_not_zero(self):
        traffic = [year(2024, 500_000), year(2025, 520_000, months=11)]
        h = preview_highlights({}, "AAA", traffic)
        self.assertEqual(h["year"], 2024)              # incomplete 2025 is not the headline year
        self.assertIsNone(h["two_year_cagr_pct"])      # needs complete 2023 and 2025
        for key in ("delayed_arrivals_pct", "longhaul_share_pct", "nonstop_destinations", "top_route",
                    "top_routes", "on_time_pct", "hub_size", "ownership", "development_estimate_usd"):
            with self.subTest(key=key):
                self.assertIsNone(h[key])

    def test_no_traffic(self):
        h = preview_highlights({}, "AAA", [])
        self.assertIsNone(h["year"])
        self.assertIsNone(h["passengers"])
        self.assertIsNone(h["seat_utilization_pct"])


if __name__ == "__main__":
    unittest.main()

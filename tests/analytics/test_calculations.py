import unittest

from airport_intel.analytics import calculations
from tests.fixtures import build_snapshot


class GoldenMathTests(unittest.TestCase):
    """Hand-computed values, independent of the fixture builder's loop."""

    def test_two_year_cagr_formula(self):
        # P2023=1,000,000 P2025=1,210,000 -> CAGR = sqrt(1.21) - 1 = 0.10 exactly.
        snap = {
            "airport_registry": [{"code": "AAA", "name": "A", "city": "A", "country": "US"}],
            "airport_years": [
                {"code": "AAA", "year": 2023, "name": "A", "city": "A", "months": 12,
                 "passengers": 1_000_000, "seats": 1_250_000, "domestic_passengers": 0,
                 "domestic_seats": 0, "domestic_departures": 0, "international_departures": 0},
                {"code": "AAA", "year": 2024, "name": "A", "city": "A", "months": 12,
                 "passengers": 1_100_000, "seats": 1_350_000, "domestic_passengers": 0,
                 "domestic_seats": 0, "domestic_departures": 0, "international_departures": 0},
                {"code": "AAA", "year": 2025, "name": "A", "city": "A", "months": 12,
                 "passengers": 1_210_000, "seats": 1_450_000, "domestic_passengers": 0,
                 "domestic_seats": 0, "domestic_departures": 0, "international_departures": 0},
            ],
        }
        rows = calculations._eligible_v2_rows(snap, {"AAA"})
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["two_year_cagr_pct"], 10.0, places=6)
        self.assertAlmostEqual(rows[0]["annualized_incremental_passengers"], 105_000.0, places=6)
        self.assertAlmostEqual(rows[0]["seat_utilization_pct"], 1_210_000 / 1_450_000 * 100, places=6)

    def test_percentile_midpoint_for_tied_value(self):
        self.assertEqual(calculations.percentile(5, [1, 5, 5, 9]), 50.0)

    def test_growth_clamp_applied_to_percentile_input_not_display(self):
        row = calculations._eligible_v2_rows(
            {"airport_registry": [{"code": "BBB", "name": "B", "city": "B", "country": "US"}],
             "airport_years": [
                 {"code": "BBB", "year": 2023, "name": "B", "city": "B", "months": 12,
                  "passengers": 100_000, "seats": 150_000, "domestic_passengers": 0,
                  "domestic_seats": 0, "domestic_departures": 0, "international_departures": 0},
                 {"code": "BBB", "year": 2024, "name": "B", "city": "B", "months": 12,
                  "passengers": 200_000, "seats": 260_000, "domestic_passengers": 0,
                  "domestic_seats": 0, "domestic_departures": 0, "international_departures": 0},
                 {"code": "BBB", "year": 2025, "name": "B", "city": "B", "months": 12,
                  "passengers": 400_000, "seats": 500_000, "domestic_passengers": 0,
                  "domestic_seats": 0, "domestic_departures": 0, "international_departures": 0},
             ]}, {"BBB"})[0]
        self.assertGreater(row["two_year_cagr_pct"], 50)  # unclamped value shown
        self.assertEqual(row["two_year_cagr_pct_clamped"], 20.0)  # clamped for scoring


class ScreenV2Tests(unittest.TestCase):
    def setUp(self):
        self.snapshot = build_snapshot()

    def test_eligible_peers_meet_minimum_cohort_size(self):
        rows = calculations.screen_v2(self.snapshot)
        self.assertEqual(len(rows), 12)
        for row in rows:
            self.assertGreaterEqual(row["peer_count"], 12)

    def test_incomplete_three_year_history_is_excluded(self):
        rows = calculations.screen_v2(self.snapshot)
        self.assertNotIn("ZZZ", {r["code"] for r in rows})

    def test_score_is_stable_under_repeated_runs(self):
        first = calculations.screen_v2(self.snapshot)
        second = calculations.screen_v2(self.snapshot)
        self.assertEqual([r["code"] for r in first], [r["code"] for r in second])
        self.assertEqual([r["score"] for r in first], [r["score"] for r in second])

    def test_ranked_order_is_deterministic_tiebreak(self):
        rows = calculations.screen_v2(self.snapshot)
        scores = [r["score"] for r in rows]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_score_is_labeled_v2(self):
        for row in calculations.screen_v2(self.snapshot):
            self.assertEqual(row["score_version"], "v2")

    def test_weight_sensitivity_within_plus_minus_point_one(self):
        base_code = calculations.screen_v2(self.snapshot)[0]["code"]
        result = calculations.weight_sensitivity(self.snapshot, None, base_code, growth_weights=(0.30, 0.40, 0.50))
        self.assertEqual(result["growth_weights"], [0.30, 0.40, 0.50])
        self.assertIsNotNone(result["base_rank_by_weight"][0.40])  # airport found in the ranked order
        for weight, ranked in result["runs"].items():
            for row in ranked:
                self.assertAlmostEqual(row["weights"]["utilization"], 0.65 - weight, places=6)


class AbsoluteChangeTests(unittest.TestCase):
    def test_ranks_by_absolute_passengers_not_percentage(self):
        snapshot = build_snapshot()
        rows = calculations.rank_by_absolute_change(snapshot)
        changes = [r["absolute_change"] for r in rows]
        self.assertEqual(changes, sorted(changes, reverse=True))
        for row in rows:
            self.assertEqual(row["absolute_change"], row["passengers_2025"] - row["passengers_2023"])

    def test_scope_filter_restricts_to_requested_codes(self):
        snapshot = build_snapshot()
        rows = calculations.rank_by_absolute_change(snapshot, codes={"XQA", "XQB"})
        self.assertEqual({r["code"] for r in rows}, {"XQA", "XQB"})


class CongestionTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = build_snapshot()

    def test_both_sides_installed_is_available(self):
        result = calculations.compare_congestion(self.snapshot, ("XQA", "XQB"))
        self.assertTrue(result["available"])
        self.assertEqual({a["code"] for a in result["airports"]}, {"XQA", "XQB"})

    def test_single_airport_profile_uses_the_same_metric_shape(self):
        result = calculations.congestion_profile(self.snapshot, "XQA")
        self.assertTrue(result["available"])
        self.assertEqual(result["airports"][0]["delayed_pct"], 18.2)
        self.assertEqual(result["airports"][0]["code"], "XQA")

    def test_missing_one_side_is_unavailable_not_a_proxy(self):
        result = calculations.compare_congestion(self.snapshot, ("XQA", "XQC"))
        self.assertFalse(result["available"])
        self.assertIn("XQC", result["reason"])

    def test_same_airport_twice_rejected(self):
        with self.assertRaises(ValueError):
            calculations.compare_congestion(self.snapshot, ("XQA", "XQA"))

    def test_raw_monthly_counts_are_aggregated_in_analytics(self):
        rows = [
            {"code": "AAA", "year": 2025, "month": 1, "flights": 100, "delayed": 10,
             "cancelled": 2, "diverted": 1, "arrival_delay_minutes": 970,
             "carrier_delay_minutes": 30, "weather_delay_minutes": 10,
             "nas_delay_minutes": 50, "security_delay_minutes": 0,
             "late_aircraft_delay_minutes": 10},
            {"code": "AAA", "year": 2025, "month": 2, "flights": 300, "delayed": 90,
             "cancelled": 3, "diverted": 0, "arrival_delay_minutes": 2_970,
             "carrier_delay_minutes": 90, "weather_delay_minutes": 30,
             "nas_delay_minutes": 150, "security_delay_minutes": 0,
             "late_aircraft_delay_minutes": 30},
        ]
        summary = calculations.summarize_delay_rows(rows)["AAA"]
        self.assertEqual(summary["scheduled_arrivals"], 400)
        self.assertEqual(summary["delayed_pct"], 25.0)
        self.assertEqual(summary["nas_pct"], 50.0)
        self.assertEqual(summary["delay_minutes_per_completed_arrival"], 10.0)


class LonghaulTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = build_snapshot()

    def test_cargo_excluded_from_denominator(self):
        result = calculations.longhaul_share(self.snapshot["route_segments"], "XQA", 2500)
        self.assertTrue(result["available"])
        self.assertEqual(result["total_departures"], 400 + 900 + 300)  # the 50 cargo departures excluded

    def test_threshold_sensitivity_changes_share(self):
        low = calculations.longhaul_share(self.snapshot["route_segments"], "XQA", 2500)
        high = calculations.longhaul_share(self.snapshot["route_segments"], "XQA", 3500)
        self.assertGreaterEqual(low["percentage"], high["percentage"])

    def test_threshold_out_of_bounds_rejected(self):
        with self.assertRaises(ValueError):
            calculations.longhaul_share(self.snapshot["route_segments"], "XQA", 100)

    def test_no_route_coverage_is_insufficient_evidence_not_zero(self):
        result = calculations.longhaul_share(self.snapshot["route_segments"], "XQB", 2500)
        self.assertFalse(result["available"])
        self.assertNotIn("percentage", result)


class DemandAndRevenueTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = build_snapshot()

    def test_demand_pressure_labels_forecast_separately(self):
        with_taf = calculations.demand_pressure(self.snapshot, "XQA")
        self.assertTrue(with_taf["forecast"]["available"])
        without_taf = calculations.demand_pressure(self.snapshot, "XQB")
        self.assertFalse(without_taf["forecast"]["available"])

    def test_demand_uplift_bounds_enforced(self):
        with self.assertRaises(ValueError):
            calculations.demand_pressure(self.snapshot, "XQA", demand_uplift_pct=50)

    def test_bottleneck_candidates_are_labeled_hypotheses(self):
        result = calculations.demand_pressure(self.snapshot, "XQA")
        for candidate in result["bottleneck_hypotheses"]:
            self.assertEqual(candidate["status"], "hypothesis")

    def test_revenue_context_unavailable_without_cats(self):
        result = calculations.revenue_context(self.snapshot, "XQB", 10_000)
        self.assertFalse(result["available"])

    def test_revenue_context_labels_historical_average_not_profit(self):
        result = calculations.revenue_context(self.snapshot, "XQA", 10_000)
        self.assertTrue(result["available"])
        self.assertIn("not marginal contribution", result["label"])
        self.assertIn("profit", result["label"])
        self.assertEqual(set(result["scenarios"]), {"low", "base", "high"})


if __name__ == "__main__":
    unittest.main()

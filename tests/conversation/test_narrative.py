"""Deterministic answer narrative: which takeaway rule fires, and when none does."""

import unittest

from airport_intel.conversation.answers import Answer
from airport_intel.conversation.narrative import insight
from airport_intel.planning.planner import Plan

WEIGHTS = {"growth": 0.40, "utilization": 0.25, "scale": 0.35}


def score_row(code, score, cagr, util, passengers, growth_p, util_p, incr_p, reversal=False):
    return {
        "code": code, "name": f"{code} Intl", "score": score, "two_year_cagr_pct": cagr,
        "seat_utilization_pct": util, "passengers_2023": passengers, "passengers_2025": passengers,
        "annualized_incremental_passengers": passengers * cagr / 100, "growth_percentile": growth_p,
        "utilization_percentile": util_p, "incremental_percentile": incr_p, "weights": WEIGHTS,
        "growth_reversal": reversal,
    }


def ranking_body(rows, scope=None):
    return {"result_state": "computed", "answer_kind": "ranking", "ranking_rows": rows, "metrics": [],
            "follow_up_context": {"geographic_scope": scope or {"kind": "region", "key": "new england"},
                                  "airport_codes": []}}


class RankingInsightTests(unittest.TestCase):
    def test_fastest_grower_that_does_not_lead_is_explained_by_its_weakest_driver(self):
        rows = [score_row("PVD", 87.8, 10.5, 77.8, 2_100_000, 98.8, 66.3, 90.5),
                score_row("BOS", 83.3, 2.8, 81.6, 21_000_000, 75.0, 78.3, 96.2),
                score_row("HVN", 74.7, 22.7, 75.3, 740_000, 99.3, 20.1, 85.6)]
        result = insight(ranking_body(rows))
        self.assertTrue(result["headline"].startswith("New England:"))
        self.assertIn("PVD leads 3 ranked airports in New England with a score of 87.8", result["summary"])
        self.assertIn("HVN is the fastest grower", result["takeaway"])
        self.assertIn("#3", result["takeaway"])
        self.assertIn("least full", result["takeaway"])

    def test_larger_airport_that_does_not_lead_gets_the_size_takeaway(self):
        rows = [score_row("PVD", 80.0, 10.0, 78.0, 2_000_000, 95.0, 60.0, 80.0),
                score_row("BOS", 70.0, 3.0, 80.0, 20_000_000, 70.0, 70.0, 90.0)]
        result = insight(ranking_body(rows, {"kind": "state", "abbr": "MA"}))
        self.assertTrue(result["headline"].startswith("Massachusetts:"))
        self.assertIn("BOS carries 10x PVD's passengers but ranks #2", result["takeaway"])

    def test_growth_reversal_on_the_leader_takes_priority(self):
        rows = [score_row("AAA", 90.0, 15.0, 80.0, 1_000_000, 99.0, 80.0, 80.0, reversal=True),
                score_row("BBB", 60.0, 20.0, 70.0, 900_000, 99.5, 40.0, 50.0)]
        self.assertIn("Read AAA's lead with care", insight(ranking_body(rows))["takeaway"])

    def test_absolute_change_ranking_contrasts_volume_and_speed(self):
        rows = [{"code": "BOS", "name": "Logan", "passengers_2023": 19_000_000, "passengers_2025": 21_000_000,
                 "absolute_change": 2_000_000, "two_year_cagr_pct": 5.1},
                {"code": "HVN", "name": "Tweed", "passengers_2023": 490_000, "passengers_2025": 740_000,
                 "absolute_change": 250_000, "two_year_cagr_pct": 22.7}]
        result = insight(ranking_body(rows))
        self.assertIn("19.0M to 21.0M", result["summary"])
        self.assertIn("Volume and speed disagree", result["takeaway"])


class OtherInsightTests(unittest.TestCase):
    def test_comparison_where_both_airports_shrank_does_not_crown_a_winner(self):
        body = {"result_state": "computed", "answer_kind": "comparison",
                "follow_up_context": {"airport_codes": ["AUS", "SAT"]},
                "metrics": [{"name": "absolute_passenger_change", "status": "computed", "data": [
                    {"code": "AUS", "passengers_2023": 11_100_000, "passengers_2025": 10_800_000,
                     "absolute_change": -300_000, "two_year_cagr_pct": -1.5},
                    {"code": "SAT", "passengers_2023": 5_400_000, "passengers_2025": 5_300_000,
                     "absolute_change": -100_000, "two_year_cagr_pct": -1.3}]}]}
        result = insight(body)
        self.assertEqual(result["headline"], "AUS vs SAT")
        self.assertIn("Both airports carried fewer passengers", result["takeaway"])

    def test_congestion_takeaway_never_claims_a_terminal_bottleneck(self):
        body = {"result_state": "computed", "answer_kind": "comparison",
                "follow_up_context": {"airport_codes": ["LAX", "SNA"]},
                "metrics": [{"name": "congestion", "status": "computed", "data": {"airports": [
                    {"code": "LAX", "delayed_pct": 21.4}, {"code": "SNA", "delayed_pct": 17.9}]}}]}
        result = insight(body)
        self.assertIn("LAX had 21.4% of flights delayed versus SNA's 17.9%, a gap of 3.5 points", result["summary"])
        self.assertIn("does not show that the terminal is the bottleneck", result["takeaway"])

    def test_demand_compares_observed_growth_with_the_faa_outlook(self):
        body = {"result_state": "partial", "answer_kind": "demand", "resolved_scope": "SFO",
                "metrics": [{"name": "demand_pressure", "status": "computed", "data": {
                    "observed": {"passengers_2025": 26_500_000, "two_year_cagr_pct": 4.3},
                    "forecast": {"available": True, "enplanement_change_pct": -2.0}}}]}
        result = insight(body)
        self.assertIn("2.0% fewer enplanements by FY2030", result["summary"])
        self.assertIn("Public data cannot show passengers turned away", result["summary"])
        self.assertIn("running ahead of the FAA's outlook", result["takeaway"])

    def test_longhaul_share(self):
        body = {"result_state": "computed", "answer_kind": "metric", "resolved_scope": "ANC",
                "metrics": [{"name": "longhaul_share", "status": "computed", "data": {
                    "percentage": 12.5, "long_departures": 125, "total_departures": 1000,
                    "threshold_miles": 3000}}]}
        result = insight(body)
        self.assertIn("12.5% of ANC's scheduled passenger departures (125 of 1,000)", result["summary"])
        self.assertIn("1 in 8", result["takeaway"])


class NoInsightTests(unittest.TestCase):
    def test_non_computed_states_and_unknown_shapes_get_none(self):
        for state in ("insufficient_evidence", "clarification_required", "out_of_scope", "unsupported_metric"):
            with self.subTest(state=state):
                self.assertIsNone(insight({"result_state": state, "answer_kind": "ranking",
                                           "ranking_rows": [{"code": "X"}]}))
        malformed = ranking_body([{"code": "X", "score": 50}])
        self.assertIsNone(insight(malformed))

    def test_envelope_carries_insight_key(self):
        plan = Plan(intent="rank", operations=["rank_airports"], requested_metrics=["opportunity_score"])
        body = Answer(answer_kind="metric", result_state="insufficient_evidence", message="missing").finalize(plan)
        self.assertIn("insight", body)
        self.assertIsNone(body["insight"])


if __name__ == "__main__":
    unittest.main()

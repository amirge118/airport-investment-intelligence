"""The evaluation scorer must be able to fail: each check is shown rejecting a bad answer."""

import importlib.util
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "run_eval", Path(__file__).resolve().parents[1] / "scripts" / "run_eval.py")
run_eval = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_eval)


def _answer(**overrides):
    body = {
        "result_state": "computed", "message": "PDX congestion proxy: 17.93% of arrivals were delayed in 2025.",
        "resolved_scope": "PDX", "metrics": [{"name": "congestion", "data": {"delayed_pct": 17.9312}}],
        "ranking_rows": [], "evidence": [{"source": "BTS"}], "missing_evidence": [],
        "follow_up_context": {"threshold_miles": None},
        "analysis_trace": {"selected_tools": ["compare_airports"], "selected_metrics": ["congestion"],
                           "planner_source": "keyword_fallback"},
    }
    body.update(overrides)
    return body


CASE = {"id": "T", "turns": ["How delayed is PDX?"],
        "expect": {"state": "computed", "operations": ["compare_airports"], "metrics": ["congestion"],
                   "scope": "PDX", "must_not_contain": ["17%"]}}


class ScoringTests(unittest.TestCase):
    def test_faithful_answer_passes_every_check(self):
        checks = run_eval.score_case(CASE, _answer(), "full")
        self.assertTrue(all(ok for ok, _ in checks.values()), checks)

    def test_number_absent_from_structured_fields_is_flagged(self):
        answer = _answer(message="PDX congestion proxy: 42.5% of arrivals were delayed in 2025.")
        self.assertEqual(run_eval.unsupported_numbers(answer, "How delayed is PDX?"), ["42.5"])

    def test_number_supported_only_at_its_own_precision(self):
        self.assertEqual(run_eval.unsupported_numbers(_answer(message="17.9% delayed"), ""), [])
        self.assertEqual(run_eval.unsupported_numbers(_answer(message="17.8% delayed"), ""), ["17.8"])

    def test_number_from_the_question_is_not_fabricated(self):
        answer = _answer(message="0.0% of departures were >= 2,500 miles.",
                         metrics=[{"name": "longhaul_share", "data": {"percentage": 0.0}}])
        self.assertEqual(run_eval.unsupported_numbers(answer, "at least 2,500 miles?"), [])

    def test_each_plan_and_answer_check_can_fail(self):
        bad = _answer(
            result_state="partial", resolved_scope="SEA", message="PDX has exactly 17% delays.",
            evidence=[], missing_evidence=[],
            analysis_trace={"selected_tools": ["rank_airports"], "selected_metrics": ["opportunity_score"],
                            "planner_source": "keyword_fallback"},
        )
        checks = run_eval.score_case(CASE, bad, "full")
        for name in ("state", "operations", "metrics", "scope", "text", "numbers", "citations", "gaps"):
            with self.subTest(check=name):
                self.assertFalse(checks[name][0], checks[name])

    def test_state_expectation_can_depend_on_data_source(self):
        case = {**CASE, "expect": {"state": {"snapshot": "insufficient_evidence", "full": "computed"}}}
        self.assertTrue(run_eval.score_case(case, _answer(), "full")["state"][0])
        self.assertFalse(run_eval.score_case(case, _answer(), "snapshot")["state"][0])


if __name__ == "__main__":
    unittest.main()

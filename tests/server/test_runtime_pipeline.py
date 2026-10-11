"""The question pipeline over the real SQLite fact source (no HTTP).

Proves the ``Orchestrator`` <- ``FactProvider`` boundary: real ranking parity,
an identity catalogue that is cached rather than rebuilt or polluted by
analysis reads, and per-request fact bundles under concurrency.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from airport_intel.conversation.orchestrator import Orchestrator
from airport_intel.planning import llm
from airport_intel.planning import planner
from airport_intel.planning.planner import Plan
from airport_intel.server.runtime import build_runtime
from airport_intel.storage import FactProvider
from tests.sqlite_fixture import SeededDatabaseTestCase


class OrchestratorOverSQLiteTests(SeededDatabaseTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(llm, "active", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.orchestrator = Orchestrator(FactProvider(self.reader))

    def test_runtime_provider_preserves_real_ranking_parity(self):
        result = self.orchestrator.handle("db-parity", "Which New England airports deserve a closer look for expansion?")
        self.assertEqual(result["result_state"], "computed")
        self.assertEqual(
            [(row["code"], row["score"]) for row in result["ranking_rows"][:5]],
            [("PVD", 87.8), ("BOS", 83.3), ("BDL", 76.2), ("HVN", 74.7), ("PWM", 73.2)],
        )

    def test_runtime_identity_is_not_rebuilt_or_polluted_by_analysis_reads(self):
        orchestrator = self.orchestrator
        identity, registry = orchestrator.identity_catalogue, orchestrator.registry
        orchestrator.handle("s", "Which New England airports deserve a closer look for expansion?")
        orchestrator.handle("s", "How much unserved demand is there at SFO, and why?")
        self.assertIs(orchestrator.identity_catalogue, identity)
        self.assertIs(orchestrator.registry, registry)
        self.assertTrue(all(row["year"] == 2025 for row in orchestrator.identity_catalogue["airport_years"]))

    def test_concurrent_requests_do_not_share_fact_bundles(self):
        orchestrator = self.orchestrator
        barrier = threading.Barrier(2)
        original = orchestrator.facts.for_analysis

        def synchronized(operations, codes=None):
            facts = original(operations, codes)
            barrier.wait(5)  # both requests hold their own bundle before either computes
            return facts

        results: dict[str, dict] = {}
        with mock.patch.object(orchestrator.facts, "for_analysis", side_effect=synchronized):
            workers = [
                threading.Thread(target=lambda: results.__setitem__(
                    "rank", orchestrator.handle("a", "Which New England airports deserve a closer look for expansion?"))),
                threading.Thread(target=lambda: results.__setitem__(
                    "demand", orchestrator.handle("b", "How much unserved demand is there at SFO, and why?"))),
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(10)
        self.assertEqual(results["rank"]["result_state"], "computed")
        self.assertEqual(results["rank"]["ranking_rows"][0]["code"], "PVD")
        self.assertEqual(results["demand"]["result_state"], "partial")
        self.assertIn("SFO", results["demand"]["message"])

    def test_resolve_only_plan_asks_for_clarification_instead_of_422(self):
        plan = Plan(intent="resolve", operations=["resolve_airports"], entities=["SFO"])
        with mock.patch.object(planner, "plan_question", return_value=plan):
            result = self.orchestrator.handle("s", "SFO")
        self.assertEqual(result["result_state"], "clarification_required")

    def test_demand_question_reads_only_the_resolved_airport(self):
        calls = []
        original = self.orchestrator.facts.for_analysis

        def recording(operations, codes=None):
            calls.append((set(operations), set(codes or ())))
            return original(operations, codes)

        with mock.patch.object(self.orchestrator.facts, "for_analysis", side_effect=recording):
            self.orchestrator.handle("scoped", "How much unserved demand is there at SFO, and why?")
        self.assertEqual(calls, [({"assess_demand_pressure"}, {"SFO"})])


class BuildRuntimeTests(unittest.TestCase):
    def test_offline_runtime_wires_one_object_graph_and_never_refreshes(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(llm, "active", return_value=None):
            runtime = build_runtime(database_path=Path(tmp) / "runtime.sqlite3", offline=True)
            self.assertIs(runtime.orchestrator.facts, runtime.facts)
            self.assertIs(runtime.orchestrator.sessions, runtime.sessions)
            self.assertIs(runtime.facts.reader, runtime.reader)
            self.assertTrue(runtime.publisher.is_seeded())
            self.assertTrue(all(item["state"] == "offline" for item in runtime.refresh.status().values()))
            answer = runtime.orchestrator.handle("rt", "How much unserved demand is there at SFO, and why?")
            self.assertEqual(answer["result_state"], "partial")


if __name__ == "__main__":
    unittest.main()

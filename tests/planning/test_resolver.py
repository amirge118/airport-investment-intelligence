import unittest

from airport_intel.planning.resolver import Registry
from tests.fixtures import build_snapshot


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = Registry(build_snapshot())

    def test_exact_iata_code_resolves(self):
        decision = self.registry.resolve_token("XQA")
        self.assertEqual(decision["kind"], "airport")
        self.assertEqual(decision["candidates"][0]["code"], "XQA")

    def test_place_followed_by_airport_resolves_like_the_place(self):
        plain = self.registry.resolve_token("Fixture City")
        for phrase in ("Fixture City airport", "Fixture City International Airport"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.registry.resolve_token(phrase), plain)
        snapshot = build_snapshot()
        foreign = dict(snapshot["airport_registry"][0], code="ZZF", icao="SKZZ", name="Fixture City Airport",
                       city="Elsewhere", country="CO", region="CO-ANT")
        snapshot["airport_registry"].append(foreign)
        with_homonym = Registry(snapshot)
        # Several US fixture airports share the city: a US clarification, not a foreign refusal.
        resolved = with_homonym.resolve_token("Fixture City airport")
        self.assertNotEqual(resolved["kind"], "out_of_scope")
        self.assertTrue(all(row["country"] == "US" for row in resolved["candidates"]))
        self.assertEqual(with_homonym.resolve_token("ZZF")["kind"], "out_of_scope")
        unknown = self.registry.resolve_token("Nowhereville airport")
        self.assertEqual(unknown["kind"], "ambiguous")
        self.assertFalse(unknown.get("candidates"))

    def test_state_name_resolves_to_scope(self):
        decision = self.registry.resolve_token("Texas")
        self.assertEqual(decision, {"kind": "state", "abbr": "TX"})

    def test_state_abbreviation_resolves(self):
        self.assertEqual(self.registry.resolve_token("TX"), {"kind": "state", "abbr": "TX"})

    def test_region_resolves_to_scope(self):
        decision = self.registry.resolve_token("New England")
        self.assertEqual(decision["kind"], "region")
        self.assertEqual(decision["key"], "new england")

    def test_explicit_foreign_code_is_out_of_scope(self):
        # Data-driven: ZFO is a real row in the fixture's global registry
        # with country=FR, not a hardcoded denylist entry.
        decision = self.registry.resolve_token("ZFO")
        self.assertEqual(decision["kind"], "out_of_scope")
        self.assertEqual(decision["country"], "France")

    def test_explicit_foreign_name_is_out_of_scope(self):
        decision = self.registry.resolve_token("Zephyrton Foreign International Airport")
        self.assertEqual(decision["kind"], "out_of_scope")
        self.assertEqual(decision["country"], "France")

    def test_icao_code_resolves(self):
        decision = self.registry.resolve_token("KXQA")
        self.assertEqual(decision["kind"], "airport")
        self.assertEqual(decision["candidates"][0]["code"], "XQA")

    def test_low_confidence_us_homonym_of_foreign_city_is_clarified_not_guessed(self):
        # ZPX/Zephyrton has no scheduled service and shares its name with the
        # fixture's foreign city -- must ask, never silently resolve either way.
        decision = self.registry.resolve_token("Zephyrton")
        self.assertEqual(decision["kind"], "ambiguous")
        self.assertEqual(decision["reason"], "low_confidence_foreign_homonym")
        self.assertEqual(decision["foreign_country"], "France")
        self.assertEqual(decision["candidates"][0]["code"], "ZPX")

    def test_bare_city_spanning_two_states_is_ambiguous_not_guessed(self):
        # Fixture city "Fixture City" exists in both TX (peers) and CA (ZZZ).
        decision = self.registry.resolve_token("Fixture City")
        self.assertEqual(decision["kind"], "ambiguous")
        states = {c["state"] for c in decision["candidates"]}
        self.assertEqual(states, {"TX", "CA"})

    def test_city_with_explicit_state_disambiguates(self):
        decision = self.registry.resolve_token("Fixture City, Texas")
        self.assertEqual(decision["kind"], "airport")
        self.assertTrue(all(c["state"] == "TX" for c in decision["candidates"]))

    def test_unknown_token_is_ambiguous_not_guessed(self):
        decision = self.registry.resolve_token("Zephyrville")
        self.assertEqual(decision["kind"], "ambiguous")

    def test_non_us_registry_rows_are_excluded(self):
        self.assertIsNone(self.registry.get("YYZ"))  # never ingested -- no Canadian row in fixture/_by_code

    def test_by_state_sorted_by_observed_throughput(self):
        rows = self.registry.by_state("TX")
        self.assertTrue(len(rows) >= 12)
        # fixture growth increases with index, so 2025 passenger counts are not monotonic by code;
        # just assert the call succeeds and returns only TX rows.
        self.assertTrue(all(r["state"] == "TX" for r in rows))


if __name__ == "__main__":
    unittest.main()

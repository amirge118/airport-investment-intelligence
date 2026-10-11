"""A small, fully synthetic snapshot for deterministic unit/HTTP tests.

This fixture installs delay, route-segment, TAF, and CATS data that the real
snapshot does NOT have, specifically so the "computed"/"partial" code paths
for congestion, long-haul share, forecast, and revenue context can be proven
to work correctly. It is a controlled coverage fixture (EVALUATION.md), not
evidence of real broad US source coverage -- tests that use it say so.
"""

from __future__ import annotations

PEER_CODES = [f"XQ{letter}" for letter in "ABCDEFGHIJKL"]  # 12 peers, same cohort


def _registry_row(code: str, name: str, city: str, state: str, icao: str = "", scheduled: bool = True) -> dict:
    return {"code": code, "icao": icao, "name": name, "city": city, "country": "US",
            "region": f"US-{state}", "type": "medium_airport", "scheduled": scheduled}


def _foreign_registry_row(code: str, icao: str, name: str, city: str, country: str) -> dict:
    return {"code": code, "icao": icao, "name": name, "city": city, "country": country,
            "region": f"{country}-00", "type": "large_airport", "scheduled": True}


def _airport_year(code: str, year: int, passengers: int, seats: int) -> dict:
    return {"code": code, "year": year, "name": f"{code} Fixture Airport", "city": "Fixture City",
            "months": 12, "passengers": passengers, "seats": seats,
            "domestic_passengers": passengers, "domestic_seats": seats,
            "domestic_departures": passengers // 100, "international_departures": 0}


def build_snapshot() -> dict:
    registry = [_registry_row(code, f"{code} Fixture Airport", "Fixture City", "TX", icao=f"K{code}")
                for code in PEER_CODES]
    registry.append(_registry_row("ZZZ", "Incomplete Fixture Airport", "Fixture City", "CA", icao="KZZZ"))
    # A US town that shares its name with a real foreign city, with no scheduled
    # service -- proves the low-confidence-homonym clarification path.
    registry.append(_registry_row("ZPX", "Zephyrton Municipal Airport", "Zephyrton", "ME",
                                   icao="KZPX", scheduled=False))
    # A real-looking foreign airport, to prove out-of-scope detection is
    # data-driven (code and name/city lookups), not a hardcoded denylist.
    registry.append(_foreign_registry_row("ZFO", "LFZZ", "Zephyrton Foreign International Airport",
                                           "Zephyrton", "FR"))

    years = []
    base = 600_000
    for index, code in enumerate(PEER_CODES):
        growth = 1.0 + (index - 5) * 0.015  # spread growth rates across peers
        p2023 = base + index * 10_000
        p2024 = round(p2023 * growth)
        p2025 = round(p2024 * growth)
        seats = round(p2025 / 0.80)
        years.append(_airport_year(code, 2023, p2023, round(p2023 / 0.80)))
        years.append(_airport_year(code, 2024, p2024, round(p2024 / 0.80)))
        years.append(_airport_year(code, 2025, p2025, seats))
    # ZZZ: only 2024/2025 installed (no 2023) -> fails v2 eligibility, proves insufficient_evidence.
    years.append(_airport_year("ZZZ", 2024, 500_000, 650_000))
    years.append(_airport_year("ZZZ", 2025, 520_000, 660_000))

    delay_rows = [
        {"code": "XQA", "year": 2025, "month": 1, "flights": 1000, "delayed": 182,
         "cancelled": 15, "diverted": 13, "arrival_delay_minutes": 13_804,
         "carrier_delay_minutes": 800, "weather_delay_minutes": 21, "nas_delay_minutes": 60,
         "security_delay_minutes": 19, "late_aircraft_delay_minutes": 100},
        {"code": "XQB", "year": 2025, "month": 1, "flights": 1000, "delayed": 224,
         "cancelled": 20, "diverted": 16, "arrival_delay_minutes": 16_392,
         "carrier_delay_minutes": 795, "weather_delay_minutes": 30, "nas_delay_minutes": 75,
         "security_delay_minutes": 0, "late_aircraft_delay_minutes": 100},
        # XQC intentionally has no delay row -> "missing one side" case.
    ]

    route_segments = [
        {"origin": "XQA", "service_class": "F", "seats": 180, "departures": 400, "distance_miles": 2600},
        {"origin": "XQA", "service_class": "F", "seats": 180, "departures": 900, "distance_miles": 1200},
        {"origin": "XQA", "service_class": "F", "seats": 180, "departures": 300, "distance_miles": 3600},
        {"origin": "XQA", "service_class": "C", "seats": 0, "departures": 50, "distance_miles": 5000},  # cargo excluded
        # XQB intentionally has zero route segments -> insufficient_evidence.
    ]

    faa_taf = {"XQA": {"change_pct": 12.4, "enplanements_2025": 500_000, "enplanements_2030": 562_000}}
    cats_revenue = {"XQA": {"revenue_per_passenger": 18.5, "period": "2023"}}

    return {
        "version": "test-fixture-v1", "retrieved_at": "2026-01-01T00:00:00+00:00",
        "sources": {"airport": {"url": "https://example.invalid/fixture"}},
        "airport_registry": registry, "airport_years": years,
        "delay_rows": delay_rows, "route_segments": route_segments,
        "faa_taf": faa_taf, "cats_revenue": cats_revenue,
    }

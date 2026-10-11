"""What this build can answer: installed metric families and their gaps.

One place states which metric families are installed, partial, or missing
(and why), so chat answers, ``/health``, ``/api/capabilities`` and the
Airport Explorer preview cannot drift apart. Counts are computed from the
facts actually present, never asserted.
"""

from __future__ import annotations

from . import calculations
from ..facts import FactBundle, FactSource
from .highlights import preview_highlights

METRIC_STATUS = {
    "opportunity_score": {"installed": True},
    "absolute_passenger_change": {"installed": True},
    "congestion": {
        "installed": False,
        "reason": "No validated BTS Airline Delay Cause source run is active in this database.",
    },
    "longhaul_share": {
        "installed": False,
        "reason": "No validated BTS T-100 Segment source run is active in this database.",
    },
    "demand_pressure": {
        "installed": "partial",
        "reason": ("Observed BTS traffic is installed for every eligible airport. FAA TAF FY2025-FY2030 "
                   "forward enplanement outlook is installed for 712 of 795 US-registry-matched locid "
                   "pairs (the rest have a non-positive enplanement value in one of the two years); an "
                   "airport outside that set shows the forecast as not installed, never as zero."),
    },
    "revenue_context": {
        "installed": False,
        "reason": "No verified FAA CATS airport-year revenue extract is installed (cats.airports.faa.gov has no documented public API).",
    },
}


def coverage_report(facts: FactBundle) -> dict:
    """Per-family installed coverage counts, for /health and the written report."""
    years = {row["year"] for row in facts.get("airport_years", [])}
    eligible_v2 = calculations.screen_v2(facts)
    installed = facts.get("installed_coverage", {})
    delay = installed.get("congestion", {})
    routes = installed.get("longhaul_share", {})
    development = installed.get("development_context", {})
    delay_count = int(delay.get(
        "covered_airports", len(calculations.summarize_delay_rows(facts.get("delay_rows", [])))
    ))
    route_count = int(routes.get("covered_airports", len({
        row.get("origin") for row in facts.get("route_segments", [])
        if row.get("service_class") == "F" and row.get("departures", 0) > 0 and row.get("seats", 0) > 0
    })))
    development_count = int(development.get("covered_airports", len(facts.get("npias_development", {}))))
    report = {
        "opportunity_score": {
            "installed": True, "version": "v2", "covered_airports": len(eligible_v2),
            "period": "2023-2025", "source": calculations.BTS_SOURCE["url"],
        },
        "absolute_passenger_change": {
            "installed": True, "covered_airports": len(calculations.rank_by_absolute_change(facts)),
            "period": "2023-2025", "source": calculations.BTS_SOURCE["url"],
        },
        "congestion": {
            "installed": delay_count > 0, "covered_airports": delay_count,
            "period": str(delay.get("min_year")) if delay_count and delay.get("min_year") == delay.get("max_year")
            else None,
            **({} if delay_count else {"reason": METRIC_STATUS["congestion"]["reason"]}),
        },
        "longhaul_share": {
            "installed": route_count > 0, "covered_airports": route_count,
            "period": str(routes.get("min_year")) if route_count and routes.get("min_year") == routes.get("max_year")
            else None,
            **({} if route_count else {"reason": METRIC_STATUS["longhaul_share"]["reason"]}),
        },
        "demand_pressure": {
            "installed": "partial", "covered_airports": len(eligible_v2),
            "period": "2023, 2025 observed BTS; FY2025-FY2030 FAA TAF forecast",
            "forecast_covered_airports": len(facts.get("faa_taf", {})),
            "reason": METRIC_STATUS["demand_pressure"]["reason"],
        },
        "revenue_context": {**METRIC_STATUS["revenue_context"], "covered_airports": 0, "period": None},
        "development_context": {
            "installed": development_count > 0, "covered_airports": development_count,
            "positive_development_estimates": int(development.get("positive_development_estimates", 0)),
            "period": "2025-2029" if development_count else None,
            "reason": ("FAA NPIAS development estimates are planning context, not terminal-only CAPEX, "
                       "committed funding, or profitability evidence."),
        },
        "us_airport_identity": {
            "installed": True,
            "covered_airports": len({r["code"] for r in facts.get("airport_registry", []) if r["country"] == "US"}),
            "period": "current OurAirports snapshot",
        },
        "bts_years_installed": sorted(years),
    }
    return report


def airport_preview(facts: FactSource, code: str) -> dict | None:
    """Bounded US-airport summary for the guided Airport Explorer UI.

    Returns None for a malformed code or a non-US airport. Uses the same
    scoped fact bundle as a demand question, so the preview cannot drift
    from chat facts.
    """
    normalized = code.strip().upper()
    if len(normalized) != 3 or not normalized.isalpha():
        return None
    bundle = facts.for_analysis(
        {"assess_demand_pressure", "compare_airports", "longhaul_share"}, {normalized}
    )
    identity = next(
        (row for row in bundle["airport_registry"]
         if row["code"] == normalized and row["country"] == "US"),
        None,
    )
    if identity is None:
        return None
    # The SQLite bundle is already scoped to this code; the filter keeps the
    # preview correct for an unscoped in-memory fact bundle too.
    traffic = [row for row in bundle["airport_years"] if row["code"] == normalized]
    complete_growth = (
        {row["year"] for row in traffic if row["months"] == 12} == {2023, 2024, 2025}
    )
    return {
        "airport": identity,
        "traffic": traffic,
        "forecast": bundle["faa_taf"].get(normalized),
        "highlights": preview_highlights(bundle, normalized, traffic),
        "capabilities": {
            "traffic_growth": {"available": complete_growth, "period": "2023-2025"},
            "forward_outlook": {
                "available": normalized in bundle["faa_taf"], "period": "FY2025-FY2030",
            },
            "congestion": (
                {"available": True}
                if calculations.congestion_profile(bundle, normalized)["available"]
                else {"available": False, "reason": "Delay-cause extract not installed for this airport"}
            ),
            "longhaul_share": {
                "available": any(row.get("origin") == normalized for row in bundle.get("route_segments", [])),
                **({} if any(row.get("origin") == normalized for row in bundle.get("route_segments", []))
                   else {"reason": "Route segments not installed for this airport"}),
            },
            "development_context": {
                "available": normalized in bundle.get("npias_development", {}),
                "data": bundle.get("npias_development", {}).get(normalized),
                **({} if normalized in bundle.get("npias_development", {})
                   else {"reason": "FAA NPIAS context not installed for this airport"}),
            },
            "revenue_context": {"available": False, "reason": "FAA CATS extract not installed"},
        },
    }

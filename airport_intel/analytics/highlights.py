"""Headline figures for the Airport Explorer preview card.

``preview_highlights`` reduces one airport's scoped fact bundle (the same
bundle a chat question uses) to a handful of display-ready figures: scale and
growth, seat utilization, international mix, delays, long-haul share, network
breadth and top routes, a per-year series, and the FAA NPIAS airport profile
(hub size, ownership, service level, based aircraft, development estimate). Every figure comes
from installed public rows through the deterministic ``analytics`` helpers or
plain arithmetic on one airport's own rows; a figure whose source is missing
is ``None`` so the browser shows a gap instead of a guess.
"""

from __future__ import annotations

from . import calculations
from ..facts import FactBundle

LONGHAUL_THRESHOLD_MILES = 3000
HUB_SIZES = {"L": "Large hub", "M": "Medium hub", "S": "Small hub", "N": "Non-hub"}
OWNERSHIP = {"PU": "Public", "PR": "Private"}
SERVICE_LEVELS = {"P": "Primary commercial service", "CS": "Non-primary commercial service",
                  "R": "Reliever", "GA": "General aviation"}
TOP_ROUTES = 3


def _pct(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator * 100, 1) if denominator else None


def preview_highlights(bundle: FactBundle, code: str, traffic: list[dict]) -> dict:
    by_year = {row["year"]: row for row in traffic if row.get("months") == 12}
    latest_year = max(by_year) if by_year else None
    latest = by_year.get(latest_year) if latest_year else None

    growth = None
    if 2023 in by_year and 2025 in by_year and by_year[2023]["passengers"] > 0:
        growth = round(((by_year[2025]["passengers"] / by_year[2023]["passengers"]) ** 0.5 - 1) * 100, 1)

    international_share = None
    if latest and latest.get("international_departures") is not None and latest.get("domestic_departures") is not None:
        international_share = _pct(latest["international_departures"],
                                   latest["international_departures"] + latest["domestic_departures"])

    congestion = calculations.congestion_profile(bundle, code)
    delay = congestion["airports"][0] if congestion["available"] else None
    segments = bundle.get("route_segments", [])
    longhaul = calculations.longhaul_share(segments, code, LONGHAUL_THRESHOLD_MILES)

    # Scheduled passenger service only, matching the long-haul denominator.
    passenger_routes: dict[str, int] = {}
    for row in segments:
        if (row.get("origin") == code and row.get("destination") and row.get("service_class") == "F"
                and row.get("seats", 0) > 0 and row.get("departures", 0) > 0):
            passenger_routes[row["destination"]] = passenger_routes.get(row["destination"], 0) + row.get("passengers", 0)
    ranked_routes = sorted(passenger_routes.items(), key=lambda item: (-item[1], item[0]))
    top_route = ranked_routes[0] if ranked_routes else None

    # Year-by-year series for the explorer's year selector (complete years only).
    yearly, previous = [], None
    for y in sorted(by_year):
        row = by_year[y]
        yearly.append({
            "year": y, "passengers": row["passengers"],
            "seat_utilization_pct": _pct(row["passengers"], row["seats"]),
            "change_pct": round((row["passengers"] / previous - 1) * 100, 1) if previous else None,
        })
        previous = row["passengers"] or None

    international_passengers = None
    if latest and latest.get("domestic_passengers") is not None and latest["passengers"]:
        international_passengers = _pct(latest["passengers"] - latest["domestic_passengers"], latest["passengers"])

    npias = bundle.get("npias_development", {}).get(code) or {}
    return {
        "year": latest_year,
        "passengers": latest["passengers"] if latest else None,
        "two_year_cagr_pct": growth,
        "seat_utilization_pct": _pct(latest["passengers"], latest["seats"]) if latest else None,
        "international_departure_share_pct": international_share,
        "delayed_arrivals_pct": delay["delayed_pct"] if delay else None,
        "delay_minutes_per_arrival": delay.get("delay_minutes_per_completed_arrival") if delay else None,
        "delay_period": delay.get("period") if delay else None,
        "longhaul_share_pct": longhaul["percentage"] if longhaul["available"] else None,
        "longhaul_threshold_miles": LONGHAUL_THRESHOLD_MILES,
        "nonstop_destinations": len(passenger_routes) or None,
        "top_route": {"destination": top_route[0], "passengers": top_route[1]} if top_route else None,
        "top_routes": [{"destination": d, "passengers": n} for d, n in ranked_routes[:TOP_ROUTES]] or None,
        "yearly": yearly or None,
        "international_passenger_share_pct": international_passengers,
        "on_time_pct": delay.get("on_time_pct") if delay else None,
        "cancelled_pct": delay.get("cancelled_pct") if delay else None,
        "nas_delay_share_pct": delay.get("nas_pct") if delay else None,
        "weather_delay_share_pct": delay.get("weather_pct") if delay else None,
        "ownership": OWNERSHIP.get(npias.get("ownership")),
        "service_level": SERVICE_LEVELS.get(npias.get("service_level")),
        "based_aircraft": npias.get("based_aircraft"),
        "hub_size": HUB_SIZES.get(npias.get("hub_size")),
        "development_estimate_usd": npias.get("development_estimate"),
    }

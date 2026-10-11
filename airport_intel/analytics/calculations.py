"""Pure, deterministic analytics. No network calls or model-generated arithmetic.

Every function here takes typed data and returns typed results; the model
never touches these numbers. Scoring is v2 only (DESIGN.md "Scoring
methodology"); the superseded one-year v1 screen has been removed.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict

from ..facts import FactBundle


BTS_SOURCE = {"label": "BTS T-100 airport summary API", "url": "https://data.bts.gov/d/r495-tyji"}
ONTIME_SOURCE = {"label": "BTS On-Time Performance", "url": "https://www.transtats.bts.gov/TableInfo.asp?QO_fu146_anzr=b0-gvzr&gnoyr_VQ=FGJ"}

GROWTH_CLAMP_PCT = 20.0


def airport_name(facts: FactBundle, code: str) -> str:
    row = annual_index(facts).get((code, 2025)) or annual_index(facts).get((code, 2023))
    if row and row.get("name"):
        return row["name"]
    for item in facts.get("airport_registry", []):
        if item["code"] == code:
            return item["name"]
    return code


def annual_index(facts: FactBundle) -> dict[tuple[str, int], dict]:
    return {(row["code"], row["year"]): row for row in facts.get("airport_years", [])}


def cohort(passengers: int) -> str:
    if passengers >= 10_000_000:
        return "10M+ departures"
    if passengers >= 2_000_000:
        return "2M–10M departures"
    if passengers >= 500_000:
        return "0.5M–2M departures"
    return "0.1M–0.5M departures"


def percentile(value: float, values: list[float]) -> float:
    if not values:
        raise ValueError("Empty peer group")
    below = sum(item < value for item in values)
    equal = sum(item == value for item in values)
    return (below + equal / 2) / len(values) * 100


def _eligible_v2_rows(facts: FactBundle, us_codes: set[str]) -> list[dict]:
    index = annual_index(facts)
    eligible = []
    for code in {c for (c, _year) in index if (not us_codes or c in us_codes)}:
        rows = {year: index.get((code, year)) for year in (2023, 2024, 2025)}
        if any(row is None or row["months"] != 12 for row in rows.values()):
            continue
        p2023, p2024, p2025 = rows[2023]["passengers"], rows[2024]["passengers"], rows[2025]["passengers"]
        seats2025 = rows[2025]["seats"]
        if p2025 < 100_000 or p2023 <= 0 or seats2025 <= 0:
            continue
        utilization = p2025 / seats2025 * 100
        if not 0 <= utilization <= 100:
            continue
        cagr = (p2025 / p2023) ** 0.5 - 1
        g1 = (p2024 / p2023 - 1) * 100 if p2023 else None
        g2 = (p2025 / p2024 - 1) * 100 if p2024 else None
        reversal = g1 is not None and g2 is not None and (g1 > 0) != (g2 > 0) and abs(g2 - g1) > 15
        eligible.append({
            "code": code, "passengers_2023": p2023, "passengers_2024": p2024, "passengers_2025": p2025,
            "seats_2025": seats2025, "two_year_cagr_pct": cagr * 100,
            "two_year_cagr_pct_clamped": max(-GROWTH_CLAMP_PCT, min(GROWTH_CLAMP_PCT, cagr * 100)),
            "seat_utilization_pct": utilization,
            "annualized_incremental_passengers": (p2025 - p2023) / 2,
            "cohort": cohort(p2025), "growth_2023_2024_pct": g1, "growth_2024_2025_pct": g2,
            "growth_reversal": reversal,
        })
    return eligible


def screen_v2(facts: FactBundle, codes: set[str] | None = None, growth_weight: float = 0.40,
              scale_weight: float = 0.35) -> list[dict]:
    """Opportunity Screening Score v2 (DESIGN.md "Scoring methodology")."""
    us_codes = {item["code"] for item in facts.get("airport_registry", []) if item["country"] == "US"}
    eligible = _eligible_v2_rows(facts, us_codes)
    utilization_weight = 0.65 - growth_weight
    peers = defaultdict(list)
    for item in eligible:
        peers[item["cohort"]].append(item)
    national_incremental = [item["annualized_incremental_passengers"] for item in eligible]

    ranked = []
    for item in eligible:
        if codes is not None and item["code"] not in codes:
            continue
        group = peers[item["cohort"]]
        if len(group) < 12:
            continue
        growth_values = [peer["two_year_cagr_pct_clamped"] for peer in group]
        util_values = [peer["seat_utilization_pct"] for peer in group]
        growth_pctile = percentile(item["two_year_cagr_pct_clamped"], growth_values)
        util_pctile = percentile(item["seat_utilization_pct"], util_values)
        incremental_pctile = percentile(item["annualized_incremental_passengers"], national_incremental)
        score = (growth_weight * growth_pctile + utilization_weight * util_pctile
                 + scale_weight * incremental_pctile)
        median_growth = statistics.median(growth_values)
        counterfactual = (growth_weight * percentile(median_growth, growth_values)
                          + utilization_weight * util_pctile + scale_weight * incremental_pctile)
        ranked.append({
            **item, "name": airport_name(facts, item["code"]),
            "score": round(score, 1), "score_version": "v2",
            "peer_count": len(group),
            "growth_percentile": round(growth_pctile, 1),
            "utilization_percentile": round(util_pctile, 1),
            "incremental_percentile": round(incremental_pctile, 1),
            "weights": {"growth": growth_weight, "utilization": utilization_weight, "scale": scale_weight},
            "rank_reversal_counterfactual": (
                f"If 2-year CAGR reverts to the cohort median ({median_growth:+.1f}%), "
                f"score becomes {counterfactual:.1f} instead of {score:.1f}; terminal-specific "
                f"evidence could still disqualify the investment thesis either way."
            ),
        })
    return sorted(ranked, key=lambda row: (-row["score"], row["code"]))


def weight_sensitivity(facts: FactBundle, codes: set[str] | None, base_code: str,
                        growth_weights: tuple[float, ...] = (0.30, 0.40, 0.50)) -> dict:
    """One-day sensitivity test: vary growth weight 0.30-0.50, scale fixed at 0.35."""
    runs = {}
    ranks_by_weight = {}
    for weight in growth_weights:
        ranked = screen_v2(facts, codes=codes, growth_weight=weight, scale_weight=0.35)
        order = [row["code"] for row in ranked]
        runs[weight] = ranked
        ranks_by_weight[weight] = order
    base_positions = {weight: (order.index(base_code) if base_code in order else None)
                       for weight, order in ranks_by_weight.items()}
    all_orders = list(ranks_by_weight.values())
    reversal = len({tuple(order[:5]) for order in all_orders if order}) > 1
    return {
        "growth_weights": list(growth_weights),
        "base_code": base_code,
        "base_rank_by_weight": base_positions,
        "top5_rank_reversal": reversal,
        "runs": runs,
    }


def rank_by_absolute_change(facts: FactBundle, codes: set[str] | None = None) -> list[dict]:
    """rank_airports(absolute_passenger_change): P2025 - P2023, requested as an absolute metric."""
    us_codes = {item["code"] for item in facts.get("airport_registry", []) if item["country"] == "US"}
    eligible = _eligible_v2_rows(facts, us_codes)
    rows = []
    for item in eligible:
        if codes is not None and item["code"] not in codes:
            continue
        rows.append({
            "code": item["code"], "name": airport_name(facts, item["code"]),
            "passengers_2023": item["passengers_2023"], "passengers_2025": item["passengers_2025"],
            "absolute_change": item["passengers_2025"] - item["passengers_2023"],
            "two_year_cagr_pct": round(item["two_year_cagr_pct"], 2),
        })
    return sorted(rows, key=lambda row: (-row["absolute_change"], row["code"]))


def summarize_delay_rows(rows: list[dict]) -> dict[str, dict]:
    """Aggregate raw airport/month delay facts into comparable airport metrics.

    Storage returns source-shaped counts only. Percentage denominators and the
    policy of using the latest installed year belong here with the rest of the
    deterministic domain math.
    """
    totals: dict[tuple[str, int], dict[str, int]] = {}
    value_fields = (
        "flights", "delayed", "cancelled", "diverted", "arrival_delay_minutes",
        "carrier_delay_minutes", "weather_delay_minutes", "nas_delay_minutes",
        "security_delay_minutes", "late_aircraft_delay_minutes",
    )
    for source_row in rows:
        code = str(source_row.get("code", "")).upper()
        year = int(source_row.get("year", 0))
        if not code or year <= 0:
            continue
        aggregate = totals.setdefault((code, year), {field: 0 for field in value_fields})
        for field in value_fields:
            aggregate[field] += int(source_row.get(field, 0))

    latest_year = {}
    for code, year in totals:
        latest_year[code] = max(year, latest_year.get(code, year))

    summary = {}
    for code, year in sorted((code, year) for code, year in latest_year.items()):
        row = totals[(code, year)]
        flights = row["flights"]
        if flights <= 0:
            continue
        delayed = row["delayed"]
        cancelled = row["cancelled"]
        diverted = row["diverted"]
        completed = flights - cancelled - diverted
        on_time = max(0, flights - delayed - cancelled - diverted)
        cause_total = sum(row[field] for field in (
            "carrier_delay_minutes", "weather_delay_minutes", "nas_delay_minutes",
            "security_delay_minutes", "late_aircraft_delay_minutes",
        ))
        summary[code] = {
            "period": str(year), "scheduled_arrivals": flights,
            "delayed_arrivals": delayed, "cancelled_arrivals": cancelled,
            "diverted_arrivals": diverted,
            "delayed_pct": round(delayed / flights * 100, 2),
            "on_time_pct": round(on_time / flights * 100, 2),
            "cancelled_pct": round(cancelled / flights * 100, 2),
            "diverted_pct": round(diverted / flights * 100, 2),
            "delay_minutes_per_completed_arrival": (
                round(row["arrival_delay_minutes"] / completed, 2) if completed else None
            ),
            "nas_pct": round(row["nas_delay_minutes"] / cause_total * 100, 2) if cause_total else 0.0,
            "weather_pct": round(row["weather_delay_minutes"] / cause_total * 100, 2) if cause_total else 0.0,
            "cause_share_basis": "share of attributed delay minutes",
        }
    return summary


def congestion_profile(facts: FactBundle, code: str) -> dict:
    """One airport's comparable BTS arrival-delay profile, when installed."""
    summary = summarize_delay_rows(facts.get("delay_rows", []))
    if code not in summary:
        return {"available": False, "reason": f"No comparable BTS delay-share summary for: {code}."}
    value = summary[code]
    airport = {
        "code": code, "name": airport_name(facts, code),
        "period": value.get("period"),
        "scheduled_arrivals": value.get("scheduled_arrivals"),
        "delayed_pct": value["delayed_pct"], "on_time_pct": value["on_time_pct"],
        "cancelled_pct": value["cancelled_pct"],
        "diverted_pct": value.get("diverted_pct"),
        "delay_minutes_per_completed_arrival": value.get("delay_minutes_per_completed_arrival"),
        "nas_pct": value["nas_pct"], "weather_pct": value["weather_pct"],
        "cause_share_basis": value.get("cause_share_basis", "not specified by fixture"),
    }
    return {"available": True, "metric_kind": "arrival_delay_share", "airports": [airport]}


def compare_congestion(facts: FactBundle, codes: tuple[str, str]) -> dict:
    if len(codes) != 2 or codes[0] == codes[1]:
        raise ValueError("Comparison requires two distinct airports")
    profiles = [congestion_profile(facts, code) for code in codes]
    if all(profile["available"] for profile in profiles):
        return {
            "available": True,
            "metric_kind": "arrival_delay_share",
            "airports": [profile["airports"][0] for profile in profiles],
        }
    missing = [code for code, profile in zip(codes, profiles) if not profile["available"]]
    return {"available": False, "reason": f"No comparable BTS delay-share summary for: {', '.join(missing)}."}


def longhaul_share(segments: list[dict], code: str, threshold_miles: int = 3000) -> dict:
    if not 500 <= threshold_miles <= 6000:
        raise ValueError("Threshold must be 500-6000 statute miles")
    eligible = [row for row in segments if row.get("origin") == code and row.get("service_class") == "F" and row.get("seats", 0) > 0 and row.get("departures", 0) > 0]
    total = sum(row["departures"] for row in eligible)
    if total == 0:
        return {"available": False, "reason": f"No verified {code} passenger-service route segments in the installed data."}
    long = sum(row["departures"] for row in eligible if row["distance_miles"] >= threshold_miles)
    cargo = sum(
        row.get("departures", 0) for row in segments
        if row.get("origin") == code and row.get("service_class") in {"G", "P"}
        and row.get("departures", 0) > 0
    )
    non_scheduled_passenger = sum(
        row.get("departures", 0) for row in segments
        if row.get("origin") == code and row.get("service_class") == "L"
        and row.get("seats", 0) > 0 and row.get("departures", 0) > 0
    )
    return {"available": True, "threshold_miles": threshold_miles, "long_departures": long,
            "total_departures": total, "percentage": round(long / total * 100, 2),
            "cargo_departures_excluded": cargo,
            "non_scheduled_passenger_departures_excluded": non_scheduled_passenger,
            "denominator": "scheduled passenger/cargo service class F departures with positive seats"}


def airport_context(facts: FactBundle, code: str) -> dict:
    index = annual_index(facts)
    current, prior = index.get((code, 2025)), index.get((code, 2023))
    if not current or not prior:
        raise ValueError(f"No complete 2023/2025 BTS airport-year data for {code}")
    return {
        "code": code, "passengers_2025": current["passengers"], "passengers_2023": prior["passengers"],
        "seats_2025": current["seats"],
        "two_year_cagr_pct": round(((current["passengers"] / prior["passengers"]) ** 0.5 - 1) * 100, 2)
        if prior["passengers"] else None,
    }


def demand_pressure(facts: FactBundle, code: str, demand_uplift_pct: float = 5.0,
                     marginal_revenue_per_pax: float | None = None) -> dict:
    """Observed traffic + an explicitly analyst-set uplift scenario; never a measured shortfall."""
    if not math.isfinite(demand_uplift_pct) or not 0 <= demand_uplift_pct <= 20:
        raise ValueError("Assumed demand uplift must be 0-20%")
    if marginal_revenue_per_pax is not None and (not math.isfinite(marginal_revenue_per_pax) or not 0 <= marginal_revenue_per_pax <= 200):
        raise ValueError("Assumed marginal revenue must be $0-$200 per passenger")
    observed = airport_context(facts, code)
    hypothetical = round(observed["passengers_2025"] * demand_uplift_pct / 100)
    taf = facts.get("faa_taf", {}).get(code)
    development = facts.get("npias_development", {}).get(code)
    return {
        "observed": observed,
        "uplift_pct": demand_uplift_pct,
        "hypothetical_additional_passengers": hypothetical,
        "gross_revenue_scenario": round(hypothetical * marginal_revenue_per_pax) if marginal_revenue_per_pax is not None else None,
        "marginal_revenue_per_pax": marginal_revenue_per_pax,
        "forecast": {
            "available": True, "period": "FY2025-FY2030", "enplanement_change_pct": taf["change_pct"],
            "enplanements_2025": taf["enplanements_2025"], "enplanements_2030": taf["enplanements_2030"],
        } if taf else {"available": False, "reason": "No verified FAA TAF extract installed for this airport."},
        "development_context": {
            "available": True, "period": "2025-2029",
            "development_estimate": development["development_estimate"],
            "service_level": development.get("service_level", ""),
            "hub_size": development.get("hub_size", ""), "role": development.get("role", ""),
            "label": ("FAA NPIAS five-year development estimate for all eligible airport development; "
                      "not terminal-only CAPEX, committed funding, marginal cost, or profit."),
        } if development else {
            "available": False, "reason": "No FAA NPIAS development context installed for this airport."
        },
        "bottleneck_hypotheses": [
            {"candidate": "runway/airfield capacity", "status": "hypothesis"},
            {"candidate": "terminal gate/curb capacity", "status": "hypothesis"},
            {"candidate": "airspace/NAS constraint", "status": "hypothesis"},
            {"candidate": "airline fleet/network economics", "status": "hypothesis"},
            {"candidate": "weather exposure", "status": "hypothesis"},
        ],
    }


def revenue_context(facts: FactBundle, code: str, incremental_passengers: int,
                     capture_low: float = 0.5, capture_base: float = 1.0, capture_high: float = 1.5) -> dict:
    """Indicative gross revenue exposure from a historical CATS average; never a profit figure."""
    cats = facts.get("cats_revenue", {}).get(code)
    if not cats:
        return {"available": False, "reason": "No verified FAA CATS airport-year revenue extract installed."}
    revenue_per_pax = cats["revenue_per_passenger"]
    scenarios = {
        label: round(incremental_passengers * revenue_per_pax * capture)
        for label, capture in (("low", capture_low), ("base", capture_base), ("high", capture_high))
    }
    return {
        "available": True, "period": cats["period"], "revenue_per_passenger": revenue_per_pax,
        "scenarios": scenarios,
        "label": "Historical average airport revenue per passenger, not marginal contribution, NPV, or profit.",
    }

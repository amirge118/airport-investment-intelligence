"""Turn one validated operation plus its fact bundle into an answer.

One function per allowlisted operation: ranking, two-airport comparison,
long-haul share, and demand/revenue context. Each calls the deterministic
``analytics`` functions, decides that operation's ``result_state`` from the
evidence actually present, and writes the evidence-first message. No
function here reads storage, calls a model, or mutates session state
(``longhaul_answer`` only records the default threshold it applied on the
context it was given, so the follow-up slot reflects what was computed).
"""

from __future__ import annotations

from ..analytics import calculations
from ..analytics.coverage import METRIC_STATUS
from ..facts import FactBundle
from ..planning.planner import MAX_RANK_ROWS, Plan
from ..planning.resolver import Registry
from .answers import Answer, clarification
from .context import TurnContext

SUPPORTED_ASSUMPTION_KEYS = {"demand_uplift_pct", "marginal_revenue_per_pax"}
DEFAULT_LONGHAUL_THRESHOLD_MILES = 3000
DEFAULT_DEMAND_UPLIFT_PCT = 5.0


def answer_plan(plan: Plan, ctx: TurnContext, facts: FactBundle, registry: Registry) -> dict:
    """Dispatch the plan (or the inherited context's operations) to one answer."""
    # No analytics function in this build computes an arbitrary single
    # requested year: the score is a fixed 2023-2025 window and demand
    # pressure only observes 2023/2025. 2025 is the window's own anchor
    # year, so it is treated as consistent; any other explicit year must
    # be surfaced, never silently answered with the full-window figure.
    if ctx.period is not None and ctx.period != 2025:
        return clarification(
            plan,
            f"This screen computes a fixed 2023-2025 window (not a single-year {ctx.period} figure). "
            f"Do you want the installed 2023-2025 result instead, or a different supported question?",
        )
    ops = set(plan.operations) or set(ctx.operations)
    if "compare_airports" in ops:
        return compare_answer(plan, ctx, facts)
    if "rank_airports" in ops:
        return rank_answer(plan, ctx, facts, registry)
    if "longhaul_share" in ops:
        return longhaul_answer(plan, ctx, facts)
    if "revenue_context" in ops or "assess_demand_pressure" in ops:
        return demand_answer(plan, ctx, facts, want_revenue="revenue_context" in ops)
    return clarification(plan, "I resolved a US location but not a supported operation. Could you rephrase the question?")


def _ranking_scope(ctx: TurnContext, registry: Registry) -> tuple[set[str] | None, str]:
    """Candidate codes (None = all US) and a human-readable scope label."""
    if ctx.geographic_scope:
        kind, key = ctx.geographic_scope["kind"], ctx.geographic_scope.get("abbr") or ctx.geographic_scope.get("key")
        codes = ([r["code"] for r in registry.by_state(key)] if kind == "state"
                 else [r["code"] for r in registry.by_region(key)])
        return set(codes), f"{kind} scope: {key}"
    if ctx.airport_codes:
        return set(ctx.airport_codes), f"airports: {', '.join(ctx.airport_codes)}"
    return None, "all installed US airports"


def rank_answer(plan: Plan, ctx: TurnContext, facts: FactBundle, registry: Registry) -> dict:
    metric = (ctx.requested_metrics or ["opportunity_score"])[0]
    scope_codes, scope_desc = _ranking_scope(ctx, registry)

    if metric == "absolute_passenger_change":
        rows = calculations.rank_by_absolute_change(facts, codes=scope_codes)
        source_coverage = {"absolute_passenger_change": "observed"}
    else:
        rows = calculations.screen_v2(facts, codes=scope_codes)
        source_coverage = {"opportunity_score": "observed+derived"}

    if not rows:
        missing = [f"No airport in {scope_desc} meets the eligibility bar (3 complete years 2023-2025, "
                   f">=100k 2025 departing passengers, >=12 national cohort peers)."]
        return Answer(answer_kind="ranking", result_state="insufficient_evidence", message=missing[0],
                      resolved_scope=scope_desc, missing_evidence=missing, follow_up_context=ctx,
                      source_coverage=source_coverage).finalize(plan)

    capped = rows[:MAX_RANK_ROWS]
    if metric == "absolute_passenger_change":
        caveats = ["Passenger change is observed BTS traffic, not proof of terminal congestion, unmet demand, "
                   "NPV, or profit."]
    else:
        caveats = ["This is a diligence lead score, not proven terminal congestion, NPV, or profit.",
                   "Utilization is a weaker, imperfect service-pressure proxy than growth or scale."]
    top = capped[0]
    if metric == "absolute_passenger_change" and ctx.airport_codes and len(capped) == 1:
        # One named airport: state its change, not a one-row "ranking".
        message = (f"{top['code']} departing passengers 2023-2025: {top['passengers_2023']:,} -> "
                   f"{top['passengers_2025']:,} ({top['absolute_change']:+,}, "
                   f"{top['two_year_cagr_pct']:+.1f}% 2-yr CAGR).")
    else:
        message = (f"Top {len(capped)} of {len(rows)} eligible US airports in {scope_desc} by "
                   f"{'absolute passenger change 2023-2025' if metric=='absolute_passenger_change' else 'Opportunity Screening Score v2'}.")
    top_summary = {"code": top["code"],
                   "value": top["absolute_change"] if metric == "absolute_passenger_change" else top["score"],
                   # ranking_rows is capped, so the "of N eligible" total needs its own field.
                   "eligible_count": len(rows)}
    return Answer(
        answer_kind="ranking", result_state="computed", message=message, resolved_scope=scope_desc,
        metrics=[{"name": metric, "status": "computed", "data": top_summary}],
        ranking_rows=capped,
        evidence=[{"source": calculations.BTS_SOURCE, "period": "2023-2025", "metric": metric, "status": "observed"}],
        caveats=caveats, follow_up_context=ctx, source_coverage=source_coverage,
    ).finalize(plan)


def _comparison_fragment(metric: dict, codes: list[str]) -> str:
    if metric["status"] != "computed":
        return f"{metric['name']} unavailable ({metric.get('reason')})"
    if metric["name"] == "congestion":
        a, b = metric["data"]["airports"]
        return (f"congestion: {a['code']} {a['delayed_pct']}% delayed (BTS On-Time) vs "
                f"{b['code']} {b['delayed_pct']}% delayed")
    if metric["name"] == "absolute_passenger_change":
        by_code = {row["code"]: row for row in metric["data"]}
        parts = ", ".join(
            f"{code} {by_code[code]['passengers_2023']:,}->{by_code[code]['passengers_2025']:,} "
            f"({by_code[code]['absolute_change']:+,}, {by_code[code]['two_year_cagr_pct']:+.1f}% 2-yr CAGR)"
            for code in codes if code in by_code
        )
        return f"absolute_passenger_change 2023-2025: {parts}"
    return f"{metric['name']} computed"


def compare_answer(plan: Plan, ctx: TurnContext, facts: FactBundle) -> dict:
    codes = ctx.airport_codes[:2]
    if len(codes) < 2:
        if len(codes) == 1 and ctx.requested_metrics == ["congestion"]:
            result = calculations.congestion_profile(facts, codes[0])
            if result["available"]:
                profile = result["airports"][0]
                return Answer(
                    answer_kind="metric", result_state="computed",
                    message=(f"{codes[0]} congestion proxy: {profile['delayed_pct']}% of scheduled "
                             f"arrivals were delayed in {profile.get('period') or 'the installed period'}."),
                    resolved_scope=codes[0],
                    metrics=[{"name": "congestion", "status": "computed", "data": result}],
                    evidence=[{"source": calculations.ONTIME_SOURCE, "metric": "congestion", "status": "observed"}],
                    caveats=["A delay-share proxy measures operational on-time performance, not direct terminal crowding."],
                    follow_up_context=ctx, source_coverage={"congestion": "computed"},
                ).finalize(plan)
            reason = result["reason"]
            return Answer(
                answer_kind="metric", result_state="insufficient_evidence", message=reason,
                resolved_scope=codes[0],
                metrics=[{"name": "congestion", "status": "insufficient_evidence", "reason": reason}],
                missing_evidence=[reason], follow_up_context=ctx, source_coverage={"congestion": "missing"},
            ).finalize(plan)
        if not codes and ctx.requested_metrics == ["congestion"]:
            return clarification(plan, "Which US airport's congestion profile do you want?")
        return clarification(plan, "Comparisons need exactly two US airports. Which two did you mean?")
    metric_results = []
    evidence = []
    missing = []
    any_computed = False
    for metric in ctx.requested_metrics or ["congestion"]:
        if metric == "congestion":
            result = calculations.compare_congestion(facts, tuple(codes))
            if result["available"]:
                metric_results.append({"name": "congestion", "status": "computed", "data": result})
                evidence.append({"source": calculations.ONTIME_SOURCE, "metric": "congestion", "status": "observed"})
                any_computed = True
            else:
                metric_results.append({"name": "congestion", "status": "insufficient_evidence", "reason": result["reason"]})
                missing.append(result["reason"])
        elif metric == "absolute_passenger_change":
            rows = calculations.rank_by_absolute_change(facts, codes=set(codes))
            if len(rows) == len(codes):
                metric_results.append({"name": "absolute_passenger_change", "status": "computed", "data": rows})
                evidence.append({"source": calculations.BTS_SOURCE, "metric": "absolute_passenger_change", "status": "observed"})
                any_computed = True
            else:
                have = {row["code"] for row in rows}
                miss = [c for c in codes if c not in have]
                reason = f"Incomplete 2023-2025 BTS coverage for: {', '.join(miss)}."
                metric_results.append({"name": "absolute_passenger_change", "status": "insufficient_evidence", "reason": reason})
                missing.append(reason)

    if any_computed and missing:
        result_state = "partial"
    elif any_computed:
        result_state = "computed"
    else:
        result_state = "insufficient_evidence"

    message = f"Comparing {codes[0]} and {codes[1]}: " + "; ".join(
        _comparison_fragment(m, codes) for m in metric_results)
    return Answer(
        answer_kind="comparison", result_state=result_state, message=message,
        resolved_scope=f"{codes[0]} vs {codes[1]}", metrics=metric_results, evidence=evidence,
        caveats=["A delay-share proxy measures operational on-time performance, not direct terminal crowding."],
        missing_evidence=missing, follow_up_context=ctx,
        source_coverage={m["name"]: m["status"] for m in metric_results},
    ).finalize(plan)


def longhaul_answer(plan: Plan, ctx: TurnContext, facts: FactBundle) -> dict:
    if not ctx.airport_codes:
        return clarification(plan, "Which US airport's long-haul departure share do you want?")
    code = ctx.airport_codes[0]
    threshold = ctx.threshold_miles or DEFAULT_LONGHAUL_THRESHOLD_MILES
    result = calculations.longhaul_share(facts.get("route_segments", []), code, threshold)
    ctx.threshold_miles = threshold
    if result["available"]:
        return Answer(
            answer_kind="metric", result_state="computed", resolved_scope=code,
            message=f"{result['percentage']}% of {code} eligible passenger-service departures were >= {threshold} miles.",
            metrics=[{"name": "longhaul_share", "status": "computed", "data": result}],
            evidence=[{"source": "BTS T-100 Segment", "metric": "longhaul_share", "status": "observed"}],
            caveats=["Cargo-only operations are excluded from the denominator.",
                     "Departure share, not passenger share -- the two are different metrics."],
            follow_up_context=ctx, source_coverage={"longhaul_share": "observed"},
        ).finalize(plan)
    return Answer(
        answer_kind="metric", result_state="insufficient_evidence", resolved_scope=code,
        message=result["reason"],
        metrics=[{"name": "longhaul_share", "status": "insufficient_evidence", "reason": result["reason"]}],
        missing_evidence=[result["reason"]], follow_up_context=ctx, source_coverage={"longhaul_share": "missing"},
    ).finalize(plan)


def _coerce_float(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _demand_assumptions(overrides: dict) -> tuple[float, float | None, list[str], list[str]]:
    """Validated (uplift, marginal revenue, invalid keys, ignored unsupported keys)."""
    unsupported_keys = sorted(k for k in overrides if k not in SUPPORTED_ASSUMPTION_KEYS)
    invalid_keys = []

    uplift = DEFAULT_DEMAND_UPLIFT_PCT
    if "demand_uplift_pct" in overrides:
        coerced = _coerce_float(overrides["demand_uplift_pct"])
        if coerced is None or not (0 <= coerced <= 20):
            invalid_keys.append("demand_uplift_pct (must be a number 0-20)")
        else:
            uplift = coerced

    marginal_revenue = None
    if "marginal_revenue_per_pax" in overrides:
        coerced = _coerce_float(overrides["marginal_revenue_per_pax"])
        if coerced is None or not (0 <= coerced <= 200):
            invalid_keys.append("marginal_revenue_per_pax (must be a number $0-$200)")
        else:
            marginal_revenue = coerced
    return uplift, marginal_revenue, invalid_keys, unsupported_keys


def demand_answer(plan: Plan, ctx: TurnContext, facts: FactBundle, want_revenue: bool) -> dict:
    if not ctx.airport_codes:
        return clarification(plan, "Which US airport do you want demand/revenue context for?")
    code = ctx.airport_codes[0]

    # Analyst/model assumption_overrides: apply validated supported keys,
    # or visibly flag anything unsupported/invalid instead of silently
    # ignoring it.
    uplift, marginal_revenue, invalid_keys, unsupported_keys = _demand_assumptions(plan.assumption_overrides or {})
    if invalid_keys:
        return clarification(
            plan,
            f"The requested assumption override(s) are out of the supported range: "
            f"{'; '.join(invalid_keys)}. Please restate them within bounds, or drop them.",
        )

    override_caveats = []
    if unsupported_keys:
        override_caveats.append(
            f"Ignored unsupported assumption override key(s), not applied: {', '.join(unsupported_keys)}."
        )

    try:
        demand = calculations.demand_pressure(facts, code, demand_uplift_pct=uplift,
                                           marginal_revenue_per_pax=marginal_revenue)
    except ValueError as exc:
        reason = str(exc)
        return Answer(
            answer_kind="demand", result_state="insufficient_evidence", message=reason,
            resolved_scope=code,
            metrics=[{"name": "demand_pressure", "status": "insufficient_evidence", "reason": reason}],
            missing_evidence=[reason], follow_up_context=ctx, source_coverage={"demand_pressure": "missing"},
        ).finalize(plan)

    metrics = [{"name": "demand_pressure", "status": "computed", "data": demand}]
    missing = ["No public data source measures actual unserved demand (turned-away passengers) "
               "or proves a causal bottleneck; the uplift is an analyst assumption and the "
               "candidates below are hypotheses."]
    if not demand["forecast"]["available"]:
        missing.append(demand["forecast"]["reason"])
    caveats = ["Observed traffic and the uplift scenario are not a measured passenger shortfall or a proven cause.",
               "Bottleneck candidates are hypotheses, not findings, absent airport-specific primary evidence.",
               *override_caveats]
    evidence = [{"source": calculations.BTS_SOURCE, "metric": "demand_pressure", "status": "observed"}]
    if demand["forecast"]["available"]:
        taf_fragment = (f"FAA TAF FY2025-FY2030 forward outlook: "
                        f"{demand['forecast']['enplanement_change_pct']:+.1f}% enplanement change.")
    else:
        taf_fragment = "FAA TAF forward outlook: not installed for this airport."
    message = (f"{code} observed 2025 departing passengers: {demand['observed']['passengers_2025']:,} "
               f"({demand['observed']['two_year_cagr_pct']:+.1f}% 2-yr CAGR vs 2023). {taf_fragment}")
    if demand["development_context"]["available"]:
        development = demand["development_context"]
        message += (f" FAA NPIAS 2025-2029 development estimate: "
                    f"${development['development_estimate']:,} across eligible airport development.")
        evidence.append({
            "source": {"label": "FAA NPIAS 2025-2029 Appendix A",
                       "url": "https://www.faa.gov/airports/planning_capacity/npias/current"},
            "metric": "development_context", "status": "observed",
        })
        caveats.append(development["label"])

    if want_revenue:
        revenue = calculations.revenue_context(facts, code, demand["hypothetical_additional_passengers"])
        metrics.append({"name": "revenue_context", "status": "computed" if revenue["available"] else "insufficient_evidence",
                        "data": revenue if revenue["available"] else None,
                        "reason": None if revenue["available"] else revenue["reason"]})
        caveats.append("No CAPEX, incremental-contribution, or terminal-capacity-unlocked estimate is available; "
                       "this screen never computes profitability or NPV.")
        if not revenue["available"]:
            missing.append(revenue["reason"])
        message += (" " + (f"Indicative revenue scenario: {revenue['scenarios']}" if revenue["available"]
                    else f"Revenue scenario unavailable: {revenue['reason']}"))

    # Always `partial`, never `computed`: this operation structurally
    # blends observed traffic with an analyst-assumed uplift and
    # unproven bottleneck hypotheses, so it never delivers a complete,
    # fully-evidenced answer even when the TAF forecast happens to be
    # installed for this airport (EVALUATION.md E08: unmet-demand counts
    # and causal proof are absent).
    return Answer(
        answer_kind="demand", result_state="partial", message=message, resolved_scope=code,
        metrics=metrics, evidence=evidence,
        assumptions=[f"Assumed demand uplift: {demand['uplift_pct']}% (analyst-set, not observed)."],
        caveats=caveats, missing_evidence=missing, follow_up_context=ctx,
        source_coverage={m["name"]: m["status"] for m in metrics},
    ).finalize(plan)

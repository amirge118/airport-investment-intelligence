"""Plain-language reading of a computed answer: headline, summary, takeaway.

``insight(body)`` turns an answer envelope (``answers.Answer``) into a short
analyst-facing narrative: a headline, a 2-3 sentence summary of what the
evidence shows, and one takeaway sentence that points at the most decision-
relevant contrast in the result. It is deterministic and rule-based: it reads
only fields already present in the envelope (ranking rows, metric data,
follow-up context), never calls a model, never reads storage, and never
states a cause the evidence does not support. Answers that are not computed
or partial get ``None`` -- their ``message`` already says what is missing.
"""

from __future__ import annotations

from ..planning.resolver import STATE_NAME_TO_ABBR

_ABBR_TO_STATE = {abbr: name.title() for name, abbr in STATE_NAME_TO_ABBR.items()}
_COMPONENT_LABELS = {
    "growth_percentile": "growth versus similar-size peers",
    "utilization_percentile": "how full its seats are versus peers",
    "incremental_percentile": "passengers added (national scale)",
}


def insight(body: dict) -> dict | None:
    """Headline + summary + takeaway for a computed/partial answer, else ``None``."""
    if body.get("result_state") not in {"computed", "partial"}:
        return None
    kind = body.get("answer_kind")
    try:
        if kind == "ranking" and body.get("ranking_rows"):
            return _ranking(body)
        if kind == "comparison":
            return _comparison(body)
        if kind == "metric":
            return _longhaul(body)
        if kind == "demand":
            return _demand(body)
    except (KeyError, TypeError, ValueError, ZeroDivisionError, IndexError):
        # A narrative is a convenience over the evidence, never a reason to
        # fail the answer: a shape this module does not recognise yields none.
        return None
    return None


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------

def _scope_label(body: dict) -> str:
    scope = (body.get("follow_up_context") or {}).get("geographic_scope")
    if isinstance(scope, dict):
        if scope.get("kind") == "region" and scope.get("key"):
            return scope["key"].title()
        if scope.get("kind") == "state" and scope.get("abbr"):
            return _ABBR_TO_STATE.get(scope["abbr"], scope["abbr"])
    codes = (body.get("follow_up_context") or {}).get("airport_codes") or []
    if codes:
        return ", ".join(codes)
    return "the United States"


def _people(n: float) -> str:
    """Human-scale passenger count: 21.1M, 738K, 950."""
    n = float(n)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 10_000:
        return f"{n / 1_000:.0f}K"
    return f"{n:,.0f}"


def _top_share(percentile: float) -> str:
    """98.8th percentile -> 'the top 2%'; below the median -> 'below the peer median'."""
    if percentile >= 50:
        return f"the top {max(1, round(100 - percentile))}%"
    return "below the peer median"


def _rate(pct: float) -> str:
    return f"{pct:+.1f}% a year"


# --------------------------------------------------------------------------
# Rankings
# --------------------------------------------------------------------------

def _contributions(row: dict) -> dict[str, float]:
    weights = row["weights"]
    return {
        "growth_percentile": weights["growth"] * row["growth_percentile"],
        "utilization_percentile": weights["utilization"] * row["utilization_percentile"],
        "incremental_percentile": weights["scale"] * row["incremental_percentile"],
    }


def _ranking(body: dict) -> dict:
    rows = body["ranking_rows"]
    scope = _scope_label(body)
    if "score" not in rows[0]:
        return _absolute_ranking(rows, scope)

    top = rows[0]
    headline = f"{scope}: {top['code']} is the strongest expansion lead"
    if len(rows) == 1:
        summary = (f"{top['code']} scores {top['score']} on the Opportunity Screening Score: "
                   f"passengers grew {_rate(top['two_year_cagr_pct'])} since 2023, "
                   f"{_top_share(top['growth_percentile'])} of its size class, and seats were "
                   f"{top['seat_utilization_pct']:.1f}% full in 2025.")
        return {"headline": f"{scope}: screening score {top['score']}", "summary": summary,
                "takeaway": _single_takeaway(top)}

    driver = max(_contributions(top).items(), key=lambda item: item[1])[0]
    summary = [f"{top['code']} leads {len(rows)} ranked airports in {scope} with a score of {top['score']}, "
               f"driven mostly by {_COMPONENT_LABELS[driver]}"]
    summary[0] += {
        "growth_percentile": (f" ({_rate(top['two_year_cagr_pct'])} since 2023, "
                              f"{_top_share(top['growth_percentile'])} of its size class)."),
        "incremental_percentile": (f" (about {_people(top['annualized_incremental_passengers'])} more "
                                   f"departing passengers a year since 2023)."),
        "utilization_percentile": f" (seats {top['seat_utilization_pct']:.1f}% full in 2025).",
    }[driver]
    second = rows[1]
    summary.append(f"{second['code']} follows at {second['score']} "
                   f"({_rate(second['two_year_cagr_pct'])}, seats {second['seat_utilization_pct']:.1f}% full).")
    strong = [row["code"] for row in rows if row["score"] >= 70]
    if 1 < len(strong) < len(rows):
        summary.append(f"{len(strong)} of {len(rows)} score 70 or higher; the rest trail on growth, "
                       f"utilization or scale.")
    return {"headline": headline, "summary": " ".join(summary), "takeaway": _ranking_takeaway(rows)}


def _ranking_takeaway(rows: list[dict]) -> str:
    top = rows[0]
    if top.get("growth_reversal"):
        return (f"Read {top['code']}'s lead with care: its growth changed direction sharply between "
                f"2024 and 2025, so one route or airline decision may be driving the score.")

    fastest = max(rows, key=lambda row: row["two_year_cagr_pct"])
    if fastest is not top:
        rank = rows.index(fastest) + 1
        weakest = min(_contributions(fastest).items(), key=lambda item: item[1])[0]
        reason = {
            "utilization_percentile": ("its seats are among the least full of its peers, so new capacity "
                                       "is absorbing the growth rather than straining it"),
            "incremental_percentile": "it is growing from a small base, so it adds relatively few passengers",
            "growth_percentile": "its peers grew even faster",
        }[weakest]
        return (f"{fastest['code']} is the fastest grower ({_rate(fastest['two_year_cagr_pct'])}) yet ranks "
                f"#{rank}: {reason}. Speed alone is not a terminal case.")

    largest = max(rows, key=lambda row: row["passengers_2025"])
    if largest is not top:
        ratio = largest["passengers_2025"] / top["passengers_2025"]
        rank = rows.index(largest) + 1
        return (f"Size is not the signal: {largest['code']} carries {ratio:.0f}x {top['code']}'s passengers "
                f"but ranks #{rank}, because {top['code']} is pulling away from airports its own size.")

    return (f"{top['code']} leads on growth and scale at once, so the next question is whether the "
            f"terminal, rather than the runway or airline economics, is what limits it.")


def _single_takeaway(row: dict) -> str:
    weakest = min(_contributions(row).items(), key=lambda item: item[1])[0]
    return (f"Its weakest driver is {_COMPONENT_LABELS[weakest]}; that is where a skeptical "
            f"investment committee will push first.")


def _absolute_ranking(rows: list[dict], scope: str) -> dict:
    top = rows[0]
    if len(rows) == 1:
        direction = "more" if top["absolute_change"] >= 0 else "fewer"
        return {
            "headline": f"{top['code']}: departing passengers 2023-2025",
            "summary": (f"{top['code']} went from {_people(top['passengers_2023'])} to "
                        f"{_people(top['passengers_2025'])} departing passengers "
                        f"({_people(abs(top['absolute_change']))} {direction}, "
                        f"{_rate(top['two_year_cagr_pct'])})."),
            "takeaway": "One airport has no peer context; compare it with a similar airport to judge the change.",
        }
    summary = (f"{top['code']} added the most departing passengers in {scope} between 2023 and 2025: "
               f"{_people(top['passengers_2023'])} to {_people(top['passengers_2025'])} "
               f"({_people(top['absolute_change'])} more, {_rate(top['two_year_cagr_pct'])}).")
    fastest = max(rows, key=lambda row: row["two_year_cagr_pct"])
    if fastest is not top:
        takeaway = (f"Volume and speed disagree: {fastest['code']} grew fastest "
                    f"({_rate(fastest['two_year_cagr_pct'])}) but {top['code']} added more people, and "
                    f"terminals are sized for people, not percentages.")
    else:
        takeaway = f"{top['code']} wins on both absolute volume and growth rate in this scope."
    return {"headline": f"{scope}: who added the most passengers", "summary": summary, "takeaway": takeaway}


# --------------------------------------------------------------------------
# Comparisons, long-haul, demand
# --------------------------------------------------------------------------

def _comparison(body: dict) -> dict | None:
    computed = {m["name"]: m["data"] for m in body.get("metrics", []) if m.get("status") == "computed"}
    codes = (body.get("follow_up_context") or {}).get("airport_codes", [])[:2]
    if len(codes) < 2 or not computed:
        return None
    a_code, b_code = codes
    summary, takeaway = [], None

    if "congestion" in computed:
        a, b = computed["congestion"]["airports"]
        worse, better = (a, b) if a["delayed_pct"] >= b["delayed_pct"] else (b, a)
        gap = worse["delayed_pct"] - better["delayed_pct"]
        summary.append(f"{worse['code']} had {worse['delayed_pct']}% of flights delayed versus "
                       f"{better['code']}'s {better['delayed_pct']}%, a gap of {gap:.1f} points.")
        takeaway = ("Delay share reflects the airfield, airspace and airline schedule; it does not show "
                    "that the terminal is the bottleneck, so it opens the diligence question rather than "
                    "answering it.")

    if "absolute_passenger_change" in computed:
        by_code = {row["code"]: row for row in computed["absolute_passenger_change"]}
        a, b = by_code[a_code], by_code[b_code]
        summary.append(f"Between 2023 and 2025 {a_code} went from {_people(a['passengers_2023'])} to "
                       f"{_people(a['passengers_2025'])} departing passengers ({_rate(a['two_year_cagr_pct'])}), "
                       f"and {b_code} from {_people(b['passengers_2023'])} to {_people(b['passengers_2025'])} "
                       f"({_rate(b['two_year_cagr_pct'])}).")
        faster = a if a["two_year_cagr_pct"] >= b["two_year_cagr_pct"] else b
        bigger_add = a if a["absolute_change"] >= b["absolute_change"] else b
        if takeaway is None:
            if a["absolute_change"] < 0 and b["absolute_change"] < 0:
                takeaway = (f"Both airports carried fewer passengers in 2025 than in 2023, so neither shows "
                            f"a growth-driven terminal case; {faster['code']} is holding up slightly better.")
            elif faster is not bigger_add:
                takeaway = (f"{faster['code']} is growing faster, but {bigger_add['code']} added more people: "
                            f"{_people(bigger_add['absolute_change'])} versus "
                            f"{_people(faster['absolute_change'])}.")
            else:
                takeaway = (f"{faster['code']} leads on both growth rate and passengers added, so it is the "
                            f"stronger traffic case of the two.")

    if not summary:
        return None
    return {"headline": f"{a_code} vs {b_code}", "summary": " ".join(summary), "takeaway": takeaway}


def _longhaul(body: dict) -> dict | None:
    metric = next((m for m in body.get("metrics", [])
                   if m.get("name") == "longhaul_share" and m.get("status") == "computed"), None)
    if not metric:
        return None
    data = metric["data"]
    code = body.get("resolved_scope")
    pct = data["percentage"]
    summary = (f"{pct}% of {code}'s scheduled passenger departures "
               f"({data['long_departures']:,} of {data['total_departures']:,}) flew "
               f"{data['threshold_miles']:,} miles or more.")
    if pct <= 0:
        takeaway = f"{code} has no long-haul passenger service at this cut-off; its network is short and medium haul."
    else:
        one_in = round(100 / pct)
        takeaway = (f"Roughly 1 in {one_in} departures is long-haul at a {data['threshold_miles']:,}-mile cut-off; "
                    f"ask the same question with a different threshold to see how sensitive that is.")
    return {"headline": f"{code}: long-haul departure share", "summary": summary, "takeaway": takeaway}


def _demand(body: dict) -> dict | None:
    metric = next((m for m in body.get("metrics", [])
                   if m.get("name") == "demand_pressure" and m.get("status") == "computed"), None)
    if not metric:
        return None
    data = metric["data"]
    code = body.get("resolved_scope")
    observed = data["observed"]
    cagr = observed["two_year_cagr_pct"]
    summary = [f"{code} handled {_people(observed['passengers_2025'])} departing passengers in 2025, "
               f"{_rate(cagr)} since 2023."]
    forecast = data.get("forecast") or {}
    if forecast.get("available"):
        change = forecast["enplanement_change_pct"]
        summary.append(f"The FAA's Terminal Area Forecast expects {abs(change):.1f}% "
                       f"{'more' if change >= 0 else 'fewer'} enplanements by FY2030.")
        annual_outlook = ((1 + change / 100) ** (1 / 5) - 1) * 100
        if cagr > annual_outlook + 1:
            takeaway = (f"Recent growth is running ahead of the FAA's outlook: either the FAA is conservative "
                        f"or the last two years were a rebound, and the terminal case depends on which.")
        elif cagr < annual_outlook - 1:
            takeaway = ("The FAA expects faster growth than the last two years delivered, so the demand case "
                        "rests on a forecast, not on observed momentum.")
        else:
            takeaway = ("Observed growth and the FAA outlook roughly agree, so the open question is not demand "
                        "but whether the terminal, rather than the runway or airlines, is the constraint.")
    else:
        takeaway = ("No public dataset counts passengers turned away, so unmet demand here is an inference; "
                    "the next step is airport-specific capacity evidence.")
    summary.append("Public data cannot show passengers turned away or prove which constraint binds.")
    return {"headline": f"{code}: demand outlook", "summary": " ".join(summary), "takeaway": takeaway}

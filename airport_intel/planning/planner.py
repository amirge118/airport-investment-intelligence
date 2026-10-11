"""Intent/slot planning: the typed ``Plan`` contract, its allowlist/bounds
gate, a narrow keyword fallback, and ``plan_question`` -- the policy that
tries the optional Gemini proposal first and falls back visibly. The model
never bypasses `validate_plan` -- only validated, allowlisted operations
ever run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import llm
from .resolver import STATE_NAME_TO_ABBR, VALID_STATE_ABBRS

if TYPE_CHECKING:
    from ..conversation.context import TurnContext

ALLOWLIST_OPERATIONS = {
    "resolve_airports", "rank_airports", "compare_airports",
    "longhaul_share", "assess_demand_pressure", "revenue_context",
}
ALLOWLIST_METRICS = {
    "opportunity_score", "absolute_passenger_change", "congestion",
    "longhaul_share", "demand_pressure", "revenue_context",
}
# The operation(s) that compute each metric; mirrors keyword_fallback_plan.
METRIC_OPERATIONS = {
    "opportunity_score": ["rank_airports"], "absolute_passenger_change": ["rank_airports"],
    "congestion": ["compare_airports"], "longhaul_share": ["longhaul_share"],
    "demand_pressure": ["assess_demand_pressure"],
    "revenue_context": ["assess_demand_pressure", "revenue_context"],
}
MAX_OPERATIONS = 3
MAX_ENTITIES = 10
MAX_RANK_ROWS = 20
MIN_THRESHOLD_MILES, MAX_THRESHOLD_MILES = 500, 6000


class PlanRejected(ValueError):
    pass


@dataclass
class Plan:
    intent: str
    operations: list[str] = field(default_factory=list)
    requested_metrics: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    geographic_scope: str | None = None
    period: int | None = None
    threshold_miles: int | None = None
    assumption_overrides: dict = field(default_factory=dict)
    ambiguities: list[str] = field(default_factory=list)
    unsupported_metric: str | None = None
    planner_source: str = "keyword_fallback"
    # Why a configured model did not plan this turn (server-written, never model text).
    fallback_reason: str | None = None
    raw_text: str = ""


def mentions_replacement(text: str) -> bool:
    return bool(_INSTEAD_RE.search(text))


def validate_plan(plan: Plan) -> Plan:
    """Server-side gate: reject anything outside the allowlisted contract.

    Assumes types are already correct (see `parse_model_proposal` for the
    model-facing entry point, which type-checks before this ever runs).
    """
    if len(plan.operations) > MAX_OPERATIONS:
        raise PlanRejected(f"Plan requested {len(plan.operations)} operations; at most {MAX_OPERATIONS} allowed.")
    for op in plan.operations:
        if op not in ALLOWLIST_OPERATIONS:
            raise PlanRejected(f"Operation '{op}' is not an allowlisted tool.")
    for metric in plan.requested_metrics:
        if metric not in ALLOWLIST_METRICS:
            raise PlanRejected(f"Metric '{metric}' is not an allowlisted metric.")
    if len(plan.entities) > MAX_ENTITIES:
        raise PlanRejected(f"Plan named {len(plan.entities)} entities; at most {MAX_ENTITIES} allowed.")
    if plan.threshold_miles is not None and not (MIN_THRESHOLD_MILES <= plan.threshold_miles <= MAX_THRESHOLD_MILES):
        raise PlanRejected(f"Threshold {plan.threshold_miles} outside {MIN_THRESHOLD_MILES}-{MAX_THRESHOLD_MILES} statute miles.")
    if plan.period is not None and not (2023 <= plan.period <= 2025):
        raise PlanRejected(f"Period {plan.period} outside the installed 2023-2025 BTS coverage.")
    return plan


def _expect_list_of_str(value, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PlanRejected(f"Field '{field_name}' must be a list of strings.")
    return value


def _expect_optional_str(value, field_name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise PlanRejected(f"Field '{field_name}' must be a string or null.")
    return value


def _expect_optional_int(value, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):  # bool is a subclass of int -- reject explicitly
        raise PlanRejected(f"Field '{field_name}' must be a number or null.")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise PlanRejected(f"Field '{field_name}' must be a whole number or null.")


def _expect_overrides(value) -> dict:
    """Assumption overrides as {key: value}. The shared schema sends a list of
    {"key", "value"} pairs (strict decoding allows no free-form object); a plain
    object is accepted too."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if not isinstance(value, list) or not all(
            isinstance(item, dict) and set(item) == {"key", "value"} and isinstance(item["key"], str)
            for item in value):
        raise PlanRejected("Field 'assumption_overrides' must be key/value pairs.")
    return {item["key"]: item["value"] for item in value}


def parse_model_proposal(proposal, raw_text: str, source: str) -> Plan:
    """The only entry point for model output: type-checks every field, then
    runs the same allowlist/bounds gate as the keyword fallback. Any
    malformed or over-budget field rejects the WHOLE proposal (never a
    partial plan built from the parts that happened to look valid) so the
    caller falls back to the deterministic planner visibly. ``source`` is the
    ``provider:model`` label recorded as the plan's ``planner_source``.

    A model-proposed `geographic_scope` is folded into `entities` so it is
    resolved through the US registry like any other entity -- it can never
    become scope-less all-US ranking by being silently ignored. A model-
    proposed `out_of_scope_place` is folded in the same way: the registry,
    not the model, decides whether a place is foreign (Paris, TX is not).
    """
    if not isinstance(proposal, dict):
        raise PlanRejected("Model proposal was not a JSON object.")
    intent = proposal.get("intent", "unknown")
    if not isinstance(intent, str):
        raise PlanRejected("Field 'intent' must be a string.")
    operations = _expect_list_of_str(proposal.get("operations"), "operations")
    for op in operations:
        if op not in ALLOWLIST_OPERATIONS:
            raise PlanRejected(f"Unknown operation '{op}' proposed.")
    requested_metrics = _expect_list_of_str(proposal.get("requested_metrics"), "requested_metrics")
    for metric in requested_metrics:
        if metric not in ALLOWLIST_METRICS:
            raise PlanRejected(f"Unknown metric '{metric}' proposed.")
    entities = _expect_list_of_str(proposal.get("entities"), "entities")
    geographic_scope = _expect_optional_str(proposal.get("geographic_scope"), "geographic_scope")
    period = _expect_optional_int(proposal.get("period"), "period")
    threshold_miles = _expect_optional_int(proposal.get("threshold_miles"), "threshold_miles")
    assumption_overrides = _expect_overrides(proposal.get("assumption_overrides"))
    ambiguities = _expect_list_of_str(proposal.get("ambiguities"), "ambiguities")
    unsupported_metric = _canonical_unsupported_metric(
        _expect_optional_str(proposal.get("unsupported_metric"), "unsupported_metric"))
    out_of_scope_place = _expect_optional_str(proposal.get("out_of_scope_place"), "out_of_scope_place")

    for place in (geographic_scope, out_of_scope_place):
        if place and place not in entities:
            entities = entities + [place]
    # The server always resolves entities, and it owns which function computes
    # a metric: a model that named the metric but no actionable operation gets
    # the same operation the keyword planner would use.
    operations = [op for op in operations if op != "resolve_airports"]
    if not operations:
        operations = list(dict.fromkeys(op for metric in requested_metrics
                                        for op in METRIC_OPERATIONS[metric]))
    if "revenue_context" in operations and "assess_demand_pressure" not in operations:
        operations.insert(operations.index("revenue_context"), "assess_demand_pressure")

    plan = Plan(
        intent=intent, operations=operations, requested_metrics=requested_metrics,
        entities=entities, geographic_scope=geographic_scope, period=period,
        threshold_miles=threshold_miles, assumption_overrides=assumption_overrides,
        ambiguities=ambiguities, unsupported_metric=unsupported_metric,
        planner_source=source,
        raw_text=raw_text,
    )
    return validate_plan(plan)


def _canonical_unsupported_metric(value: str | None) -> str | None:
    """Map the model's free-text metric name to a fixed key; the model's own
    wording never reaches the user-facing message. Blank means none."""
    if value is None or not value.strip():
        return None
    return "security_wait_time" if _TSA_RE.search(value.replace("_", " ")) else "unlisted_metric"


_AIRPORT_SUFFIX_RE = re.compile(r"\s+(international\s+)?(airport|airfield)$", re.I)


def _mentions(text: str, phrase: str) -> bool:
    return bool(phrase) and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.I) is not None


def _is_grounded(entity: str, text: str, context: TurnContext) -> bool:
    """True when a model-proposed place is traceable to the question or the
    validated prior turn: the user wrote it (a state name or its USPS code
    counts as the same place), or it names the inherited airport/scope."""
    if "," in entity:
        return all(_is_grounded(part.strip(), text, context) for part in entity.split(",") if part.strip())
    place = _AIRPORT_SUFFIX_RE.sub("", entity.strip())
    lower, upper = place.lower(), place.upper()
    if _mentions(text, place):
        return True
    if lower in STATE_NAME_TO_ABBR and re.search(rf"\b{STATE_NAME_TO_ABBR[lower]}\b", text):
        return True
    if upper in VALID_STATE_ABBRS and any(
            abbr == upper and _mentions(text, name) for name, abbr in STATE_NAME_TO_ABBR.items()):
        return True
    if upper in context.airport_codes:
        return True
    scope = context.geographic_scope or {}
    if scope.get("kind") == "state":
        return scope["abbr"] in (upper, STATE_NAME_TO_ABBR.get(lower))
    if scope.get("kind") == "region":
        return lower == scope["key"]
    return False


def check_grounding(plan: Plan, text: str, context: TurnContext) -> Plan:
    """Reject a schema-valid model plan that is still wrong in a checkable way:
    a place the user never mentioned, or missing the TSA refusal the
    deterministic signal requires. The caller falls back visibly."""
    for entity in plan.entities:
        if not _is_grounded(entity, text, context):
            raise PlanRejected(f"Model proposed '{entity}', which the question does not mention.")
    if _TSA_RE.search(text) and plan.unsupported_metric is None:
        raise PlanRejected("Question asks for a security wait metric the model did not flag as unsupported.")
    return plan


_TSA_RE = re.compile(r"\btsa\b|security (wait|line|queue)", re.I)
_LONGHAUL_RE = re.compile(r"long[\s-]?haul|(\d[\d,]*)\s*mile", re.I)
_CONGESTION_RE = re.compile(r"\bdelay(ed|s)?\b|congest|on-time|on time", re.I)
_REVENUE_RE = re.compile(r"\bprofit(able)?\b|\bnpv\b|revenue|worth building|marginal contribution", re.I)
_DEMAND_RE = re.compile(r"unserved|unmet demand|\bdemand\b|turned away|why (is|are|does)", re.I)
_RANK_RE = re.compile(r"deserve a closer look|rank|best airports?|which .*(grow|invest)|expansion|renovation|opportunit", re.I)
_ABS_CHANGE_RE = re.compile(
    r"passenger volume (grow|change)|grew the most|grow most"
    r"|\b(grow(th)?|grew|chang(e|ed)|increas(e|ed)|declin(e|ed)) since\b", re.I)
_COMPARE_RE = re.compile(r"\bcompare\b|\bvs\.?\b|\bversus\b|equally (delayed|congested)", re.I)
_THRESHOLD_NUM_RE = re.compile(r"(\d[\d,]{2,})\s*mile", re.I)
_BARE_NUMBER_RE = re.compile(r"^\s*(\d[\d,]{2,})\s*(miles?)?\s*\??\s*$", re.I)
_INSTEAD_RE = re.compile(r"\binstead\b|\brather than\b|\breplace\b", re.I)
_INJECTION_RE = re.compile(r"ignore (your|the|all|previous)|disregard (your|the)|pretend (that|you)", re.I)

# City shorthand that is also a state code ("LA" = Louisiana). Extracted as an
# entity here; the orchestrator expands it only for airport-level questions.
CITY_ABBREVIATIONS = {"LA": "Los Angeles"}

_CANDIDATE_TOKEN_RE = re.compile(
    r"\b(?:" + "|".join(CITY_ABBREVIATIONS) + r")\b(?![a-z])"   # city shorthand, e.g. LA
    r"|[A-Z]{3}\b"                               # IATA-looking codes
    r"|[A-Z][a-z]+(?:\s[A-Z][a-z]+)*,\s[A-Z][a-z]+(?:\s[A-Z][a-z]+)*"  # "City, State"
    r"|[A-Z][a-z]+(?:\s[A-Z][a-z]+)*"            # Capitalized place name
)
# Sentence-structural words that are capitalized only by position (start of
# sentence/question), not because they name a place. A single-word match
# against this list is dropped; multi-word matches (e.g. "New England") are
# never affected because the regex already consumes them as one token.
_STRUCTURAL_WORDS = {
    "which", "what", "how", "where", "when", "why", "who", "compare", "and",
    "in", "at", "is", "are", "would", "does", "do", "the", "this", "that",
    "with", "for", "on", "after", "before", "ignore", "disregard", "say",
    "would", "share", "average", "closer", "look", "equally", "exactly",
    "right", "now", "there", "was", "were",
    # Sentence-initial imperatives/auxiliaries ("Rank airports in Texas ..."):
    # capitalized by position, and several are substrings of real airport
    # names (Frank Wiley Field, Show Low Regional) that must never resolve.
    "rank", "show", "list", "give", "tell", "find", "top", "best", "identify", "name",
    "highlight", "look", "can", "could", "please", "has", "have", "did", "did", "will",
    "should", "need", "want", "see", "get",
}


def _trim_structural_words(token: str) -> str:
    words = token.split(" ")
    while words and words[0].rstrip(",").lower() in _STRUCTURAL_WORDS:
        words.pop(0)
    while words and words[-1].rstrip(",").lower() in _STRUCTURAL_WORDS:
        words.pop()
    return " ".join(words)


def _candidate_entities(text: str) -> list[str]:
    seen: list[str] = []
    for match in _CANDIDATE_TOKEN_RE.finditer(text):
        token = _trim_structural_words(match.group(0).strip())
        if not token or token.lower() == "tsa":
            continue
        if " " not in token and token.lower() in _STRUCTURAL_WORDS:
            continue
        if token not in seen:
            seen.append(token)
    return seen


def keyword_fallback_plan(text: str) -> Plan:
    """Narrow deterministic planner: allowlisted keywords, exact US IATA codes,
    state names/abbreviations, and simple follow-up overrides only. Anything
    else returns ambiguities so the orchestrator can ask for a rephrase.
    """
    plan = Plan(intent="unknown", planner_source="keyword_fallback", raw_text=text)
    stripped = text.strip()

    if _INJECTION_RE.search(text):
        plan.ambiguities.append("Message content looks like an instruction, not a question; treating it as untrusted text.")

    if _TSA_RE.search(text):
        plan.intent = "unsupported_metric"
        plan.unsupported_metric = "security_wait_time"
        return plan

    bare_number = _BARE_NUMBER_RE.match(stripped)
    if bare_number:
        plan.intent = "follow_up_override"
        plan.threshold_miles = int(bare_number.group(1).replace(",", ""))
        return plan

    entities = _candidate_entities(text)
    plan.entities = entities[:MAX_ENTITIES]

    threshold_match = _THRESHOLD_NUM_RE.search(text)
    if threshold_match:
        plan.threshold_miles = int(threshold_match.group(1).replace(",", ""))

    if _LONGHAUL_RE.search(text):
        plan.intent = "metric_lookup"
        plan.requested_metrics = ["longhaul_share"]
        plan.operations = ["longhaul_share"]
        if plan.threshold_miles is None:
            plan.threshold_miles = 3000
        return plan

    if _REVENUE_RE.search(text):
        plan.intent = "metric_lookup"
        plan.requested_metrics = ["revenue_context"]
        plan.operations = ["assess_demand_pressure", "revenue_context"]
        return plan

    if _DEMAND_RE.search(text):
        plan.intent = "explain"
        plan.requested_metrics = ["demand_pressure"]
        plan.operations = ["assess_demand_pressure"]
        return plan

    wants_compare = bool(_COMPARE_RE.search(text)) or (len(entities) >= 2 and ("and" in text.lower()))
    wants_congestion = bool(_CONGESTION_RE.search(text))
    wants_abs_change = bool(_ABS_CHANGE_RE.search(text))
    wants_rank = bool(_RANK_RE.search(text)) and not wants_compare

    if wants_congestion and not wants_compare:
        # The same fixed congestion operation returns either one airport's
        # profile or a comparable two-airport result after entity resolution.
        plan.intent = "metric_lookup"
        plan.operations = ["compare_airports"]
        plan.requested_metrics = ["congestion"]
        return plan

    if wants_compare:
        plan.intent = "compare"
        plan.operations = ["compare_airports"]
        metrics = []
        if wants_congestion:
            metrics.append("congestion")
        if wants_abs_change or "growth" in text.lower() or "passenger" in text.lower():
            metrics.append("absolute_passenger_change")
        plan.requested_metrics = metrics or ["congestion"]
        if _INSTEAD_RE.search(text) and entities:
            plan.intent = "follow_up_replace"
        return plan

    if wants_abs_change:
        plan.intent = "rank"
        plan.operations = ["rank_airports"]
        plan.requested_metrics = ["absolute_passenger_change"]
        return plan

    if wants_rank:
        plan.intent = "rank"
        plan.operations = ["rank_airports"]
        plan.requested_metrics = ["opportunity_score"]
        return plan

    plan.ambiguities.append("Could not confidently match a supported question pattern; please rephrase.")
    return plan


def _actionable(plan: Plan) -> Plan:
    """A model plan that asks for nothing (no operation, metric, refusal or
    follow-up qualifier) is treated as rejected, so the keyword planner gets a
    second look instead of the turn ending in "please rephrase"."""
    if (not plan.operations and not plan.requested_metrics and plan.unsupported_metric is None
            and plan.threshold_miles is None and not plan.intent.startswith("follow_up")):
        raise PlanRejected("Model proposed no actionable plan.")
    return plan


def plan_question(text: str, context: TurnContext) -> Plan:
    """Validated plan for one turn: the configured model's proposal if usable, else fallback.

    A model failure or a rejected proposal never fails the turn: the bounded
    keyword fallback runs and, when a model was configured, the plan records
    that it was used. A fallback plan that itself fails validation becomes an
    ``unknown`` plan with no operations, which the orchestrator clarifies.
    """
    fallback_reason = None
    if llm.is_configured():
        try:
            context_summary = (f"airports={context.airport_codes} scope={context.geographic_scope} "
                               f"threshold_miles={context.threshold_miles} operations={context.operations}")
            proposal, source = llm.propose_plan(text, context_summary)
            # parse_model_proposal is the single gate: it type-checks and
            # allowlist/bounds-validates every field and rejects the WHOLE
            # proposal (never a partial plan) on any malformed or
            # over-budget field, including a non-US geographic_scope that
            # must be resolved through the registry like any entity.
            # check_grounding then rejects a schema-valid but checkably
            # wrong plan (ungrounded place, missed TSA refusal).
            return check_grounding(_actionable(parse_model_proposal(proposal, text, source)), text, context)
        except llm.LLMUnavailable as exc:
            fallback_reason = f"unavailable: {exc}"
        except PlanRejected as exc:
            fallback_reason = f"rejected: {exc}"
    plan = keyword_fallback_plan(text)
    plan.fallback_reason = fallback_reason
    if fallback_reason:
        plan.ambiguities.append(
            "Live model proposal was unavailable or rejected this turn; used the bounded keyword fallback."
        )
    try:
        return validate_plan(plan)
    except PlanRejected as exc:
        plan.intent = "unknown"
        plan.ambiguities.append(str(exc))
        plan.operations, plan.requested_metrics = [], []
        return plan

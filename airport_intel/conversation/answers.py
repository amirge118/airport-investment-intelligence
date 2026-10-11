"""The answer contract: one envelope shape for every ``result_state``.

Every chat response, from a computed ranking to a refusal, has the same
keys so the browser and tests never special-case a shape. This module owns
that envelope and the non-computing outcomes (refusal, clarification,
unsupported metric). Operation-specific content lives in
``operations``; the decision of *which* outcome applies lives in
``orchestrator``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from ..planning.planner import Plan
from .narrative import insight

if TYPE_CHECKING:
    from .context import TurnContext

RESULT_STATES = {
    "computed", "partial", "clarification_required",
    "insufficient_evidence", "unsupported_metric", "out_of_scope",
}


@dataclass
class Answer:
    """The single definition of the HTTP/UI answer contract.

    Every field except ``source_coverage`` becomes an envelope key;
    ``source_coverage`` feeds only the trace. ``finalize`` adds the two
    derived keys (``insight``, ``analysis_trace``) and returns the JSON body.
    """

    answer_kind: str
    result_state: str
    message: str
    resolved_scope: str | None = None
    metrics: list = field(default_factory=list)
    ranking_rows: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)
    caveats: list = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    follow_up_context: TurnContext | None = None
    candidates: list[dict] | None = None  # present in the body only for clarifications
    suggested_questions: list[str] | None = None  # present only when a clarification offers routed questions
    source_coverage: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.result_state not in RESULT_STATES:
            raise ValueError(f"Unknown result_state: {self.result_state!r}")

    def finalize(self, plan: Plan) -> dict:
        body = {name: value for name, value in self.__dict__.items() if name != "source_coverage"}
        # Snapshot the context now: a caller may keep mutating its TurnContext.
        body["follow_up_context"] = asdict(self.follow_up_context) if self.follow_up_context else None
        for optional in ("candidates", "suggested_questions"):
            if body[optional] is None:
                del body[optional]
        body["insight"] = insight(body)
        body["analysis_trace"] = {
            "interpreted_request": plan.intent,
            "selected_tools": plan.operations,
            "selected_metrics": plan.requested_metrics,
            "source_coverage": self.source_coverage,
            "result_state": self.result_state,
            "missing_evidence": self.missing_evidence,
            "planner_source": plan.planner_source,
            "planner_fallback_reason": plan.fallback_reason,
        }
        return body


def refusal(plan: Plan, place: str, country: str) -> dict:
    return Answer(answer_kind="refusal", result_state="out_of_scope",
                  message=f"{place} is outside the US investment scope ({country}).").finalize(plan)


def clarification(plan: Plan, question: str, candidates: list[dict] | None = None,
                  suggested_questions: list[str] | None = None) -> dict:
    return Answer(answer_kind="clarification", result_state="clarification_required",
                  message=question, candidates=candidates or [],
                  suggested_questions=suggested_questions).finalize(plan)


# Fixed wording per canonical key (see planner._canonical_unsupported_metric);
# model text is never echoed into the message.
_UNSUPPORTED_LABELS = {"security_wait_time": "'security wait time' is"}


def unsupported(plan: Plan, metric: str) -> dict:
    subject = _UNSUPPORTED_LABELS.get(metric, "That is")
    return Answer(
        answer_kind="unsupported", result_state="unsupported_metric",
        message=(f"{subject} not a metric this tool implements. It covers US "
                 f"airport traffic growth/scale screening, operational delay share, long-haul "
                 f"departure share, demand-pressure context, and indicative revenue exposure only."),
    ).finalize(plan)


def ambiguity_clarification(plan: Plan, decision: dict) -> dict:
    """Ask about an unresolved place; never guess between candidates."""
    candidates = decision.get("candidates")
    if decision.get("reason") == "low_confidence_foreign_homonym":
        options = ", ".join(f"{c['name']} ({c['code']}, {c['state']})" for c in candidates[:5])
        question = (f"'{decision['query']}' matches a US airport with no scheduled service ({options}) "
                    f"and is also a well-known place name in {decision['foreign_country']}, which is "
                    f"outside the US investment scope. Which US airport/city/state did you mean, "
                    f"if any?")
    elif candidates:
        options = ", ".join(f"{c['name']} ({c['code']}, {c['state']})" for c in candidates[:5])
        question = f"'{decision['query']}' matches more than one US airport: {options}. Which did you mean?"
    else:
        question = f"'{decision['query']}' is not a recognized US airport, city, state, or region. Could you rephrase it?"
    return clarification(plan, question, candidates)

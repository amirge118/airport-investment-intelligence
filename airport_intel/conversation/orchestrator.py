"""One chat turn: plan -> early outcomes -> resolve -> context -> facts -> answer.

``Orchestrator`` owns the *order* of decisions for a turn and the only
per-process state the pipeline keeps: the resolver catalogue and its
``Registry``, replaced atomically when the active data version changes.
Everything else is delegated to a single-purpose module:

- ``planner.plan_question``      model-or-fallback validated ``Plan``
- ``Registry.classify_entities``  US resolution, ambiguity and foreign places
- ``context.next_turn_context``   which slots a follow-up inherits
- ``operations``                  deterministic analytics -> evidence-first answer
- ``answers``                     refusal/clarification/unsupported envelopes

Every number in an answer traces back to ``analytics.calculations``, never
to model or user text.
"""

from __future__ import annotations

from ..facts import FactBundle, FactSource, InMemoryFacts
from ..planning import planner
from ..planning.planner import CITY_ABBREVIATIONS, Plan, mentions_replacement
from ..planning.resolver import STATE_NAME_TO_ABBR, Registry
from .answers import ambiguity_clarification, clarification, refusal, unsupported
from .context import InMemorySessionStore, TurnContext, merge_follow_up, next_turn_context
from .operations import answer_plan

MAX_MESSAGE_CHARS = 2000
_ABBR_TO_STATE = {abbr: name.title() for name, abbr in STATE_NAME_TO_ABBR.items()}

# "LA" is also Louisiana's postal code. In an airport-level question (compare
# two airports, long-haul share, demand, revenue) an analyst means Los Angeles;
# the reading is disclosed as an assumption and a state ranking keeps Louisiana.
AIRPORT_LEVEL_OPERATIONS = {"compare_airports", "longhaul_share", "assess_demand_pressure", "revenue_context"}


def _expand_city_abbreviations(plan: Plan) -> list[str]:
    """Rewrite airport-question entities such as "LA"; return the assumption notes."""
    if not set(plan.operations) & AIRPORT_LEVEL_OPERATIONS:
        return []
    notes, entities = [], []
    for entity in plan.entities:
        city = CITY_ABBREVIATIONS.get(entity.strip()) if entity.strip().isupper() else None
        if city:
            notes.append(f'"{entity.strip()}" read as {city}, not Louisiana; name the airport (e.g. LAX, BUR, LGB) to change it.')
        entities.append(city or entity)
    plan.entities = entities
    return notes


def _suggested_questions(resolved: dict) -> tuple[str, list[str]] | None:
    """Questions the keyword planner routes, for a place that resolved but had no intent.

    Each one leads to a supported operation; coverage is still checked when it
    runs, so an uncovered airport answers ``insufficient_evidence``, not a guess.
    """
    if len(resolved["airports"]) == 1 and not resolved["scopes"]:
        code = resolved["airports"][0]["code"]
        return code, [
            f"How did departing passenger volume at {code} change since 2023?",
            f"How delayed was {code} in 2025?",
            f"What share of {code} departures go at least 3,000 miles?",
            f"What is the unmet demand at {code} and why?",
        ]
    if len(resolved["scopes"]) == 1 and not resolved["airports"]:
        scope = resolved["scopes"][0]
        name = _ABBR_TO_STATE.get(scope.get("abbr", ""), "") or scope.get("key", "").title()
        return name, [
            f"Which airports in {name} deserve a closer look for expansion?",
            f"In {name}, where did departing passenger volume grow most since 2023?",
        ]
    return None


def _explicit_replacement(previous: list[str], resolved: dict, text: str) -> list[str] | None:
    """New comparison codes for "X instead of Y" / "replace Y with X", else None.

    Applies only to a follow-up on a two-airport comparison, when the message
    names exactly one airport already in it (the one to drop) and exactly one
    new airport. Order in the sentence does not matter, so no guess is made.
    """
    if len(previous) != 2 or not mentions_replacement(text) or resolved["scopes"]:
        return None
    codes = [airport["code"] for airport in resolved["airports"]]
    old = [code for code in codes if code in previous]
    new = [code for code in codes if code not in previous]
    if len(codes) != 2 or len(old) != 1 or len(new) != 1:
        return None
    return [new[0] if code == old[0] else code for code in previous]


class Orchestrator:
    def __init__(self, facts: FactSource | dict, sessions=None):
        """Create an orchestrator over a fact source and a session store.

        Tests and pure analytics may pass an immutable fact-bundle dict, served
        through ``InMemoryFacts``. The runtime passes ``storage.FactProvider``:
        identity is cached for resolution, then only the validated plan's
        fact family is queried from SQLite.
        """
        self.facts: FactSource = InMemoryFacts(facts) if isinstance(facts, dict) else facts
        # The identity catalogue is replaced atomically, never mutated per
        # request: the HTTP server is threaded, so per-request fact bundles
        # are passed down as locals.
        initial = self.facts.identity_catalogue()
        self._identity: tuple[dict, Registry] = (initial, Registry(initial))
        self.sessions = sessions or InMemorySessionStore()

    @property
    def identity_catalogue(self) -> dict:
        return self._identity[0]

    @property
    def registry(self) -> Registry:
        return self._identity[1]

    def _current_registry(self) -> Registry:
        """Return the registry for the current active identity catalogue."""
        current = self.facts.identity_catalogue()
        if current is not self._identity[0]:
            self._identity = (current, Registry(current))
        return self._identity[1]

    def _facts_for(self, plan: Plan, context: TurnContext) -> FactBundle:
        """Facts for exactly this request (scoped SQL reads, or the fixture)."""
        operations = (set(plan.operations) or set(context.operations)) - {"resolve_airports"}
        if not operations:
            return {"airport_registry": [], "airport_years": [], "faa_taf": {}}
        return self.facts.for_analysis(operations, set(context.airport_codes))

    def handle(self, session_id: str, text: str) -> dict:
        if not text or not text.strip():
            raise ValueError("Empty message")
        if len(text) > MAX_MESSAGE_CHARS:
            raise ValueError("Message too long (max 2000 characters)")

        registry = self._current_registry()
        context = self.sessions.get(session_id)
        plan = planner.plan_question(text, context)

        if plan.unsupported_metric:
            return unsupported(plan, plan.unsupported_metric)

        # Follow-up: a bare threshold override inherits the prior airport/operation.
        if plan.intent == "follow_up_override" and context.airport_codes:
            new_context = merge_follow_up(context, plan.threshold_miles, None)
            self.sessions.set(session_id, new_context)
            plan.operations = new_context.operations
            plan.requested_metrics = new_context.requested_metrics
            return answer_plan(plan, new_context, self._facts_for(plan, new_context), registry)

        alias_notes = _expand_city_abbreviations(plan)
        resolved = registry.classify_entities(plan.entities)

        if resolved["out_of_scope"]:
            place = resolved["out_of_scope"][0]
            return refusal(plan, place["place"], place["country"])

        swapped = _explicit_replacement(context.airport_codes, resolved, text)
        if swapped:
            # "JFK instead of SNA": keep the prior comparison and its metrics,
            # swap exactly the airport the user named (whatever the planner proposed).
            plan.intent = "follow_up_replace"
            plan.operations = list(context.operations)
            plan.requested_metrics = list(context.requested_metrics)
            plan.ambiguities = []
            new_context = next_turn_context(context, plan, new_airports=swapped, new_scope=None)
            self.sessions.set(session_id, new_context)
            return answer_plan(plan, new_context, self._facts_for(plan, new_context), registry)

        if (len(context.airport_codes) == 2 and len(resolved["airports"]) == 1
                and not resolved["scopes"] and mentions_replacement(text)):
            new_airport = resolved["airports"][0]["code"]
            existing = ", ".join(context.airport_codes)
            return clarification(
                plan,
                f"Should {new_airport} replace {context.airport_codes[0]} or {context.airport_codes[1]} "
                f"in the comparison (currently {existing})?",
            )

        if resolved["ambiguous"]:
            return ambiguity_clarification(plan, resolved["ambiguous"][0])

        if not plan.operations and not plan.ambiguities:
            plan.ambiguities.append("Could not confidently match a supported question pattern.")
        if plan.ambiguities and not plan.operations:
            guided = _suggested_questions(resolved)
            if guided:
                place, questions = guided
                return clarification(
                    plan, f"I found {place} but couldn't tell what you want to know about it. Pick one:",
                    suggested_questions=questions,
                )
            return clarification(
                plan,
                "I couldn't match that to a ranking, comparison, long-haul share, demand, or revenue "
                "question for a US airport/state/region. Could you rephrase it?",
            )

        new_context = next_turn_context(
            context, plan,
            new_airports=[a["code"] for a in resolved["airports"]],
            new_scope=resolved["scopes"][0] if resolved["scopes"] else None,
        )
        self.sessions.set(session_id, new_context)
        result = answer_plan(plan, new_context, self._facts_for(plan, new_context), registry)
        if alias_notes:
            result["assumptions"] = [*alias_notes, *result.get("assumptions", [])]
        return result

"""Typed conversation slots and how they evolve between turns.

``TurnContext`` is the only state a follow-up inherits: validated airport
codes or a geographic scope, period, threshold, operations and metrics --
never raw text. An explicit new value always overrides an inherited one, and
an ambiguous replacement (which of two prior airports to swap) is never
guessed. ``InMemorySessionStore`` serves fixtures/unit tests; the runtime
uses ``storage.SQLiteSessionStore`` (24h expiry, bounded rows) with the same
``get``/``set``/``clear`` contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..planning.planner import Plan


@dataclass
class TurnContext:
    airport_codes: list[str] = field(default_factory=list)
    geographic_scope: str | None = None
    period: int | None = None
    threshold_miles: int | None = None
    operations: list[str] = field(default_factory=list)
    requested_metrics: list[str] = field(default_factory=list)


class InMemorySessionStore:
    def __init__(self):
        self._sessions: dict[str, TurnContext] = {}
        self._lock = Lock()

    def get(self, session_id: str) -> TurnContext:
        with self._lock:
            return self._sessions.get(session_id, TurnContext())

    def set(self, session_id: str, context: TurnContext) -> None:
        with self._lock:
            self._sessions[session_id] = context

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)


def merge_follow_up(previous: TurnContext, threshold_miles: int | None,
                    new_airport_codes: list[str] | None) -> TurnContext:
    """Pure threshold/parameter override: keep the prior airport(s)."""
    return TurnContext(
        airport_codes=new_airport_codes if new_airport_codes else previous.airport_codes,
        geographic_scope=previous.geographic_scope,
        period=previous.period,
        threshold_miles=threshold_miles if threshold_miles is not None else previous.threshold_miles,
        operations=previous.operations,
        requested_metrics=previous.requested_metrics,
    )


def next_turn_context(previous: TurnContext, plan: Plan, new_airports: list[str],
                      new_scope: dict | None) -> TurnContext:
    """Context for a fully-planned turn: new explicit slots win, others inherit.

    Airport codes and geographic scope are mutually-exclusive scope anchors:
    a newly-resolved one replaces the other rather than both persisting
    -- otherwise a stale inherited
    region can silently outrank a new explicit airport, or vice versa.
    """
    if new_airports or new_scope:
        airport_codes = [] if new_scope and not new_airports else new_airports
        geographic_scope = new_scope if (new_scope and not new_airports) else None
    else:
        airport_codes, geographic_scope = previous.airport_codes, previous.geographic_scope
    return TurnContext(
        airport_codes=airport_codes,
        geographic_scope=geographic_scope,
        period=plan.period or previous.period,
        threshold_miles=plan.threshold_miles if plan.threshold_miles is not None else previous.threshold_miles,
        operations=plan.operations or previous.operations,
        requested_metrics=plan.requested_metrics or previous.requested_metrics,
    )

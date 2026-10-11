"""The fact port the question pipeline depends on.

``Orchestrator`` needs two things from storage: a resolver catalogue (airport
identity plus 2025 traffic) and a plan-scoped fact bundle. ``FactSource``
names that contract so the pipeline never imports SQLite. Two implementations
exist: ``storage.FactProvider`` (runtime, SQLite) and ``InMemoryFacts``
(an immutable fact bundle, used by fixtures and pure tests).
"""

from __future__ import annotations

from typing import Protocol, TypedDict


class FactBundle(TypedDict, total=False):
    """Plan-scoped, server-owned facts passed into deterministic analytics.

    Every list/dict contains source facts, not model output. Keys are optional
    because a validated operation loads only the families it needs.
    """

    version: str
    retrieved_at: str
    sources: dict
    airport_registry: list[dict]
    airport_years: list[dict]
    faa_taf: dict[str, dict]
    delay_rows: list[dict]
    route_segments: list[dict]
    npias_development: dict[str, dict]
    cats_revenue: dict[str, dict]
    installed_coverage: dict


class FactSource(Protocol):
    def identity_catalogue(self) -> FactBundle:
        """Airport identity rows plus 2025 traffic, for entity resolution."""

    def for_analysis(self, operations: set[str], codes: set[str] | None = None) -> FactBundle:
        """The minimum fact bundle the validated plan's operations need."""


class InMemoryFacts:
    """A whole fixture/recovery bundle served through the same fact port."""

    def __init__(self, facts: FactBundle):
        self.facts = facts

    def identity_catalogue(self) -> FactBundle:
        return self.facts

    def for_analysis(self, operations: set[str], codes: set[str] | None = None) -> FactBundle:
        return self.facts

"""Which planner model answers this turn.

``PLANNER_PROVIDER`` (``groq`` or ``gemini``) pins one provider; unset, the
first configured one in ``PREFERENCE`` is used. Nothing configured means the
keyword fallback plans every turn. The planner only talks to this module.
"""

from __future__ import annotations

import os

from . import gemini, groq
from .llm_contract import LLMUnavailable

PROVIDERS = {"groq": groq, "gemini": gemini}
PREFERENCE = ("groq", "gemini")


def active():
    pinned = os.environ.get("PLANNER_PROVIDER", "").strip().lower()
    if pinned:
        provider = PROVIDERS.get(pinned)
        return provider if provider and provider.is_configured() else None
    return next((PROVIDERS[name] for name in PREFERENCE if PROVIDERS[name].is_configured()), None)


def is_configured() -> bool:
    return active() is not None


def label() -> str | None:
    """``provider:model`` for traces and /health, or None when keyless."""
    provider = active()
    return f"{provider.NAME}:{provider.model_name()}" if provider else None


def propose_plan(text: str, context_summary: str = "") -> tuple[dict, str]:
    """The active provider's proposal and the ``provider:model`` label that produced it."""
    provider = active()
    if provider is None:
        raise LLMUnavailable("No planner model is configured.")
    return provider.propose_plan(text, context_summary), f"{provider.NAME}:{provider.model_name()}"

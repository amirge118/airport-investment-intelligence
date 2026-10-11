"""Gemini live planning path. The API key is read from the environment and
is never logged, printed, or included in any response. On any failure
(missing key, timeout, HTTP error, malformed JSON) the caller falls back to
the deterministic keyword planner; this module never fabricates a plan.
The prompt, schema and key loading are shared with every provider
(``llm_contract``).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .llm_contract import (
    REQUEST_TIMEOUT_SECONDS, SYSTEM_PROMPT, LLMUnavailable, user_message,
)

NAME = "gemini"

# Verified against https://ai.google.dev/gemini-api/docs/models and
# https://ai.google.dev/gemini-api/docs/pricing on 2026-10-11. The page
# explicitly tells new projects to use 3.5 Flash-Lite or 3.8 Flash; both
# have a free tier. gemini-2.0-flash (the prior default) and
# gemini-3.1-flash-lite-preview (a previously-suggested replacement; the GA
# gemini-3.1-flash-lite is still active) are both
# listed as shut down as of that date -- do not reintroduce either as the
# default without re-checking the models page first.
DEFAULT_MODEL = "gemini-3.8-flash"
SHUT_DOWN_MODELS = {
    "gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-3.1-flash-lite-preview",
    "gemini-3-pro-preview", "gemini-2.5-computer-use-preview-10-2025",
}
ACTIVE_MODELS = {
    "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite", "gemini-3.1-pro-preview", "gemini-3-flash-preview",
    "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro",
}


class GeminiUnavailable(LLMUnavailable):
    """Raised for any live-model failure. Message never contains the key."""


def is_configured() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def model_name() -> str:
    return os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)


def propose_plan(text: str, context_summary: str = "") -> dict:
    if not is_configured():
        raise GeminiUnavailable("No local Gemini API key is configured.")
    prompt = SYSTEM_PROMPT + "\n\n" + user_message(text, context_summary)

    # Official migration guidance (ai.google.dev/gemini-api/docs/generate-content/whats-new-gemini-3.6,
    # checked 2026-10-11): pass the key via the x-goog-api-key header, not the
    # URL query string; temperature/top_p/top_k/candidate_count are deprecated
    # for Gemini 3.6+ models and a future generation will 400 on them.
    api_key = os.environ["GEMINI_API_KEY"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name()}:generateContent"
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST",
                                      headers={"Content-Type": "application/json", "x-goog-api-key": api_key})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise GeminiUnavailable(f"Gemini HTTP error: {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise GeminiUnavailable(f"Gemini request failed: {type(exc).__name__}") from exc

    try:
        text_out = payload["candidates"][0]["content"]["parts"][0]["text"]
        proposal = json.loads(text_out)
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise GeminiUnavailable("Gemini returned a malformed plan.") from exc

    if not isinstance(proposal, dict):
        raise GeminiUnavailable("Gemini plan was not a JSON object.")
    # Deliberately no filtering here: allowlist/type/bounds validation is a
    # single gate owned by planner.parse_model_proposal, which rejects a
    # malformed or over-budget proposal as a whole rather than silently
    # dropping the offending field and running a partial plan.
    return proposal

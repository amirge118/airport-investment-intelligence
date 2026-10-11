"""Groq live planning path (OpenAI-compatible chat completions).

Uses strict ``json_schema`` structured output, so the reply is decoded
against ``PLAN_SCHEMA`` and cannot be malformed JSON or carry an unknown
operation. The API key comes from the environment (``GROQ_API_KEY``), goes
only in the Authorization header, and never appears in a log, error, or
response. Any failure raises ``GroqUnavailable`` and the caller falls back.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .llm_contract import PLAN_SCHEMA, REQUEST_TIMEOUT_SECONDS, SYSTEM_PROMPT, LLMUnavailable, user_message

NAME = "groq"
API_URL = "https://api.groq.com/openai/v1/chat/completions"
# Verified against console.groq.com/docs/structured-outputs and /docs/reasoning
# on 2026-10-11: strict json_schema is supported on openai/gpt-oss-20b,
# openai/gpt-oss-120b and qwen/qwen3.8-27b; gpt-oss takes reasoning_effort
# low|medium|high and include_reasoning to drop the reasoning field.
DEFAULT_MODEL = "openai/gpt-oss-120b"
STRICT_SCHEMA_MODELS = {"openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"}
# Groq's edge (Cloudflare) rejects urllib's default "Python-urllib" agent with
# 403 / error code 1010; an explicit agent is required.
USER_AGENT = "AirportIntelligenceResearch/1.0"


class GroqUnavailable(LLMUnavailable):
    pass


def is_configured() -> bool:
    return bool(os.environ.get("GROQ_API_KEY"))


def model_name() -> str:
    return os.environ.get("GROQ_MODEL", DEFAULT_MODEL)


def request_body(text: str, context_summary: str = "") -> dict:
    model = model_name()
    if model not in STRICT_SCHEMA_MODELS:
        raise GroqUnavailable(f"Model {model} does not support strict structured output.")
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": user_message(text, context_summary)}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "plan", "strict": True, "schema": PLAN_SCHEMA}},
    }
    if model.startswith("openai/gpt-oss"):
        # Slot extraction needs little reasoning; keep latency and tokens low.
        body.update({"reasoning_effort": "low", "include_reasoning": False})
    return body


def propose_plan(text: str, context_summary: str = "") -> dict:
    if not is_configured():
        raise GroqUnavailable("No local Groq API key is configured.")
    request = urllib.request.Request(
        API_URL, data=json.dumps(request_body(text, context_summary)).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT,
                 "Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise GroqUnavailable(f"Groq HTTP error: {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise GroqUnavailable(f"Groq request failed: {type(exc).__name__}") from exc
    try:
        proposal = json.loads(payload["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise GroqUnavailable("Groq returned a malformed plan.") from exc
    if not isinstance(proposal, dict):
        raise GroqUnavailable("Groq plan was not a JSON object.")
    return proposal

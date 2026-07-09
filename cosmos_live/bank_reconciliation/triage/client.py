"""LLM adapter — isolated so the rest of the agent is testable without a network call.

Default provider is OpenAI (or Azure OpenAI), driven by config. The completion function
is injectable, so tests/eval pass a stub and no live call is made. Live wiring must be
confirmed against your own .env — this module never reads secrets directly, only settings.
"""
from __future__ import annotations

from ..config import settings
from .prompt import SYSTEM
from .schema import TriageOutput


def _client():
    if settings.llm_provider == "azure":
        from openai import AzureOpenAI
        return AzureOpenAI(
            api_key=settings.azure_openai_key,
            azure_endpoint=settings.azure_openai_endpoint,
            api_version=settings.azure_openai_api_version,
        )
    from openai import OpenAI
    return OpenAI(api_key=settings.openai_api_key)


def default_complete(system: str, user: str) -> TriageOutput:
    """Call the configured provider with structured output. Raises on any failure;
    callers (agent.enrich_anomalies) catch and fall back to the deterministic comment."""
    resp = _client().beta.chat.completions.parse(
        model=settings.triage_model,
        temperature=0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format=TriageOutput,
    )
    parsed = resp.choices[0].message.parsed
    if parsed is None:
        raise ValueError("model returned no parsed structured output")
    return parsed


def complete_text(system: str, user: str) -> str:
    """Plain-text completion (for the run-level summary). Raises on failure; callers fall back."""
    resp = _client().chat.completions.create(
        model=settings.triage_model,
        temperature=0,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content or ""


def system_prompt() -> str:
    return SYSTEM

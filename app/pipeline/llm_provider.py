"""Provider chain construction, HTTP completion, and response parsing for LLM calls."""

from __future__ import annotations

import importlib
import json
import os
import re
from typing import Any, cast
from app.models import LLMClassificationOutput
from app.pipeline.llm_types import ProviderConfig


def build_provider_chain_from_env() -> list[ProviderConfig]:
    """Builds provider order from available API keys.

    Live provider calls are intentionally opt-in to keep local dev/tests deterministic.
    Set `LLM_ENABLE_LIVE_CALLS=true` to enable real provider chaining.
    """
    if os.getenv("LLM_ENABLE_LIVE_CALLS", "false").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return []

    chain: list[ProviderConfig] = []
    if os.getenv("GOOGLE_API_KEY"):
        chain.append(
            ProviderConfig(
                name="gemini",
                model=os.getenv("GEMINI_MODEL", "gemini/gemini-2.0-flash"),
            )
        )
    if os.getenv("CLAUDE_API_KEY"):
        chain.append(
            ProviderConfig(
                name="claude",
                model=os.getenv("CLAUDE_MODEL", "anthropic/claude-3-5-sonnet-latest"),
            )
        )
    if os.getenv("OPENAI_API_KEY"):
        chain.append(
            ProviderConfig(
                name="openai", model=os.getenv("OPENAI_MODEL", "openai/gpt-4o")
            )
        )
    return chain


def default_completion_fn(
    *, model: str, messages: list[dict[str, str]], temperature: float, timeout: float
) -> object:
    """Calls LiteLLM completion lazily to keep imports optional in tests."""
    litellm_module = importlib.import_module("litellm")
    completion = getattr(litellm_module, "completion")

    return completion(
        model=model,
        messages=messages,
        temperature=temperature,
        timeout=timeout,
    )


def parse_response_output(response: object) -> LLMClassificationOutput:
    """Parses a LiteLLM response object into validated output."""
    if isinstance(response, dict):
        content = response["choices"][0]["message"]["content"]
    else:
        content = response.choices[0].message.content  # type: ignore[attr-defined]
    payload = extract_json_payload(str(content))
    # Pydantic validation is the actual schema gate; mypy can't reason about dynamic JSON keys.
    return LLMClassificationOutput.model_validate(payload)


def extract_json_payload(content: str) -> dict[str, Any]:
    """Extracts the first valid JSON object from model output text."""
    content = content.strip()
    if content.startswith("{") and content.endswith("}"):
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise json.JSONDecodeError("Expected JSON object at top-level", content, 0)
        return cast(dict[str, Any], parsed)

    candidates = re.findall(r"\{.*?\}", content, flags=re.DOTALL)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return cast(dict[str, Any], parsed)
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("No JSON object found in model output", content, 0)


def extract_usage(response: object) -> dict[str, int]:
    """Extracts token usage counters from a LiteLLM/OpenAI-like response."""
    usage: object | None = (
        response.get("usage")
        if isinstance(response, dict)
        else getattr(response, "usage", None)
    )
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return {
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }


def extract_status_code(exc: Exception) -> int | None:
    """Extracts HTTP-like status code from provider exceptions."""
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    if isinstance(response_status, int):
        return response_status
    return None

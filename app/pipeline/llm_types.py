"""Shared types for the LLM classification pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.models import LLMClassificationOutput


@dataclass(frozen=True)
class ProviderConfig:
    """Defines one LLM provider in the fallback chain."""

    name: str
    model: str


@dataclass
class LLMClassificationResult:
    """Represents classifier success or a terminal error outcome."""

    output: LLMClassificationOutput | None
    provider_name: str | None
    error_reason: str | None
    latency_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


CompletionFn = Callable[..., object]

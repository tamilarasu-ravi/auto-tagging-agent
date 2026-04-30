from __future__ import annotations

from typing import Literal

def route_by_confidence(
    confidence: float,
    *,
    review_threshold: float,
    auto_post_threshold: float,
) -> Literal["AUTO_TAG", "REVIEW_QUEUE", "UNKNOWN"]:
    if confidence >= auto_post_threshold:
        return "AUTO_TAG"
    if confidence >= review_threshold:
        return "REVIEW_QUEUE"
    return "UNKNOWN"

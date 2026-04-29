from __future__ import annotations


def route_by_confidence(
    confidence: float,
    *,
    review_threshold: float,
    auto_post_threshold: float,
) -> str:
    if confidence >= auto_post_threshold:
        return "AUTO_TAG"
    if confidence >= review_threshold:
        return "REVIEW_QUEUE"
    return "UNKNOWN"

from app.pipeline.router import route_by_confidence


def test_route_by_confidence_uses_inclusive_thresholds() -> None:
    assert route_by_confidence(0.85, review_threshold=0.5, auto_post_threshold=0.85) == "AUTO_TAG"
    assert route_by_confidence(0.5, review_threshold=0.5, auto_post_threshold=0.85) == "REVIEW_QUEUE"
    assert route_by_confidence(0.49, review_threshold=0.5, auto_post_threshold=0.85) == "UNKNOWN"

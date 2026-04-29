from app.pipeline.preprocessor import normalize_vendor


def test_normalize_vendor_strips_punctuation_and_collapses_whitespace() -> None:
    assert normalize_vendor("  AWS Marketplace, Inc.  ") == "aws marketplace inc"


def test_normalize_vendor_empty_after_cleaning_returns_empty() -> None:
    assert normalize_vendor("!!!   ...") == ""

from app.pipeline.preprocessor import normalize_vendor, sanitize_ocr_text


def test_normalize_vendor_strips_punctuation_and_collapses_whitespace() -> None:
    assert normalize_vendor("  AWS Marketplace, Inc.  ") == "aws marketplace inc"


def test_normalize_vendor_empty_after_cleaning_returns_empty() -> None:
    assert normalize_vendor("!!!   ...") == ""


def test_sanitize_ocr_text_masks_email_and_card_last4_patterns() -> None:
    raw = "john.doe@example.com paid with card ending 1234 and ref 9876."
    sanitized = sanitize_ocr_text(raw)

    assert "john.doe@example.com" not in sanitized
    assert "[REDACTED_EMAIL]" in sanitized
    assert "1234" not in sanitized
    assert "9876" not in sanitized


def test_sanitize_ocr_text_masks_chunked_pan() -> None:
    raw = "Charge on card 4111-1111-1111-1111 for subscription."
    assert "4111" not in sanitize_ocr_text(raw)
    assert "XXXX-XXXX-XXXX-XXXX" in sanitize_ocr_text(raw)


def test_sanitize_ocr_text_none_returns_placeholder() -> None:
    assert sanitize_ocr_text(None) == "Not available"

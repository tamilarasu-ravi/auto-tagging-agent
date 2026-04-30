from __future__ import annotations

import re

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")

# MVP scope: regex-only OCR redaction before any LLM call. NER / structured DLP in production.
_PAN_CHUNKED_RE = re.compile(r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b")
_EMAIL_RE = re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b")
_CARD_LAST4_RE = re.compile(r"\b\d{4}\b")


def normalize_vendor(vendor_raw: str) -> str:
    """Lowercases vendor text, strips punctuation to spaces, and collapses whitespace."""
    normalized = _PUNCT_RE.sub(" ", vendor_raw.lower())
    normalized = _WS_RE.sub(" ", normalized).strip()
    return normalized


def sanitize_ocr_text(ocr_text: str | None) -> str:
    """Redacts common PII patterns from OCR snippets before prompt construction.

    Args:
        ocr_text: Raw OCR string from the transaction, or None if absent.

    Returns:
        Sanitized text for safe inclusion in LLM prompts, or a fixed placeholder when empty.
    """
    if not ocr_text:
        return "Not available"

    sanitized = _PAN_CHUNKED_RE.sub("XXXX-XXXX-XXXX-XXXX", ocr_text)
    sanitized = _EMAIL_RE.sub("[REDACTED_EMAIL]", sanitized)
    sanitized = _CARD_LAST4_RE.sub("[REDACTED_4DIGITS]", sanitized)
    return sanitized

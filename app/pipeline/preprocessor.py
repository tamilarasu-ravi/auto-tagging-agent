from __future__ import annotations

import re

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")

# MVP scope: regex-only OCR redaction before any LLM call. NER / structured DLP in production.
_PAN_CHUNKED_RE = re.compile(r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b")
_EMAIL_RE = re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b")
_CARD_ENDING_LAST4_RE = re.compile(
    r"\b(card\s+ending|ending|last\s*4|last4)\s*[:#-]?\s*(\d{4})\b",
    flags=re.IGNORECASE,
)
_MASKED_LAST4_RE = re.compile(r"\b(x{2,}|\*{2,})\s*(\d{4})\b", flags=re.IGNORECASE)


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
    sanitized = _CARD_ENDING_LAST4_RE.sub(r"\1 [REDACTED_4DIGITS]", sanitized)
    sanitized = _MASKED_LAST4_RE.sub("[REDACTED_4DIGITS]", sanitized)
    return sanitized


def sanitize_free_text(text: str | None) -> str | None:
    """Sanitizes free-form text for safe storage/logging.

    This is intentionally conservative and focused on high-risk patterns (emails, PANs,
    and explicit last-4 mentions) rather than fully general DLP.

    Args:
        text: Potentially sensitive free-form text, or None.

    Returns:
        Sanitized text, or None if input is None.
    """
    if text is None:
        return None
    return sanitize_ocr_text(text)

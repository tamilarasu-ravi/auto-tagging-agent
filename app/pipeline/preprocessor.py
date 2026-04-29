from __future__ import annotations

import re

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def normalize_vendor(vendor_raw: str) -> str:
    normalized = _PUNCT_RE.sub(" ", vendor_raw.lower())
    normalized = _WS_RE.sub(" ", normalized).strip()
    return normalized

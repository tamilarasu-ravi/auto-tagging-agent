from __future__ import annotations

from app.models import VendorRule


def build_rule_index(rules: list[VendorRule]) -> dict[str, VendorRule]:
    return {rule.vendor_key: rule for rule in rules}


def match_vendor_rule(rule_index: dict[str, VendorRule], vendor_key: str) -> VendorRule | None:
    return rule_index.get(vendor_key)

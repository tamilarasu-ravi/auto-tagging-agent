from app.models import VendorRule
from app.pipeline.rule_engine import build_rule_index, match_vendor_rule


def test_match_vendor_rule_uses_exact_vendor_key() -> None:
    rule = VendorRule(
        tenant_id="tenant_a",
        vendor_key="zoom us",
        coa_account_id="6100",
        created_by="import",
        created_at="2026-01-01T00:00:00Z",
        source_tx_id=None,
    )
    index = build_rule_index([rule])

    assert match_vendor_rule(index, "zoom us") == rule
    assert match_vendor_rule(index, "zoom") is None

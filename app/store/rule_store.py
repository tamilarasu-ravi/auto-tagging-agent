from __future__ import annotations

import json
from pathlib import Path

from app.models import VendorRule
from app.pipeline.rule_engine import build_rule_index, match_vendor_rule


class RuleStore:
    def __init__(self, repo_root: Path, rules_paths: dict[str, str]) -> None:
        self._rules_by_tenant: dict[str, dict[str, VendorRule]] = {}
        for tenant_id, relative_path in rules_paths.items():
            file_path = repo_root / relative_path
            payload = json.loads(file_path.read_text(encoding="utf-8"))
            rules = [VendorRule(**item) for item in payload]
            self._rules_by_tenant[tenant_id] = build_rule_index(rules)

    def match(self, tenant_id: str, vendor_key: str) -> VendorRule | None:
        tenant_rules = self._rules_by_tenant.get(tenant_id, {})
        return match_vendor_rule(tenant_rules, vendor_key)

    def list_rules(self, tenant_id: str) -> list[VendorRule]:
        return list(self._rules_by_tenant.get(tenant_id, {}).values())

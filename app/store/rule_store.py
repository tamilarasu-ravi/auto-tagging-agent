from __future__ import annotations

import json
from pathlib import Path

from app.models import VendorRule
from app.pipeline.rule_engine import build_rule_index, match_vendor_rule


class RuleStore:
    """Loads and serves deterministic vendor rules by tenant."""

    def __init__(
        self,
        repo_root: Path,
        rules_paths: dict[str, str],
        coa_ids_by_tenant: dict[str, set[str]],
    ) -> None:
        """Initializes rule indexes and validates CoA references.

        Args:
            repo_root: Repository root path.
            rules_paths: Mapping of tenant IDs to relative rules file paths.
            coa_ids_by_tenant: Valid CoA account IDs keyed by tenant.

        Raises:
            ValueError: If a rule references a CoA account not defined for the tenant.
        """
        self._rules_by_tenant: dict[str, dict[str, VendorRule]] = {}
        for tenant_id, relative_path in rules_paths.items():
            file_path = repo_root / relative_path
            payload = json.loads(file_path.read_text(encoding="utf-8"))
            rules = [VendorRule(**item) for item in payload]
            valid_coa_ids = coa_ids_by_tenant.get(tenant_id, set())
            invalid_rule = next(
                (rule for rule in rules if rule.coa_account_id not in valid_coa_ids),
                None,
            )
            if invalid_rule:
                raise ValueError(
                    "invalid coa_account_id in rule store "
                    f"tenant={tenant_id} vendor_key={invalid_rule.vendor_key} "
                    f"coa_account_id={invalid_rule.coa_account_id}"
                )
            self._rules_by_tenant[tenant_id] = build_rule_index(rules)

    def match(self, tenant_id: str, vendor_key: str) -> VendorRule | None:
        """Looks up an exact vendor-key rule for a tenant.

        Args:
            tenant_id: Tenant identifier.
            vendor_key: Normalized vendor key.

        Returns:
            A matching vendor rule or None when no match exists.
        """
        tenant_rules = self._rules_by_tenant.get(tenant_id, {})
        return match_vendor_rule(tenant_rules, vendor_key)

    def list_rules(self, tenant_id: str) -> list[VendorRule]:
        """Lists all rules for one tenant.

        Args:
            tenant_id: Tenant identifier.

        Returns:
            Deterministic vendor rules for the tenant.
        """
        return list(self._rules_by_tenant.get(tenant_id, {}).values())

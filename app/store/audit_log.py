from __future__ import annotations

from collections import defaultdict

from app.models import TaggingResult


class AuditLogStore:
    def __init__(self) -> None:
        self._items: dict[str, list[TaggingResult]] = defaultdict(list)

    def append(self, result: TaggingResult) -> None:
        self._items[result.tenant_id].append(result)

    def list_by_tenant(self, tenant_id: str) -> list[TaggingResult]:
        return self._items.get(tenant_id, [])

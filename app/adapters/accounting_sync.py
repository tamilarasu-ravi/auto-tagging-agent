from __future__ import annotations

from collections import defaultdict

from app.models import TaggingResult


class MockAccountingSyncAdapter:
    def __init__(self) -> None:
        self._synced: dict[str, list[TaggingResult]] = defaultdict(list)

    def sync(self, result: TaggingResult) -> None:
        self._synced[result.tenant_id].append(result)

    def list_by_tenant(self, tenant_id: str) -> list[TaggingResult]:
        return self._synced.get(tenant_id, [])

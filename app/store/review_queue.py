from __future__ import annotations

import threading

from app.models import TaggingResult


class ReviewQueueStore:
    """Stores review queue items in memory for Step 3."""

    def __init__(self) -> None:
        """Initializes an in-memory queue grouped by tenant."""
        self._lock = threading.RLock()
        self._items: dict[str, list[TaggingResult]] = {}

    def add(self, result: TaggingResult) -> None:
        """Adds one tagging result into the tenant review queue.

        Args:
            result: Review-queued tagging result event.
        """
        with self._lock:
            tenant_items = self._items.setdefault(result.tenant_id, [])
            tenant_items.append(result)

    def list_by_tenant(self, tenant_id: str) -> list[TaggingResult]:
        """Lists pending review items for a tenant.

        Args:
            tenant_id: Tenant identifier.

        Returns:
            Queue items for the tenant.
        """
        with self._lock:
            return list(self._items.get(tenant_id, []))

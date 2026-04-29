from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
import threading

from app.models import TaggingResult


class AuditLogStore:
    """Persists append-only audit records per tenant."""

    def __init__(self, root_dir: Path) -> None:
        """Initializes the audit store and loads existing files.

        Args:
            root_dir: Base directory for tenant audit JSONL files.
        """
        self._root_dir = root_dir
        self._root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._items: dict[str, list[TaggingResult]] = defaultdict(list)
        self._load_existing_files()

    def _load_existing_files(self) -> None:
        """Loads tenant audit events from disk into memory."""
        for file_path in self._root_dir.glob("*.jsonl"):
            tenant_id = file_path.stem
            for line in file_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                self._items[tenant_id].append(TaggingResult(**json.loads(line)))

    def _tenant_file(self, tenant_id: str) -> Path:
        """Builds a tenant-specific audit file path.

        Args:
            tenant_id: Tenant identifier.

        Returns:
            Path to tenant audit jsonl file.
        """
        return self._root_dir / f"{tenant_id}.jsonl"

    def append(self, result: TaggingResult) -> None:
        """Appends a single immutable audit event.

        Args:
            result: Tagging result event to append.
        """
        with self._lock:
            self._items[result.tenant_id].append(result)
            file_path = self._tenant_file(result.tenant_id)
            line = json.dumps(result.model_dump(mode="json"), ensure_ascii=True)
            with file_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")

    def list_by_tenant(self, tenant_id: str) -> list[TaggingResult]:
        """Returns audit events for one tenant.

        Args:
            tenant_id: Tenant identifier.

        Returns:
            A copy of stored tenant audit events.
        """
        with self._lock:
            return list(self._items.get(tenant_id, []))

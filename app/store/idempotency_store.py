from __future__ import annotations

import json
import threading
from pathlib import Path

from app.models import TaggingResult


class IdempotencyStore:
    """Persists idempotency records per tenant on local disk."""

    def __init__(self, root_dir: Path) -> None:
        """Initializes file-backed idempotency storage.

        Args:
            root_dir: Base directory where tenant cache files are stored.
        """
        self._root_dir = root_dir
        self._root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, dict[str, tuple[str, TaggingResult]]] = {}

    def _tenant_file(self, tenant_id: str) -> Path:
        """Returns the cache file path for one tenant.

        Args:
            tenant_id: Tenant identifier.

        Returns:
            Tenant-specific JSON cache path.
        """
        return self._root_dir / f"{tenant_id}.json"

    def _load_tenant(self, tenant_id: str) -> None:
        """Loads a tenant cache from disk into memory if not already loaded.

        Args:
            tenant_id: Tenant identifier.
        """
        if tenant_id in self._cache:
            return

        file_path = self._tenant_file(tenant_id)
        if not file_path.exists():
            self._cache[tenant_id] = {}
            return

        payload = json.loads(file_path.read_text(encoding="utf-8"))
        tenant_cache: dict[str, tuple[str, TaggingResult]] = {}
        for idempotency_key, value in payload.items():
            fingerprint = value["fingerprint"]
            result = TaggingResult(**value["result"])
            tenant_cache[idempotency_key] = (fingerprint, result)
        self._cache[tenant_id] = tenant_cache

    def get(self, tenant_id: str, idempotency_key: str) -> tuple[str, TaggingResult] | None:
        """Reads a previously persisted idempotency result if present.

        Args:
            tenant_id: Tenant identifier.
            idempotency_key: Key to look up.

        Returns:
            Fingerprint and cached result when present, otherwise None.
        """
        with self._lock:
            self._load_tenant(tenant_id)
            return self._cache[tenant_id].get(idempotency_key)

    def put(
        self,
        tenant_id: str,
        idempotency_key: str,
        fingerprint: str,
        result: TaggingResult,
    ) -> None:
        """Stores and persists an idempotency record for a tenant.

        Args:
            tenant_id: Tenant identifier.
            idempotency_key: Key to save.
            fingerprint: Stable request fingerprint used for conflict detection.
            result: Final tagging result to return for retries.
        """
        with self._lock:
            self._load_tenant(tenant_id)
            self._cache[tenant_id][idempotency_key] = (fingerprint, result)
            payload = {
                key: {"fingerprint": value[0], "result": value[1].model_dump(mode="json")}
                for key, value in self._cache[tenant_id].items()
            }
            self._tenant_file(tenant_id).write_text(
                json.dumps(payload, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field


class TenantConfig(BaseModel):
    tenant_id: str
    tenant_name: str
    review_threshold: float = Field(ge=0.0, le=1.0)
    auto_post_threshold: float = Field(ge=0.0, le=1.0)
    coa_path: str
    rules_path: str


class AppConfig(BaseModel):
    tenants: dict[str, TenantConfig]


def load_app_config(config_path: Path) -> AppConfig:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    tenants_list = payload.get("tenants", [])
    tenants = {item["tenant_id"]: TenantConfig(**item) for item in tenants_list}
    return AppConfig(tenants=tenants)

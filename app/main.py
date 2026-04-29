from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException

from app.config import AppConfig, load_app_config
from app.models import Transaction


APP_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = APP_ROOT / "data" / "tenants.json"

app = FastAPI(title="Reap CFO Agent", version="0.1.0")
app_config: AppConfig = load_app_config(CONFIG_PATH)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "reap-cfo-agent"}


@app.post("/transactions/tag")
def tag_transaction(transaction: Transaction) -> dict[str, str]:
    if transaction.tenant_id not in app_config.tenants:
        raise HTTPException(status_code=404, detail="Unknown tenant_id.")
    raise HTTPException(status_code=501, detail="Tagging pipeline not implemented yet.")

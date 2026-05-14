"""Phase 0: golden-case regression checks and edge-case metric baselines.

Run:
  LLM_ENABLE_LIVE_CALLS=false python3 tests/eval/golden_gate.py
  LLM_ENABLE_LIVE_CALLS=false python3 tests/eval/golden_gate.py --check-metrics
  LLM_ENABLE_LIVE_CALLS=false python3 tests/eval/golden_gate.py --write-baseline
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient


def _ensure_project_root_on_path() -> None:
    """Adds repository root to sys.path for direct script execution."""
    import sys

    project_root = Path(__file__).resolve().parents[2]
    project_root_str = str(project_root)
    if project_root_str not in sys.path:
        sys.path.insert(0, project_root_str)


_ensure_project_root_on_path()

from app.main import app, app_config  # noqa: E402
from tests.eval.eval_runner import run_eval  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLDEN_FIXTURE = REPO_ROOT / "tests" / "eval" / "fixtures" / "golden_cases.json"
DEFAULT_EDGE_FIXTURE = REPO_ROOT / "tests" / "eval" / "fixtures" / "edge_cases.json"
DEFAULT_BASELINE_METRICS = REPO_ROOT / "tests" / "eval" / "fixtures" / "golden_baseline_metrics.json"


@dataclass(frozen=True)
class GoldenCaseFailure:
    """One golden case that did not match expectations."""

    case_id: str
    tenant_id: str
    vendor_raw: str
    expected_status: str
    expected_coa_account_id: str | None
    actual_status: str
    actual_coa_account_id: str | None


def _json_safe_float(value: float) -> float | None:
    """Converts non-finite floats to None for JSON compatibility."""
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _metrics_for_json(metrics: dict[str, float | int]) -> dict[str, Any]:
    """Normalizes eval metrics dict for JSON serialization."""
    out: dict[str, Any] = {}
    for key, val in metrics.items():
        if isinstance(val, float):
            out[key] = _json_safe_float(val)
        else:
            out[key] = val
    return out


def run_golden_cases(fixture_path: Path | None = None) -> dict[str, Any]:
    """Runs each golden fixture row against POST /transactions/tag and checks expectations.

    Args:
        fixture_path: Path to golden_cases.json; defaults to tests/eval/fixtures/golden_cases.json.

    Returns:
        Dict with total, passed, failed counts and a list of failure dicts (if any).
    """
    path = fixture_path or DEFAULT_GOLDEN_FIXTURE
    raw = json.loads(path.read_text(encoding="utf-8"))
    failures: list[GoldenCaseFailure] = []
    run_id = uuid4().hex[:8]

    with TestClient(app) as client:
        for index, item in enumerate(raw):
            tenant_id = item["tenant_id"]
            tenant_cfg = app_config.tenants[tenant_id]
            headers = {"X-API-Key": tenant_cfg.api_key}
            payload = {
                "tx_id": f"{item['tx_id']}_{run_id}_{index}",
                "tenant_id": tenant_id,
                "vendor_raw": item["vendor_raw"],
                "amount": str(item["amount"]),
                "currency": item["currency"],
                "date": "2026-04-30",
                "transaction_type": "card",
                "ocr_text": None,
                "idempotency_key": f"golden_{run_id}_{index}",
            }
            response = client.post("/transactions/tag", json=payload, headers=headers)
            result = response.json()
            actual_status = result["status"]
            actual_coa = result.get("coa_account_id")
            expected_status = item["expected_status"]
            expected_coa = item.get("expected_coa_account_id")

            status_ok = actual_status == expected_status
            if expected_coa is None:
                coa_ok = actual_coa is None
            else:
                coa_ok = actual_coa == expected_coa

            if not (status_ok and coa_ok):
                failures.append(
                    GoldenCaseFailure(
                        case_id=item.get("case_id", item["tx_id"]),
                        tenant_id=tenant_id,
                        vendor_raw=item["vendor_raw"],
                        expected_status=expected_status,
                        expected_coa_account_id=expected_coa,
                        actual_status=actual_status,
                        actual_coa_account_id=actual_coa,
                    )
                )

    total = len(raw)
    failed = len(failures)
    passed = total - failed
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "failures": [f.__dict__ for f in failures],
    }


def write_baseline_metrics(output_path: Path | None = None) -> Path:
    """Writes edge-case eval metrics for tenant_a and tenant_b to a JSON baseline file.

    Args:
        output_path: Destination path; defaults to tests/eval/fixtures/golden_baseline_metrics.json.

    Returns:
        Path written.
    """
    from datetime import datetime, timezone

    out = output_path or DEFAULT_BASELINE_METRICS
    edge = DEFAULT_EDGE_FIXTURE
    tenants: dict[str, Any] = {}
    for tenant_id in ("tenant_a", "tenant_b"):
        metrics = run_eval(tenant_id, edge)
        tenants[tenant_id] = _metrics_for_json(metrics)

    payload = {
        "schema_version": 1,
        "documented_at": datetime.now(timezone.utc).isoformat(),
        "fixture": str(edge.relative_to(REPO_ROOT)),
        "note": "Captured with LLM_ENABLE_LIVE_CALLS=false (deterministic classifier). Re-run --write-baseline after intentional pipeline changes.",
        "tenants": tenants,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return out


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        x = float(value)
        if math.isnan(x):
            return None
        return x
    return None


def check_metrics_against_baseline(
    baseline_path: Path | None = None,
    *,
    edge_fixture: Path | None = None,
    precision_slack_pp: float = 1.0,
    long_tail_slack_pp: float = 1.0,
    brier_slack: float = 0.03,
) -> list[str]:
    """Compares current edge-case metrics to committed baseline; returns human-readable violations.

    Args:
        baseline_path: Path to golden_baseline_metrics.json.
        edge_fixture: Path to edge_cases.json.
        precision_slack_pp: Allowed drop in auto_tag_precision (percentage points).
        long_tail_slack_pp: Allowed drop in long_tail_unknown_rate (percentage points).
        brier_slack: Allowed increase in Brier score (absolute).

    Returns:
        Empty list if all checks pass; otherwise list of violation messages.
    """
    base_path = baseline_path or DEFAULT_BASELINE_METRICS
    edge = edge_fixture or DEFAULT_EDGE_FIXTURE
    baseline = json.loads(base_path.read_text(encoding="utf-8"))
    violations: list[str] = []

    for tenant_id in ("tenant_a", "tenant_b"):
        expected_block = baseline["tenants"][tenant_id]
        current = run_eval(tenant_id, edge)

        base_precision = _as_float(expected_block.get("auto_tag_precision"))
        cur_precision = _as_float(float(current["auto_tag_precision"]))  # type: ignore[arg-type]

        if base_precision is not None and cur_precision is not None:
            if cur_precision < base_precision - precision_slack_pp:
                violations.append(
                    f"{tenant_id}: auto_tag_precision {cur_precision:.2f}% dropped below "
                    f"baseline {base_precision:.2f}% (slack {precision_slack_pp} pp)."
                )

        base_lt = _as_float(expected_block.get("long_tail_unknown_rate"))
        cur_lt = _as_float(float(current["long_tail_unknown_rate"]))  # type: ignore[arg-type]
        if base_lt is not None and cur_lt is not None:
            if cur_lt < base_lt - long_tail_slack_pp:
                violations.append(
                    f"{tenant_id}: long_tail_unknown_rate {cur_lt:.2f}% dropped below "
                    f"baseline {base_lt:.2f}% (slack {long_tail_slack_pp} pp)."
                )

        base_brier = _as_float(expected_block.get("brier_score"))
        cur_brier = _as_float(float(current["brier_score"]))  # type: ignore[arg-type]
        if base_brier is not None and cur_brier is not None:
            if cur_brier > base_brier + brier_slack:
                violations.append(
                    f"{tenant_id}: brier_score {cur_brier:.3f} above baseline {base_brier:.3f} "
                    f"(slack +{brier_slack})."
                )

    return violations


def main() -> None:
    parser = argparse.ArgumentParser(description="Golden-case and baseline metric gates (Phase 0).")
    parser.add_argument(
        "--fixture",
        type=Path,
        default=None,
        help="Path to golden_cases.json (default: tests/eval/fixtures/golden_cases.json)",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Write golden_baseline_metrics.json from edge_cases eval (tenant_a + tenant_b).",
    )
    parser.add_argument(
        "--check-metrics",
        action="store_true",
        help="Compare edge_cases metrics to golden_baseline_metrics.json; exit non-zero on regression.",
    )
    args = parser.parse_args()

    if args.write_baseline:
        path = write_baseline_metrics()
        print(f"Wrote baseline metrics to {path}")
        return

    if args.check_metrics:
        violations = check_metrics_against_baseline()
        if violations:
            print("Metric gate FAILED:")
            for v in violations:
                print(f"  - {v}")
            raise SystemExit(1)
        print("Metric gate OK: edge_cases metrics within baseline slack.")
        return

    result = run_golden_cases(args.fixture)
    print(f"Golden cases: {result['passed']}/{result['total']} passed")
    if result["failures"]:
        for f in result["failures"]:
            print(
                f"  FAIL {f['case_id']}: expected {f['expected_status']} coa={f['expected_coa_account_id']} "
                f"got {f['actual_status']} coa={f['actual_coa_account_id']} "
                f"(tenant={f['tenant_id']} vendor={f['vendor_raw']!r})"
            )
        raise SystemExit(1)
    print("All golden cases match.")


if __name__ == "__main__":
    main()

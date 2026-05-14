"""Phase 0: golden-case regression tests."""

from pathlib import Path

from tests.eval.golden_gate import check_metrics_against_baseline, run_golden_cases


def test_golden_cases_all_match() -> None:
    """Every row in golden_cases.json must match status and CoA expectations."""
    fixture = Path(__file__).resolve().parents[1] / "tests" / "eval" / "fixtures" / "golden_cases.json"
    result = run_golden_cases(fixture)
    assert result["failed"] == 0, result["failures"]


def test_edge_case_metrics_within_baseline() -> None:
    """edge_cases.json aggregate metrics must not regress vs committed baseline."""
    violations = check_metrics_against_baseline()
    assert not violations, violations

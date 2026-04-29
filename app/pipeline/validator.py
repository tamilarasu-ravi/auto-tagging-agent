from __future__ import annotations

from app.models import LLMClassificationOutput


def validate_classification_output(
    output: LLMClassificationOutput,
    valid_coa_account_ids: set[str],
) -> bool:
    return output.coa_account_id in valid_coa_account_ids

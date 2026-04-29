from app.models import LLMClassificationOutput
from app.pipeline.validator import validate_classification_output


def test_validate_classification_output_rejects_account_outside_coa() -> None:
    output = LLMClassificationOutput(
        coa_account_id="9999",
        confidence=0.7,
        reasoning="Guess",
    )

    assert validate_classification_output(output, {"6100", "6200"}) is False


def test_validate_classification_output_accepts_valid_output() -> None:
    output = LLMClassificationOutput(
        coa_account_id="6100",
        confidence=0.9,
        reasoning="Known vendor",
    )

    assert validate_classification_output(output, {"6100", "6200"}) is True

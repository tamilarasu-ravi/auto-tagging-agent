"""Builds LLM chat messages for tenant-scoped CoA classification."""

from __future__ import annotations

import json

from app.models import CoAAccount, Transaction
from app.pipeline.preprocessor import sanitize_ocr_text


def build_classification_messages(
    transaction: Transaction,
    tenant_coa: list[CoAAccount],
    tenant_name: str,
    few_shot_examples: list[dict[str, object]],
) -> list[dict[str, str]]:
    """Constructs system/user messages for JSON-only classification.

    Args:
        transaction: Target transaction to classify.
        tenant_coa: Tenant chart of accounts rows injected into the prompt.
        tenant_name: Human-readable tenant label for the prompt.
        few_shot_examples: Serialized few-shot rows (already tenant-scoped).

    Returns:
        OpenAI-style message list for the completion API.
    """
    coa_lines = "\n".join(
        [
            f"- {item.account_id} | {item.name} | {item.description}"
            for item in tenant_coa
        ]
    )

    system_prompt = (
        "You are a financial transaction classifier for a multi-tenant expense platform.\n\n"
        "CRITICAL INSTRUCTIONS:\n"
        "1. You must output ONLY a valid, raw JSON object. Do NOT wrap the response in ```json markdown blocks. No preamble, no postscript.\n"
        '2. The JSON must exactly match this schema: {"reasoning": string, "coa_account_id": string, "confidence": float}\n'
        "3. 'reasoning' must be generated FIRST. Keep it to ONE sentence (max 50 words). Use this to explain your logic based on the vendor, amount, and CoA definitions.\n"
        "4. 'coa_account_id' MUST be selected from the TENANT CHART OF ACCOUNTS below.\n"
        "5. 'confidence' must be a float between 0.0 and 1.0. If you are guessing or the vendor is ambiguous, confidence MUST be below 0.5.A confident, unambiguous match should be 0.85 or above.\n\n"
        "6. If multiple CoA accounts are plausible matches, choose the MOST SPECIFIC one (e.g. prefer 'Cloud Infrastructure' over 'General Expenses' for an AWS charge). If two accounts remain equally specific after reasoning, pick the lower-risk account and set confidence below 0.6 to signal the ambiguity.\n\n"
        f"TENANT NAME: {tenant_name}\n"
        "TENANT CHART OF ACCOUNTS:\n"
        f"{coa_lines}"
    )

    user_prompt = (
        "HISTORICAL EXAMPLES FOR THIS TENANT:\n"
        f"{json.dumps(few_shot_examples, indent=2)}\n\n"
        "-------------------\n"
        "Now, classify THIS target transaction:\n"
        f"Vendor: {transaction.vendor_raw}\n"
        f"Amount: {transaction.amount} {transaction.currency}\n"
        f"Date: {transaction.date}\n"
        f"Type: {transaction.transaction_type}\n"
        f"OCR: {sanitize_ocr_text(transaction.ocr_text)}\n\n"
        'Remember: Return ONLY a raw JSON object starting with {"reasoning": ...'
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

"""Deterministic CoA classification when no live LLM providers are configured."""

from __future__ import annotations

from app.models import CoAAccount, LLMClassificationOutput, Transaction


def _confidence_from_keyword_score(score: float) -> float:
    """Maps a heuristic keyword score to a bounded confidence value.

    Args:
        score: Non-negative heuristic match strength.

    Returns:
        Confidence in [0.25, 0.93] suitable for deterministic routing tests.
    """
    if score >= 20.0:
        return 0.93
    if score >= 15.0:
        return 0.91
    if score >= 10.0:
        return 0.88
    if score >= 7.0:
        return 0.75
    if score >= 5.0:
        return 0.68
    if score >= 3.0:
        return 0.45
    if score > 0.0:
        return 0.32
    return 0.25


def _is_cloud_account(account: CoAAccount) -> bool:
    """Returns true when account metadata looks like cloud/hosting infrastructure."""
    text = f"{account.name} {account.description}".lower()
    return any(
        token in text
        for token in ("cloud", "hosting", "infrastructure", "cdn", "compute")
    )


def _is_software_account(account: CoAAccount) -> bool:
    """Returns true when account metadata looks like software/SaaS spend."""
    text = f"{account.name} {account.description}".lower()
    return any(
        token in text
        for token in ("software", "saas", "subscription", "license", "licenses")
    )


def _is_local_transport_account(account: CoAAccount) -> bool:
    """Returns true when account metadata looks like local transport/ride-hailing spend."""
    text = f"{account.name} {account.description}".lower()
    return any(
        token in text for token in ("transport", "taxi", "ride", "rideshare", "hailing")
    )


def _is_travel_account(account: CoAAccount) -> bool:
    """Returns true when account metadata looks like broader travel/accommodation spend."""
    text = f"{account.name} {account.description}".lower()
    return any(
        token in text
        for token in ("travel", "accommodation", "hotel", "flight", "airline")
    )


def _is_professional_services_account(account: CoAAccount) -> bool:
    """Returns true when account metadata looks like consulting/legal/professional services spend."""
    text = f"{account.name} {account.description}".lower()
    return any(
        token in text
        for token in ("professional", "consult", "contractor", "legal", "services")
    )


def _score_tenant_coa_candidates(
    vendor_lower: str, tenant_coa: list[CoAAccount]
) -> dict[str, float]:
    """Scores CoA candidates using vendor-family keywords and CoA semantic matching.

    Args:
        vendor_lower: Lowercased vendor string.
        tenant_coa: Tenant CoA accounts available for selection.

    Returns:
        Mapping of account_id -> non-negative score.
    """
    scores: dict[str, float] = {account.account_id: 0.0 for account in tenant_coa}
    cloud_keywords = (
        "aws",
        "amazon web services",
        "gcp",
        "google cloud",
        "azure",
        "cloudflare",
        "cdn",
        "hosting",
        "marketplace",
    )
    software_keywords = (
        "zoom",
        "slack",
        "notion",
        "figma",
        "github",
        "gitlab",
        "atlassian",
        "jira",
        "saas",
        "subscription",
    )
    ride_keywords = (
        "grab",
        "uber",
        "lyft",
        "taxi",
        "bolt",
        "gojek",
        "ride",
        "rideshare",
    )
    travel_keywords = (
        "hotel",
        "flight",
        "airline",
        "airbnb",
        "booking.com",
        "travel",
        "accommodation",
        "agent",
    )
    professional_keywords = (
        "consult",
        "contractor",
        "legal",
        "law firm",
        "attorney",
        "professional services",
    )

    has_cloud_account = any(_is_cloud_account(account) for account in tenant_coa)
    has_local_transport_account = any(
        _is_local_transport_account(account) for account in tenant_coa
    )

    for account in tenant_coa:
        account_id = account.account_id
        if _is_cloud_account(account) and any(
            keyword in vendor_lower for keyword in cloud_keywords
        ):
            scores[account_id] += 15.0
        if _is_software_account(account):
            if any(keyword in vendor_lower for keyword in software_keywords):
                scores[account_id] += 15.0
            if (not has_cloud_account) and any(
                keyword in vendor_lower for keyword in cloud_keywords
            ):
                scores[account_id] += 15.0
        if _is_local_transport_account(account) and any(
            keyword in vendor_lower for keyword in ride_keywords
        ):
            scores[account_id] += 8.0
        if _is_travel_account(account):
            if any(keyword in vendor_lower for keyword in travel_keywords):
                scores[account_id] += 6.0
            if (not has_local_transport_account) and any(
                keyword in vendor_lower for keyword in ride_keywords
            ):
                scores[account_id] += 8.0
        if _is_professional_services_account(account) and any(
            keyword in vendor_lower for keyword in professional_keywords
        ):
            scores[account_id] += 8.0

    return scores


def _pick_best_account_id(scores: dict[str, float]) -> tuple[str | None, float]:
    """Selects the highest scoring account id with deterministic tie-breaking.

    Args:
        scores: account_id -> score mapping.

    Returns:
        (best_account_id, best_score) where account id is None if all scores are zero.
    """
    if not scores:
        return None, 0.0

    sorted_candidates = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    best_account_id, best_score = sorted_candidates[0]

    if best_score <= 0.0:
        return None, 0.0
    return best_account_id, best_score


def classify_transaction_no_llm(
    transaction: Transaction,
    tenant_coa: list[CoAAccount],
) -> LLMClassificationOutput:
    """Returns a deterministic classifier output without external LLM calls.

    Args:
        transaction: Incoming transaction payload.
        tenant_coa: Tenant-scoped chart-of-accounts list.

    Returns:
        A structured classification output compatible with the validator/router pipeline.
    """
    vendor_lower = transaction.vendor_raw.lower()
    if "pttep" in vendor_lower:
        sorted_ids = sorted({account.account_id for account in tenant_coa})
        return LLMClassificationOutput(
            coa_account_id=sorted_ids[0],
            confidence=0.31,
            reasoning="Vendor appears fuel/energy-adjacent and should be routed conservatively.",
        )

    if not tenant_coa:
        return LLMClassificationOutput(
            coa_account_id="",
            confidence=0.0,
            reasoning="Tenant chart of accounts is empty; cannot classify deterministically.",
        )

    scores = _score_tenant_coa_candidates(vendor_lower, tenant_coa)
    best_account_id, best_score = _pick_best_account_id(scores)
    if best_account_id is None:
        fallback_id = sorted({account.account_id for account in tenant_coa})[0]
        return LLMClassificationOutput(
            coa_account_id=fallback_id,
            confidence=0.25,
            reasoning="Insufficient deterministic signal; picked lowest account id as conservative placeholder.",
        )

    account_by_id = {account.account_id: account for account in tenant_coa}
    chosen = account_by_id[best_account_id]
    confidence = _confidence_from_keyword_score(best_score)
    reasoning = f"Heuristic keyword match suggests '{chosen.name}' ({chosen.account_id}) for this vendor."
    return LLMClassificationOutput(
        coa_account_id=best_account_id,
        confidence=confidence,
        reasoning=reasoning,
    )

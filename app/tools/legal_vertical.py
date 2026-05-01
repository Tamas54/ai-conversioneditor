"""
Legal vertical — magyar jogi review-tools (PLACEHOLDER SKELETON).

Tier-S #4 from the priority list. The actual logic is TODO; these stubs
exist so the API surface is reserved and the agent can call them, but they
return a placeholder response indicating the feature is not yet implemented.

Planned features:
  * legal_contract_extract_clauses(file_id) — break a contract into atomic
    clauses by heading hierarchy + cross-reference numbering. Output: clause
    map for downstream comparison.
  * legal_compare_to_template(file_id, template_id) — diff a draft contract
    against a known-good template, flag deviations (missing clauses, added
    clauses, materially changed clauses). Output: structured deviation list
    with severity scoring.
  * legal_gdpr_review(file_id) — scan a contract / privacy policy for GDPR
    compliance: lawful basis, data subject rights, retention periods,
    international transfer safeguards, DPO contact, breach notification.
  * legal_aiact_review(file_id) — scan a doc / system spec against EU AI Act
    risk categories: prohibited practices, high-risk obligations, GPAI duties,
    transparency requirements.
  * legal_term_consistency(file_id, glossary_file_id?) — flag inconsistent
    use of defined terms within a single document (e.g. "Megrendelő" vs.
    "Vevő" used interchangeably).

Implementation plan: each function will use Kimi K2.6 with a tightly
scoped system prompt + JSON schema response. NOT MinerU/Docling/LlamaParse
pipelines — Kimi K2.6 is smarter than those OSS tools per user assessment.
"""
from __future__ import annotations

from typing import Any, Optional


_PLACEHOLDER_RESPONSE = {
    "status": "not_implemented",
    "note": (
        "Legal vertical tools are placeholder skeletons. The agent can "
        "call them but they return this stub response. Implementation "
        "comes after the core multimodal/parser/redraw/podcast features."
    ),
}


async def legal_contract_extract_clauses(file_id: str) -> dict[str, Any]:
    """PLACEHOLDER. Break a contract into atomic clauses with hierarchy."""
    return {**_PLACEHOLDER_RESPONSE, "tool": "legal_contract_extract_clauses",
            "file_id": file_id}


async def legal_compare_to_template(
    file_id: str, template_id: str,
) -> dict[str, Any]:
    """PLACEHOLDER. Diff a draft against a known-good template, flag deviations."""
    return {**_PLACEHOLDER_RESPONSE, "tool": "legal_compare_to_template",
            "file_id": file_id, "template_id": template_id}


async def legal_gdpr_review(file_id: str) -> dict[str, Any]:
    """PLACEHOLDER. Scan a doc for GDPR compliance gaps."""
    return {**_PLACEHOLDER_RESPONSE, "tool": "legal_gdpr_review",
            "file_id": file_id}


async def legal_aiact_review(file_id: str) -> dict[str, Any]:
    """PLACEHOLDER. Scan a doc/spec against EU AI Act risk categories."""
    return {**_PLACEHOLDER_RESPONSE, "tool": "legal_aiact_review",
            "file_id": file_id}


async def legal_term_consistency(
    file_id: str, glossary_file_id: Optional[str] = None,
) -> dict[str, Any]:
    """PLACEHOLDER. Flag inconsistent defined-term usage in a document."""
    return {**_PLACEHOLDER_RESPONSE, "tool": "legal_term_consistency",
            "file_id": file_id, "glossary_file_id": glossary_file_id}

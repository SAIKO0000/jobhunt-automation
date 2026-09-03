from __future__ import annotations

from datetime import UTC, datetime

from jobhunt.models import Lead, ReviewStatus


class LeadPolicyError(ValueError):
    """Raised when lead data does not satisfy the v1 privacy workflow."""


APPROVED_LAWFUL_BASIS = {
    "business data only",
    "consent documented",
    "legitimate interest documented",
}


def assess_lead(lead: Lead) -> Lead:
    if lead.opted_out or lead.suppressed_at:
        return lead.model_copy(
            update={
                "ai_relevance_score": 0,
                "ai_rationale": "Lead is suppressed; no further drafting is permitted.",
                "ai_confidence": 1.0,
                "draft": "",
                "draft_fact_ids": [],
                "updated_at": datetime.now(UTC),
            }
        )
    if (
        lead.contains_personal_data
        and lead.lawful_basis_status.casefold() not in APPROVED_LAWFUL_BASIS
    ):
        raise LeadPolicyError(
            "Personal contact data requires a documented consent or legitimate-interest status"
        )

    score = 10  # A user-seeded official domain is required by the Lead model.
    reasons: list[str] = ["User supplied an official business domain"]
    if lead.verified_at:
        score += 20
        reasons.append("Business identity was manually verified")
    if lead.observed_problem:
        score += 25
        reasons.append("An observed operational problem is recorded")
    if lead.evidence_url:
        score += 20
        reasons.append("The problem has a supporting public evidence URL")
    if lead.service_hypothesis:
        score += 25
        reasons.append("A scoped service hypothesis is recorded")
    score = min(score, 100)

    draft = ""
    fact_ids: list[str] = []
    if score >= 65 and lead.observed_problem and lead.service_hypothesis and lead.evidence_url:
        fact_ids = [
            f"lead:{lead.lead_id}:business",
            f"lead:{lead.lead_id}:problem",
            f"lead:{lead.lead_id}:service",
        ]
        draft = (
            f"Hello {lead.business_name} team - I noticed {lead.observed_problem}. "
            f"A possible way to help is {lead.service_hypothesis}. "
            "If this is relevant, I would be happy to discuss it. This draft must be reviewed "
            "and sent manually."
        )
    return lead.model_copy(
        update={
            "ai_relevance_score": score,
            "ai_rationale": "; ".join(reasons),
            "ai_confidence": 1.0,
            "ai_model": "deterministic-v1",
            "prompt_version": "lead-rules-v1",
            "analyzed_at": datetime.now(UTC),
            "draft": draft,
            "draft_fact_ids": fact_ids,
            "review_status": ReviewStatus.NEEDS_REVIEW if draft else lead.review_status,
            "updated_at": datetime.now(UTC),
        }
    )

from __future__ import annotations

import re
from collections.abc import Iterable

from jobhunt.canonical import normalized_token
from jobhunt.models import (
    EligibilityDecision,
    FitBand,
    Opportunity,
    OpportunityType,
    ProfileClaim,
    ScoreBreakdown,
    SearchPreferences,
)


def score_opportunity(
    opportunity: Opportunity,
    preferences: SearchPreferences,
    verified_claims: Iterable[ProfileClaim],
) -> Opportunity:
    record = opportunity.source_record
    claims = list(verified_claims)
    listing_text = " ".join(
        (
            record.title,
            record.description_excerpt,
            " ".join(record.requirements),
            " ".join(record.tags),
        )
    )
    skills = set(preferences.skills)
    listing_skill_hits = {skill for skill in skills if _contains_term(listing_text, skill)}
    # Categories/tags describe a listing; they are not reliable evidence that
    # every tag is a mandatory requirement. Only an adapter's explicit
    # requirement field may drive the proportional required-skill score.
    required_terms = {
        normalized_token(item) for item in record.requirements if normalized_token(item)
    }
    if required_terms:
        matched_required = sum(
            1
            for requirement in required_terms
            if any(_contains_term(requirement, skill) for skill in skills)
        )
        required_score = round(35 * matched_required / len(required_terms))
    else:
        required_score = min(35, len(listing_skill_hits) * 5)

    matched_claims = [
        claim
        for claim in claims
        if any(_contains_term(listing_text, skill) for skill in claim.skills)
    ]
    evidence_score = min(25, len(matched_claims) * 8)
    title_hits = sum(
        _contains_term(record.title, term) for term in preferences.preferred_title_terms
    )
    niche_hits = sum(_contains_term(listing_text, term) for term in preferences.niche_terms)
    scope_score = min(15, title_hits * 5 + niche_hits * 2)
    location_score = {
        EligibilityDecision.ELIGIBLE: 15,
        EligibilityDecision.MANUAL_REVIEW: 5,
        EligibilityDecision.INELIGIBLE: 0,
    }[opportunity.eligibility_decision]
    engagement = normalized_token(f"{record.employment_type} {record.engagement_type}")
    engagement_score = (
        5
        if any(_contains_term(engagement, term) for term in preferences.preferred_engagement_terms)
        else 2
        if not engagement
        else 0
    )
    completeness_fields = (
        record.company,
        record.title,
        record.location_text,
        record.description_excerpt,
        str(record.apply_url or ""),
    )
    completeness_score = sum(bool(value) for value in completeness_fields)
    breakdown = ScoreBreakdown(
        required_skill_match=required_score,
        evidence_relevance=evidence_score,
        scope_seniority=scope_score,
        location_eligibility=location_score,
        engagement_alignment=engagement_score,
        listing_completeness=completeness_score,
    )
    total = breakdown.total
    if opportunity.eligibility_decision is EligibilityDecision.INELIGIBLE:
        band = FitBand.SKIP
    elif total >= 80:
        band = FitBand.STRONG
    elif total >= 65:
        band = FitBand.REVIEW
    # Keep plausible early-career work visible while portfolio claims are still
    # deliberately unverified. These floors only create Low Priority review
    # rows; they never override a location or qualification hard failure.
    elif total >= 30 or (record.opportunity_type is OpportunityType.VA_FREELANCE and total >= 25):
        band = FitBand.LOW
    else:
        band = FitBand.SKIP
    return opportunity.model_copy(
        update={
            "score_breakdown": breakdown,
            "rule_score": total,
            "final_score": total,
            "fit_band": band,
            "matched_evidence_ids": [claim.claim_id for claim in matched_claims],
        }
    )


def _contains_term(text: str, term: str) -> bool:
    """Match skill/title phrases without Java→JavaScript or C#→customer collisions."""
    raw_text = text.casefold()
    raw_term = term.casefold().strip()
    if raw_term == "c#":
        return re.search(r"(?<!\w)c\s*#(?!\w)", raw_text) is not None
    if raw_term == ".net":
        return re.search(r"(?<!\w)(?:\.net|dotnet)(?!\w)", raw_text) is not None
    words = re.findall(r"[a-z0-9]+", raw_term)
    if not words:
        return False
    separator = r"[\s._+#/\-]*"
    pattern = r"(?<![a-z0-9])" + separator.join(map(re.escape, words)) + r"(?![a-z0-9])"
    return re.search(pattern, raw_text) is not None

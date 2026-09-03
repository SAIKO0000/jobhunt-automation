from __future__ import annotations

from conftest import make_record

from jobhunt.canonical import canonicalize_url
from jobhunt.config import load_search_preferences
from jobhunt.dedupe import build_opportunities
from jobhunt.eligibility import evaluate_eligibility
from jobhunt.models import (
    EligibilityDecision,
    FitBand,
    LocationMode,
    LocationPolicy,
    OpportunityType,
    SourceKind,
    WorkArrangement,
)
from jobhunt.scoring import score_opportunity


def test_canonical_url_removes_tracking_and_normalizes() -> None:
    assert canonicalize_url("https://EXAMPLE.com/jobs/1/?utm_source=x&b=2&a=1#top") == (
        "https://example.com/jobs/1?a=1&b=2"
    )


def test_exact_and_cross_source_duplicates_are_safe() -> None:
    first = make_record(source_record_id="1")
    exact = make_record(
        source_record_id="2", source_url="https://example.com/jobs/1?utm_source=other"
    )
    exact.apply_url = first.apply_url
    deduped = build_opportunities([first, exact])
    assert len(deduped) == 1

    second_source = make_record(
        source=SourceKind.WWR,
        source_record_id="wwr-1",
        source_url="https://weworkremotely.com/remote-jobs/example-role",
        apply_url="https://weworkremotely.com/remote-jobs/example-role",
    )
    linked = build_opportunities([first, second_source])
    assert len(linked) == 2
    assert linked[0].duplicate_group_id == linked[1].duplicate_group_id


def test_location_policy_boundaries() -> None:
    policy = LocationPolicy(
        mode=LocationMode.COMMUTE_BANDS,
        under_one_hour_areas=["Makati"],
        one_to_two_hour_areas=["Quezon City"],
    )
    remote_overseas = evaluate_eligibility(
        make_record(country="United States", work_arrangement=WorkArrangement.REMOTE), policy
    )
    assert remote_overseas.eligibility_decision is EligibilityDecision.ELIGIBLE

    onsite_overseas = evaluate_eligibility(
        make_record(country="United States", work_arrangement=WorkArrangement.ONSITE), policy
    )
    assert onsite_overseas.eligibility_decision is EligibilityDecision.INELIGIBLE

    onsite_near = evaluate_eligibility(
        make_record(location_text="Makati, Philippines", work_arrangement=WorkArrangement.ONSITE),
        policy,
    )
    assert onsite_near.eligibility_decision is EligibilityDecision.ELIGIBLE

    onsite_mid = evaluate_eligibility(
        make_record(
            location_text="Quezon City, Philippines", work_arrangement=WorkArrangement.ONSITE
        ),
        policy,
    )
    assert onsite_mid.eligibility_decision is EligibilityDecision.INELIGIBLE

    unknown = evaluate_eligibility(make_record(work_arrangement=WorkArrangement.UNKNOWN), policy)
    assert unknown.eligibility_decision is EligibilityDecision.MANUAL_REVIEW


def test_remote_first_ignores_commute_lists_and_reviews_non_remote() -> None:
    policy = LocationPolicy(
        mode=LocationMode.REMOTE_FIRST,
        under_one_hour_areas=["Makati"],
        one_to_two_hour_areas=["Quezon City"],
    )
    for arrangement in (
        WorkArrangement.ONSITE,
        WorkArrangement.HYBRID,
        WorkArrangement.UNKNOWN,
    ):
        result = evaluate_eligibility(
            make_record(
                location_text="Makati, Philippines",
                work_arrangement=arrangement,
            ),
            policy,
        )
        assert result.eligibility_decision is EligibilityDecision.MANUAL_REVIEW


def test_remote_location_restrictions_exclude_country_locked_roles() -> None:
    policy = LocationPolicy(mode=LocationMode.REMOTE_FIRST)
    excluded = evaluate_eligibility(
        make_record(
            country="United States",
            remote_location_restrictions=["United States only"],
            work_arrangement=WorkArrangement.REMOTE,
        ),
        policy,
    )
    included = evaluate_eligibility(
        make_record(
            country="",
            remote_location_restrictions=["APAC"],
            work_arrangement=WorkArrangement.REMOTE,
        ),
        policy,
    )
    assert excluded.eligibility_decision is EligibilityDecision.INELIGIBLE
    assert included.eligibility_decision is EligibilityDecision.ELIGIBLE


def test_transparent_scoring_uses_verified_evidence(config_dir, verified_claim) -> None:
    record = make_record()
    opportunity = build_opportunities([record])[0]
    eligibility = evaluate_eligibility(record, LocationPolicy(mode=LocationMode.REMOTE_FIRST))
    opportunity = opportunity.model_copy(
        update={
            "location_band": eligibility.location_band,
            "location_decision": eligibility.location_decision,
            "eligibility_decision": eligibility.eligibility_decision,
        }
    )
    scored = score_opportunity(opportunity, load_search_preferences(config_dir), [verified_claim])
    assert scored.rule_score == scored.final_score
    assert scored.fit_band in {FitBand.STRONG, FitBand.REVIEW}
    assert scored.matched_evidence_ids == [verified_claim.claim_id]


def test_categories_are_not_misrepresented_as_required_skills(config_dir) -> None:
    record = make_record(
        title="Customer Support",
        description_excerpt="",
        requirements=[],
        tags=["engineering", "python", "remote"],
    )
    opportunity = build_opportunities([record])[0].model_copy(
        update={"eligibility_decision": EligibilityDecision.ELIGIBLE}
    )

    scored = score_opportunity(opportunity, load_search_preferences(config_dir), [])

    assert scored.score_breakdown is not None
    assert scored.score_breakdown.required_skill_match == 5


def test_qualified_va_role_is_visible_as_low_priority_without_claim_inflation(
    config_dir,
) -> None:
    record = make_record(
        title="Virtual Assistant",
        description_excerpt="General administrative support.",
        employment_type="full-time",
        requirements=[],
        opportunity_type=OpportunityType.VA_FREELANCE,
    )
    opportunity = build_opportunities([record])[0].model_copy(
        update={"eligibility_decision": EligibilityDecision.ELIGIBLE}
    )

    scored = score_opportunity(opportunity, load_search_preferences(config_dir), [])

    assert scored.final_score >= 25
    assert scored.fit_band is FitBand.LOW
    assert scored.matched_evidence_ids == []


def test_plausible_technical_role_is_visible_at_thirty_without_verified_claims(
    config_dir,
) -> None:
    record = make_record(
        title="Junior Python Developer",
        description_excerpt="Build Python services.",
        employment_type="full-time",
        requirements=[],
        opportunity_type=OpportunityType.TECHNICAL,
    )
    opportunity = build_opportunities([record])[0].model_copy(
        update={"eligibility_decision": EligibilityDecision.ELIGIBLE}
    )

    scored = score_opportunity(opportunity, load_search_preferences(config_dir), [])

    assert scored.final_score >= 30
    assert scored.fit_band is FitBand.LOW


def test_low_priority_floor_never_overrides_ineligibility(config_dir) -> None:
    record = make_record(
        title="Virtual Assistant",
        description_excerpt="Administrative support, scheduling, and data entry.",
        opportunity_type=OpportunityType.VA_FREELANCE,
    )
    opportunity = build_opportunities([record])[0].model_copy(
        update={"eligibility_decision": EligibilityDecision.INELIGIBLE}
    )

    scored = score_opportunity(opportunity, load_search_preferences(config_dir), [])

    assert scored.fit_band is FitBand.SKIP

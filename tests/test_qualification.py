from __future__ import annotations

import json

import pytest
from conftest import make_record
from pydantic import ValidationError

from jobhunt.config import load_candidate_profile
from jobhunt.models import (
    ApplicantCostDecision,
    CandidateProfile,
    CompensationDecision,
    QualificationDecision,
    SeniorityLevel,
)
from jobhunt.qualification import assess_candidate_qualification, infer_seniority


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senior Software Engineer", SeniorityLevel.SENIOR),
        ("Senior React Developer", SeniorityLevel.SENIOR),
        ("Sr. Backend Developer", SeniorityLevel.SENIOR),
        ("Staff Data Engineer", SeniorityLevel.STAFF),
        ("Principal Solutions Architect", SeniorityLevel.PRINCIPAL),
        ("Lead Frontend Developer", SeniorityLevel.LEAD),
        ("Lead Python Engineer", SeniorityLevel.LEAD),
        ("Technical Lead", SeniorityLevel.LEAD),
        ("Engineering Manager", SeniorityLevel.MANAGER),
        ("Associate Director of Engineering", SeniorityLevel.DIRECTOR),
        ("Chief Technology Officer", SeniorityLevel.EXECUTIVE),
        ("Junior Developer", SeniorityLevel.JUNIOR),
        ("Entry-level QA Analyst", SeniorityLevel.ENTRY),
        ("Graduate Software Engineer", SeniorityLevel.ENTRY),
        ("Software Engineering Intern", SeniorityLevel.INTERNSHIP),
        ("Associate Software Developer", SeniorityLevel.ASSOCIATE),
        ("Mid-level Software Developer", SeniorityLevel.MID),
    ],
)
def test_infer_seniority_uses_role_aware_patterns(title: str, expected: SeniorityLevel) -> None:
    assert infer_seniority(title) is expected


@pytest.mark.parametrize(
    "title",
    [
        "Senior Care Coordinator",
        "Senior Living Software Developer",
        "Lead Generation Specialist",
        "Executive Assistant",
        "Staffing Coordinator",
    ],
)
def test_infer_seniority_avoids_known_false_positives(title: str) -> None:
    assert infer_seniority(title) is SeniorityLevel.UNSPECIFIED


def test_excluded_seniority_is_a_hard_failure() -> None:
    result = assess_candidate_qualification(
        make_record(title="Senior Full Stack Developer"), CandidateProfile()
    )
    assert result.decision is QualificationDecision.INELIGIBLE
    assert result.seniority_level is SeniorityLevel.SENIOR
    assert "excluded_seniority" in result.reason_codes


def test_target_seniority_with_no_other_blocker_is_eligible() -> None:
    result = assess_candidate_qualification(
        make_record(title="Junior Full Stack Developer"), CandidateProfile()
    )
    assert result.decision is QualificationDecision.ELIGIBLE
    assert result.reason_codes == []


def test_unspecified_seniority_requires_review_by_default() -> None:
    result = assess_candidate_qualification(make_record(), CandidateProfile())
    assert result.decision is QualificationDecision.MANUAL_REVIEW
    assert "seniority_unspecified" in result.reason_codes


def test_structured_source_seniority_is_used_when_title_is_neutral() -> None:
    result = assess_candidate_qualification(
        make_record(title="Software Developer", tags=["Entry-level"]),
        CandidateProfile(),
    )
    assert result.seniority_level is SeniorityLevel.ENTRY
    assert result.decision is QualificationDecision.ELIGIBLE


def test_mandatory_experience_above_verified_maximum_is_ineligible() -> None:
    profile = CandidateProfile(maximum_verified_professional_experience_years=1)
    result = assess_candidate_qualification(
        make_record(
            title="Junior Developer",
            description_excerpt="Candidates must have 3+ years of experience in web development.",
            requirements=[],
        ),
        profile,
    )
    assert result.decision is QualificationDecision.INELIGIBLE
    assert result.mandatory_experience_years == 3
    assert "experience_requirement_exceeds_profile" in result.reason_codes


def test_mandatory_experience_uses_lower_bound_and_supports_words() -> None:
    profile = CandidateProfile(maximum_verified_professional_experience_years=1)
    numeric_range = assess_candidate_qualification(
        make_record(
            title="Junior Developer",
            description_excerpt="Requires 2-4 years of professional experience.",
            requirements=[],
        ),
        profile,
    )
    word_minimum = assess_candidate_qualification(
        make_record(
            title="Junior Developer",
            description_excerpt="Minimum two years of relevant experience required.",
            requirements=[],
        ),
        profile,
    )
    assert numeric_range.mandatory_experience_years == 2
    assert word_minimum.mandatory_experience_years == 2


def test_unverified_experience_profile_routes_numeric_requirement_to_review() -> None:
    result = assess_candidate_qualification(
        make_record(
            title="Junior Developer",
            description_excerpt="At least one year of experience is required.",
            requirements=[],
        ),
        CandidateProfile(),
    )
    assert result.decision is QualificationDecision.MANUAL_REVIEW
    assert "experience_profile_unverified" in result.reason_codes


def test_early_career_search_cap_excludes_high_experience_even_if_profile_is_unverified() -> None:
    result = assess_candidate_qualification(
        make_record(
            title="Software Developer",
            description_excerpt="At least 5 years of experience is required.",
            requirements=[],
        ),
        CandidateProfile(maximum_target_experience_years=3),
    )
    assert result.decision is QualificationDecision.INELIGIBLE
    assert "experience_requirement_exceeds_target" in result.reason_codes


@pytest.mark.parametrize(
    "description",
    [
        "Three years of experience preferred, but equivalent projects are welcome.",
        "Our team has 10 years of experience delivering workflow tools.",
        "Applicants with up to 3 years of experience are welcome.",
    ],
)
def test_nonmandatory_year_statements_do_not_create_a_requirement(description: str) -> None:
    result = assess_candidate_qualification(
        make_record(title="Junior Developer", description_excerpt=description, requirements=[]),
        CandidateProfile(maximum_verified_professional_experience_years=0),
    )
    assert result.decision is QualificationDecision.ELIGIBLE
    assert result.mandatory_experience_years is None


def test_ambiguous_experience_requirement_routes_to_review() -> None:
    result = assess_candidate_qualification(
        make_record(
            title="Junior Developer",
            description_excerpt="Several years of experience are required.",
            requirements=[],
        ),
        CandidateProfile(maximum_verified_professional_experience_years=1),
    )
    assert result.decision is QualificationDecision.MANUAL_REVIEW
    assert "experience_requirement_ambiguous" in result.reason_codes


def test_implausible_experience_is_discarded_and_routes_to_review() -> None:
    result = assess_candidate_qualification(
        make_record(
            title="Junior Developer",
            description_excerpt="Applicants must have at least 130 years of experience.",
            requirements=[],
        ),
        CandidateProfile(maximum_verified_professional_experience_years=1),
    )

    assert result.decision is QualificationDecision.MANUAL_REVIEW
    assert result.mandatory_experience_years is None
    assert "experience_requirement_implausible" in result.reason_codes


def test_implausible_experience_does_not_replace_a_valid_requirement() -> None:
    result = assess_candidate_qualification(
        make_record(
            title="Junior Developer",
            description_excerpt=(
                "Applicants need 2 years of experience. "
                "A malformed source field says 130 years of experience."
            ),
            requirements=[],
        ),
        CandidateProfile(maximum_verified_professional_experience_years=2),
    )

    assert result.decision is QualificationDecision.MANUAL_REVIEW
    assert result.mandatory_experience_years == 2
    assert "experience_requirement_implausible" in result.reason_codes


def test_explicit_source_applicant_cost_overrides_silent_listing() -> None:
    result = assess_candidate_qualification(
        make_record(
            title="Junior Developer",
            applicant_cost=ApplicantCostDecision.PAYMENT_REQUIRED,
        ),
        CandidateProfile(),
    )
    assert result.decision is QualificationDecision.INELIGIBLE
    assert result.applicant_cost_decision is ApplicantCostDecision.PAYMENT_REQUIRED
    assert "applicant_payment_required" in result.reason_codes


def test_listing_payment_signal_overrides_source_free_to_apply_default() -> None:
    result = assess_candidate_qualification(
        make_record(
            title="Junior Developer",
            applicant_cost=ApplicantCostDecision.FREE_TO_APPLY,
            description_excerpt="No application fee. A training fee is required before starting.",
            requirements=[],
        ),
        CandidateProfile(),
    )
    assert result.decision is QualificationDecision.INELIGIBLE
    assert result.applicant_cost_decision is ApplicantCostDecision.PAYMENT_REQUIRED


def test_fee_language_is_fail_closed_but_explicit_no_fee_is_not() -> None:
    paid = assess_candidate_qualification(
        make_record(
            title="Junior Developer",
            description_excerpt="A refundable equipment deposit is required before starting.",
            requirements=[],
        ),
        CandidateProfile(),
    )
    free = assess_candidate_qualification(
        make_record(
            title="Junior Developer",
            description_excerpt="We never charge candidates application fees.",
            requirements=[],
        ),
        CandidateProfile(),
    )
    ambiguous = assess_candidate_qualification(
        make_record(
            title="Junior Developer",
            description_excerpt="Equipment costs may apply.",
            requirements=[],
        ),
        CandidateProfile(),
    )
    assert paid.decision is QualificationDecision.INELIGIBLE
    assert paid.applicant_cost_decision is ApplicantCostDecision.PAYMENT_REQUIRED
    assert free.decision is QualificationDecision.ELIGIBLE
    assert free.applicant_cost_decision is ApplicantCostDecision.FREE_TO_APPLY
    assert ambiguous.decision is QualificationDecision.MANUAL_REVIEW
    assert ambiguous.applicant_cost_decision is ApplicantCostDecision.AMBIGUOUS


def test_cost_silence_is_recorded_without_rejecting_an_otherwise_eligible_role() -> None:
    result = assess_candidate_qualification(
        make_record(title="Junior Developer"), CandidateProfile()
    )
    assert result.decision is QualificationDecision.ELIGIBLE
    assert result.applicant_cost_decision is ApplicantCostDecision.NOT_STATED


def test_explicitly_unpaid_role_is_ineligible_and_unknown_pay_is_configurable() -> None:
    unpaid = assess_candidate_qualification(
        make_record(
            title="Junior Developer",
            description_excerpt="This is an unpaid internship position.",
            requirements=[],
        ),
        CandidateProfile(),
    )
    unknown = assess_candidate_qualification(
        make_record(title="Junior Developer"),
        CandidateProfile(require_explicit_compensation=True),
    )
    assert unpaid.decision is QualificationDecision.INELIGIBLE
    assert unpaid.compensation_decision is CompensationDecision.UNPAID
    assert unknown.decision is QualificationDecision.MANUAL_REVIEW
    assert "compensation_not_stated" in unknown.reason_codes


def test_missing_qualification_details_require_review() -> None:
    result = assess_candidate_qualification(
        make_record(
            title="Junior Developer",
            description_excerpt="",
            requirements=[],
        ),
        CandidateProfile(),
    )
    assert result.decision is QualificationDecision.MANUAL_REVIEW
    assert "qualification_details_missing" in result.reason_codes


def test_checked_in_candidate_profile_is_conservative(config_dir) -> None:
    profile = load_candidate_profile(config_dir)
    assert SeniorityLevel.JUNIOR in profile.target_seniority_levels
    assert SeniorityLevel.SENIOR in profile.excluded_seniority_levels
    assert profile.maximum_verified_professional_experience_years is None
    assert profile.maximum_target_experience_years == 3
    assert profile.review_unspecified_seniority
    assert not profile.allow_unpaid_roles


def test_candidate_profile_rejects_conflicting_seniority_configuration(tmp_path) -> None:
    path = tmp_path / "candidate_profile.json"
    path.write_text(
        json.dumps(
            {
                "target_seniority_levels": ["junior"],
                "excluded_seniority_levels": ["junior"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="overlap"):
        load_candidate_profile(tmp_path)


def test_candidate_profile_model_rejects_hard_exclusion_of_unspecified() -> None:
    with pytest.raises(ValidationError, match="unspecified seniority"):
        CandidateProfile(excluded_seniority_levels=[SeniorityLevel.UNSPECIFIED])

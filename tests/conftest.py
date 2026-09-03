from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobhunt.canonical import canonical_key
from jobhunt.models import (
    EligibilityDecision,
    FitBand,
    LocationDecision,
    Opportunity,
    OpportunityType,
    ProfileClaim,
    RetentionClass,
    ScoreBreakdown,
    SourceKind,
    SourceRecord,
    WorkArrangement,
)


@pytest.fixture
def config_dir() -> Path:
    return Path("config")


@pytest.fixture
def verified_claim() -> ProfileClaim:
    return ProfileClaim(
        claim_id="verified_python_project",
        statement="Built a Python and TypeScript workflow dashboard.",
        evidence_url="https://example.com/evidence",
        scope="Test evidence",
        skills=["python", "typescript", "workflow", "dashboard"],
        verified=True,
    )


def make_record(**updates: object) -> SourceRecord:
    values: dict[str, object] = {
        "source": SourceKind.MANUAL,
        "source_record_id": "record-1",
        "source_url": "https://example.com/jobs/1",
        "apply_url": "https://example.com/jobs/1/apply",
        "company": "Example Company",
        "title": "Full Stack Automation Developer",
        "location_text": "Remote - Philippines",
        "country": "Philippines",
        "work_arrangement": WorkArrangement.REMOTE,
        "opportunity_type": OpportunityType.TECHNICAL,
        "employment_type": "Full-time",
        "description_excerpt": "Build Python TypeScript React workflow dashboards.",
        "requirements": ["python", "typescript", "react"],
        "published_at": datetime(2026, 8, 30, tzinfo=UTC),
        "retrieved_at": datetime(2026, 9, 1, tzinfo=UTC),
        "attribution": "Manual test fixture",
        "retention_class": RetentionClass.EXCERPT,
        "ai_processing_allowed": False,
    }
    values.update(updates)
    return SourceRecord.model_validate(values)


def make_opportunity(**record_updates: object) -> Opportunity:
    record = make_record(**record_updates)
    return Opportunity(
        canonical_key=canonical_key(record),
        source_record=record,
        location_band="Remote",
        location_decision=LocationDecision.ELIGIBLE,
        eligibility_decision=EligibilityDecision.ELIGIBLE,
        score_breakdown=ScoreBreakdown(
            required_skill_match=35,
            evidence_relevance=25,
            scope_seniority=15,
            location_eligibility=15,
            engagement_alignment=5,
            listing_completeness=5,
        ),
        rule_score=100,
        final_score=100,
        fit_band=FitBand.STRONG,
    )

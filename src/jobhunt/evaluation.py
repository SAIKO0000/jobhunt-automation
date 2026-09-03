from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from jobhunt.ai import AnalysisProvider, apply_ai_analysis
from jobhunt.canonical import canonical_key
from jobhunt.models import (
    EligibilityDecision,
    FitBand,
    LocationDecision,
    Opportunity,
    ProfileClaim,
    RetentionClass,
    ScoreBreakdown,
    SourceKind,
    SourceRecord,
    WorkArrangement,
)


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    title: str
    description_excerpt: str
    requirements: list[str] = Field(default_factory=list)
    expected_positive_fit: bool


class EvaluationFixtureSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verified_claims: list[ProfileClaim]
    cases: list[EvaluationCase]


class EvaluationReport(BaseModel):
    total: int
    completed: int
    label_matches: int
    errors: list[str]
    passed: bool


def load_evaluation_fixtures(path: Path) -> EvaluationFixtureSet:
    fixtures = EvaluationFixtureSet.model_validate_json(path.read_text(encoding="utf-8"))
    if len(fixtures.cases) < 20:
        raise ValueError("Ollama evaluation requires at least 20 labeled cases")
    if not fixtures.verified_claims or any(
        not claim.verified for claim in fixtures.verified_claims
    ):
        raise ValueError("Evaluation fixtures require synthetic, explicitly verified claims")
    return fixtures


def evaluate_provider(
    provider: AnalysisProvider, fixtures: EvaluationFixtureSet
) -> EvaluationReport:
    completed = 0
    label_matches = 0
    errors: list[str] = []
    for case in fixtures.cases:
        opportunity = _opportunity(case)
        try:
            response = provider.analyze(opportunity, fixtures.verified_claims)
            analyzed = apply_ai_analysis(opportunity, response)
        except Exception as exc:
            errors.append(f"{case.case_id}: {exc.__class__.__name__}: {str(exc)[:160]}")
            continue
        completed += 1
        positive = analyzed.fit_band in {FitBand.STRONG, FitBand.REVIEW}
        if positive == case.expected_positive_fit:
            label_matches += 1
    total = len(fixtures.cases)
    passed = completed == total and label_matches / total >= 0.85 and not errors
    return EvaluationReport(
        total=total,
        completed=completed,
        label_matches=label_matches,
        errors=errors,
        passed=passed,
    )


def _opportunity(case: EvaluationCase) -> Opportunity:
    record = SourceRecord(
        source=SourceKind.MANUAL,
        source_record_id=f"evaluation-{case.case_id}",
        source_url=f"https://example.com/evaluation/{case.case_id}",
        company="Synthetic Evaluation Company",
        title=case.title,
        location_text="Remote - Philippines",
        country="Philippines",
        work_arrangement=WorkArrangement.REMOTE,
        description_excerpt=case.description_excerpt,
        requirements=case.requirements,
        attribution="Synthetic local evaluation fixture",
        retention_class=RetentionClass.EXCERPT,
        ai_processing_allowed=True,
    )
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

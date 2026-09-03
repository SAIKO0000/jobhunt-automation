from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jobhunt.leads import LeadPolicyError, assess_lead
from jobhunt.models import Lead, ReviewStatus, RunResult
from jobhunt.workbook.gateway import LocalJsonWorkbook
from jobhunt.workbook.schema import COLD_LEAD_COLUMNS


def _lead(**updates) -> Lead:
    values = {
        "business_name": "Example Clinic",
        "official_domain": "https://clinic.example.com",
        "area": "Makati",
        "industry": "Healthcare",
        "seeded_by": "Mark",
        "verification_source_url": "https://databank.business.gov.ph/example-clinic",
        "verified_at": datetime(2026, 9, 2, tzinfo=UTC),
        "observed_problem": "the public booking process appears to use manual handoffs",
        "evidence_url": "https://clinic.example.com/appointments",
        "service_hypothesis": "an approval-based booking and operations dashboard",
        "lawful_basis_status": "Business data only",
    }
    values.update(updates)
    return Lead.model_validate(values)


def test_user_seeded_business_lead_gets_review_only_draft() -> None:
    assessed = assess_lead(_lead())
    assert assessed.ai_relevance_score == 100
    assert assessed.review_status is ReviewStatus.NEEDS_REVIEW
    assert assessed.draft
    assert assessed.draft_fact_ids
    assert "sent manually" in assessed.draft


def test_suppression_prevents_drafting() -> None:
    assessed = assess_lead(_lead(opted_out=True))
    assert assessed.ai_relevance_score == 0
    assert assessed.draft == ""


def test_personal_data_requires_documented_lawful_basis() -> None:
    with pytest.raises(LeadPolicyError):
        assess_lead(
            _lead(
                contains_personal_data=True,
                public_contact="person@example.com",
                lawful_basis_status="Not assessed",
            )
        )


def test_lead_commit_initializes_sheet_without_outbound_action(tmp_path) -> None:
    workbook = LocalJsonWorkbook(tmp_path / "workbook.json")
    workbook.ensure_schema()
    assessed = assess_lead(_lead())
    run = RunResult(trigger="manual-lead", status="success", records_fetched=1)
    plan = workbook.commit_leads(workbook.snapshot(), [assessed], run)
    assert plan.records_written == 1
    row = workbook.snapshot().tabs["Cold Outreach Leads"][1]
    assert row[COLD_LEAD_COLUMNS.index("Review Status")] == "Needs Review"
    assert row[COLD_LEAD_COLUMNS.index("Manual Contact At")] == ""

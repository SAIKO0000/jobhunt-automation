from __future__ import annotations

import pytest
from pydantic import ValidationError

from jobhunt.config import (
    load_analysis_config,
    load_location_policy,
    load_profile_claims,
    load_source_manifests,
    validate_all,
)
from jobhunt.models import ApplicantCostDecision, LocationMode, SourceManifest


def test_checked_in_configuration_activates_only_owner_approved_feeds(config_dir) -> None:
    manifests = load_source_manifests(config_dir)
    assert manifests
    active = {
        adapter_id for adapter_id, item in manifests.items() if item.enabled and item.owner_approved
    }
    assert active == {"himalayas", "jobicy"}
    assert all(
        not item.enabled and not item.owner_approved
        for adapter_id, item in manifests.items()
        if adapter_id not in active
    )
    assert all(not item.ai_processing_allowed for item in manifests.values())
    assert manifests["remoteok"].default_applicant_cost is ApplicantCostDecision.PAYMENT_REQUIRED
    assert manifests["wwr"].default_applicant_cost is ApplicantCostDecision.PAYMENT_REQUIRED
    assert manifests["himalayas"].default_applicant_cost is ApplicantCostDecision.FREE_TO_APPLY
    assert manifests["jobicy"].default_applicant_cost is ApplicantCostDecision.FREE_TO_APPLY
    assert manifests["himalayas"].endpoint is not None
    assert "q=virtual%20assistant" in manifests["himalayas"].endpoint
    assert "country=Philippines" in manifests["himalayas"].endpoint
    assert "seniority=Entry-level" in manifests["himalayas"].endpoint
    assert len(manifests["himalayas"].fetch_endpoints) == 4
    assert any(
        "q=software%20developer" in value for value in manifests["himalayas"].fetch_endpoints
    )
    assert any("worldwide=true" in value for value in manifests["himalayas"].fetch_endpoints)
    assert len(manifests["jobicy"].fetch_endpoints) == 2
    assert all("industry=engineering" in value for value in manifests["jobicy"].fetch_endpoints)
    assert any("geo=apac" in value for value in manifests["jobicy"].fetch_endpoints)
    verified_claims = load_profile_claims(config_dir, verified_only=True)
    assert {claim.claim_id for claim in verified_claims} == {
        "portfolio_nextjs",
        "relay_operations_workspace",
        "accounting_modernization",
        "resource_hive_booking",
        "shoulder_dss_cv",
    }
    assert all(claim.verified for claim in verified_claims)
    resource_hive = next(
        claim
        for claim in load_profile_claims(config_dir, verified_only=False)
        if claim.claim_id == "resource_hive_booking"
    )
    assert "firebase" in resource_hive.skills
    assert "php" not in resource_hive.skills
    policy = load_location_policy(config_dir)
    assert policy.mode is LocationMode.REMOTE_FIRST
    assert policy.under_one_hour_areas == []
    assert policy.one_to_two_hour_areas == []
    diagnostics = validate_all(config_dir)
    assert len(diagnostics) == 1
    assert diagnostics[0].level == "info"
    analysis = load_analysis_config(config_dir)
    assert not analysis.gemini.enabled
    assert not analysis.ollama.enabled


def test_source_manifest_rejects_unapproved_or_duplicate_query_endpoints() -> None:
    base = {
        "adapter_id": "jobicy",
        "display_name": "Jobicy",
        "allowed_hosts": ["jobicy.com"],
        "attribution": "Jobicy",
    }
    with pytest.raises(ValidationError, match="not allowed"):
        SourceManifest.model_validate(
            {
                **base,
                "endpoint": "https://evil.example/jobs",
            }
        )
    with pytest.raises(ValidationError, match="must be unique"):
        SourceManifest.model_validate(
            {
                **base,
                "endpoint": "https://jobicy.com/api/v2/remote-jobs",
                "additional_endpoints": ["https://jobicy.com/api/v2/remote-jobs"],
            }
        )

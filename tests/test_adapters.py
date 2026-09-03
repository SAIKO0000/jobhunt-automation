from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from jobhunt.adapters import adapter_registry
from jobhunt.adapters.common import infer_opportunity_type
from jobhunt.config import load_source_manifests
from jobhunt.models import (
    ApplicantCostDecision,
    OpportunityType,
    SourceKind,
    WorkArrangement,
)
from jobhunt.security import SafeHttpClient


@pytest.mark.parametrize(
    ("source", "filename"),
    [
        (SourceKind.REMOTEOK, "remoteok.json"),
        (SourceKind.WWR, "wwr.xml"),
        (SourceKind.HIMALAYAS, "himalayas.json"),
        (SourceKind.JOBICY, "jobicy.json"),
        (SourceKind.REMOTIVE, "remotive.json"),
        (SourceKind.GREENHOUSE, "greenhouse.json"),
        (SourceKind.LEVER, "lever.json"),
        (SourceKind.ASHBY, "ashby.json"),
        (SourceKind.MANUAL, "manual.json"),
    ],
)
def test_fixture_contracts(config_dir: Path, source: SourceKind, filename: str) -> None:
    path = Path("tests/fixtures") / filename
    text = path.read_text(encoding="utf-8")
    payload = text if path.suffix == ".xml" else json.loads(text)
    manifest = load_source_manifests(config_dir)[source]
    batch = adapter_registry()[source].parse_payload(payload, manifest)
    assert batch.records
    assert all(record.source is source for record in batch.records)
    assert all(record.attribution for record in batch.records)


def test_remote_sources_are_not_fetchable_without_approval(config_dir: Path) -> None:
    manifest = load_source_manifests(config_dir)[SourceKind.REMOTEOK].model_copy(
        update={"enabled": False, "owner_approved": False}
    )
    with pytest.raises(PermissionError):
        adapter_registry()[SourceKind.REMOTEOK].fetch(manifest)


def test_ashby_ignores_unlisted_jobs(config_dir: Path) -> None:
    payload = json.loads(Path("tests/fixtures/ashby.json").read_text(encoding="utf-8"))
    manifest = load_source_manifests(config_dir)[SourceKind.ASHBY]
    batch = adapter_registry()[SourceKind.ASHBY].parse_payload(payload, manifest)
    assert [record.source_record_id for record in batch.records] == ["ashby-1"]


def test_ats_company_slug_is_preserved(config_dir: Path) -> None:
    fixtures = Path("tests/fixtures")
    manifests = load_source_manifests(config_dir)
    greenhouse = adapter_registry()[SourceKind.GREENHOUSE].parse_payload(
        json.loads((fixtures / "greenhouse.json").read_text(encoding="utf-8")),
        manifests[SourceKind.GREENHOUSE],
    )
    lever = adapter_registry()[SourceKind.LEVER].parse_payload(
        json.loads((fixtures / "lever.json").read_text(encoding="utf-8")),
        manifests[SourceKind.LEVER],
    )
    assert greenhouse.records[0].company == "Acme"
    assert lever.records[0].company == "Acme"


def test_manual_intake_retains_only_the_bounded_excerpt(config_dir: Path) -> None:
    payload = json.loads(Path("tests/fixtures/manual.json").read_text(encoding="utf-8"))
    manifest = load_source_manifests(config_dir)[SourceKind.MANUAL]
    record = adapter_registry()[SourceKind.MANUAL].parse_payload(payload, manifest).records[0]
    assert record.description_excerpt == "Short user-selected listing excerpt."
    assert len(record.description_excerpt) <= 4_000
    assert record.work_arrangement is WorkArrangement.REMOTE


def test_himalayas_maps_cursor_location_salary_and_sanitized_excerpt(config_dir: Path) -> None:
    payload = json.loads(Path("tests/fixtures/himalayas.json").read_text(encoding="utf-8"))
    manifest = load_source_manifests(config_dir)[SourceKind.HIMALAYAS]
    batch = adapter_registry()[SourceKind.HIMALAYAS].parse_payload(payload, manifest)

    assert batch.next_cursor == "fixture-cursor-2"
    assert len(batch.records) == 1
    assert batch.warnings == ["Himalayas record skipped: record must be an object"]
    record = batch.records[0]
    assert record.company == "Northstar Systems"
    assert record.location_text == "Remote - Philippines"
    assert record.country == "Philippines"
    assert record.work_arrangement is WorkArrangement.REMOTE
    assert record.salary_raw == "USD 36000-48000 annual"
    assert record.description_excerpt == "Build Python workflow tools and dashboards."
    assert record.source_url.startswith("https://himalayas.app/")


@pytest.mark.parametrize(
    ("source", "filename", "expected_company"),
    [
        (SourceKind.JOBICY, "jobicy.json", "Acme Remote"),
        (SourceKind.REMOTIVE, "remotive.json", "Example Operations"),
    ],
)
def test_public_json_adapters_skip_invalid_records_and_preserve_attribution(
    config_dir: Path, source: SourceKind, filename: str, expected_company: str
) -> None:
    payload = json.loads((Path("tests/fixtures") / filename).read_text(encoding="utf-8"))
    manifest = load_source_manifests(config_dir)[source]
    batch = adapter_registry()[source].parse_payload(payload, manifest)

    assert len(batch.records) == 1
    assert len(batch.warnings) == 1
    record = batch.records[0]
    assert record.company == expected_company
    assert record.work_arrangement is WorkArrangement.REMOTE
    assert record.attribution == manifest.attribution
    assert record.source_url.startswith("https://")


@pytest.mark.parametrize("source", [SourceKind.HIMALAYAS, SourceKind.JOBICY])
def test_zero_budget_public_sources_are_approved_and_free_to_apply(
    config_dir: Path, source: SourceKind
) -> None:
    manifest = load_source_manifests(config_dir)[source]
    assert manifest.enabled
    assert manifest.owner_approved
    assert manifest.default_applicant_cost is ApplicantCostDecision.FREE_TO_APPLY


def test_remotive_remains_disabled_and_unapproved(config_dir: Path) -> None:
    manifest = load_source_manifests(config_dir)[SourceKind.REMOTIVE]
    assert not manifest.enabled
    assert not manifest.owner_approved
    with pytest.raises(PermissionError):
        adapter_registry()[SourceKind.REMOTIVE].fetch(manifest)


@pytest.mark.parametrize(
    "title",
    [
        "Medical Virtual Assistant",
        "Virtual Medical Assistant - Communications Specialist",
        "Virtual Medical Biller",
        "Medical Patient Coordinator",
        "Remote Bookkeeper - PH",
        "Appointment Setter",
        "Data Entry Specialist",
        "Executive Assistant, Operations",
        "Social Media & Admin Assistant",
        "Home-Based Accounting Assistant",
        "Technical Operations Coordinator",
    ],
)
def test_clear_assistant_and_operations_titles_use_va_track(title: str) -> None:
    assert infer_opportunity_type(title, []) is OpportunityType.VA_FREELANCE


def test_unrelated_customer_support_role_stays_technical_track() -> None:
    assert infer_opportunity_type("Customer Support Engineer", []) is OpportunityType.TECHNICAL


@pytest.mark.parametrize("source", [SourceKind.HIMALAYAS, SourceKind.JOBICY, SourceKind.REMOTIVE])
def test_public_json_adapters_reject_unexpected_top_level_shapes(
    config_dir: Path, source: SourceKind
) -> None:
    manifest = load_source_manifests(config_dir)[source]
    with pytest.raises(ValueError, match="must contain a jobs list"):
        adapter_registry()[source].parse_payload([], manifest)


class _QueryClient:
    def __init__(self, payload: object, *, fail_all: bool = False) -> None:
        self.payload = payload
        self.fail_all = fail_all
        self.calls = 0

    def get_json(self, _: str) -> object:
        self.calls += 1
        if self.fail_all or self.calls == 2:
            raise TimeoutError("simulated query failure")
        return self.payload


def test_multi_query_source_keeps_partial_success_and_reports_failure(
    config_dir: Path,
) -> None:
    payload = json.loads(Path("tests/fixtures/jobicy.json").read_text(encoding="utf-8"))
    manifest = load_source_manifests(config_dir)[SourceKind.JOBICY]
    client = cast(SafeHttpClient, _QueryClient(payload))

    batch = adapter_registry()[SourceKind.JOBICY]._fetch_with_client(client, manifest)

    assert len(batch.records) == 1
    assert batch.warnings[-1].startswith("Jobicy query 2 failed: TimeoutError")


def test_multi_query_source_fails_when_every_query_fails(config_dir: Path) -> None:
    manifest = load_source_manifests(config_dir)[SourceKind.JOBICY]
    client = cast(SafeHttpClient, _QueryClient({}, fail_all=True))

    with pytest.raises(RuntimeError, match="All configured queries failed for jobicy"):
        adapter_registry()[SourceKind.JOBICY]._fetch_with_client(client, manifest)

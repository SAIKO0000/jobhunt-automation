from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jobhunt.adapters import adapter_registry
from jobhunt.adapters.base import SourceAdapter
from jobhunt.adapters.common import infer_country, infer_opportunity_type
from jobhunt.ai import AIValidationError, AnalysisProvider, ProviderUnavailable, apply_ai_analysis
from jobhunt.availability import review_inbox_availability
from jobhunt.backup import SnapshotStore
from jobhunt.budget import QuotaExceeded
from jobhunt.config import (
    load_candidate_profile,
    load_location_policy,
    load_profile_claims,
    load_search_preferences,
    load_source_manifests,
)
from jobhunt.dedupe import build_opportunities
from jobhunt.eligibility import combine_eligibility, evaluate_eligibility
from jobhunt.lease import LeaseContext, RunLease
from jobhunt.models import (
    ApplicantCostDecision,
    EligibilityDecision,
    FetchBatch,
    FitBand,
    Opportunity,
    ProcessingStatus,
    QualificationDecision,
    RunResult,
    SourceKind,
    SourceManifest,
    SourceRecord,
    WorkbookSnapshot,
)
from jobhunt.pending import PendingRun, PendingRunStore
from jobhunt.qualification import assess_candidate_qualification
from jobhunt.scoring import score_opportunity
from jobhunt.workbook.gateway import CommitPlan, WorkbookGateway
from jobhunt.workbook.schema import MANUAL_INTAKE_COLUMNS, SCHEMA_VERSION, initial_tab_values


@dataclass(frozen=True)
class PipelineOutput:
    run: RunResult
    opportunities: list[Opportunity]
    commit: CommitPlan | None = None
    snapshot_identifier: str | None = None


class Pipeline:
    def __init__(
        self,
        *,
        config_dir: Path,
        workbook: WorkbookGateway,
        snapshot_store: SnapshotStore,
        lease: RunLease,
        ai_analyzer: AnalysisProvider | None = None,
        pending_store: PendingRunStore | None = None,
        adapters: dict[SourceKind, SourceAdapter] | None = None,
    ) -> None:
        self.config_dir = config_dir
        self.workbook = workbook
        self.snapshot_store = snapshot_store
        self.lease = lease
        self.ai_analyzer = ai_analyzer
        self.pending_store = pending_store
        self.adapters = adapters or adapter_registry()

    def run(
        self,
        *,
        write: bool,
        fixture_dir: Path | None = None,
        allow_live_sources: bool = False,
        trigger: str = "manual",
        image_digest: str = "local",
    ) -> PipelineOutput:
        result = RunResult(trigger=trigger, image_digest=image_digest)
        lease_context = (
            LeaseContext(self.lease, str(result.run_id), timedelta(minutes=20))
            if write
            else nullcontext()
        )
        with lease_context:
            if write:
                pre_schema = self.workbook.snapshot_existing()
                self.snapshot_store.save(pre_schema, reason=f"pre-schema-{result.run_id}")
                self.workbook.ensure_schema()
                initial_snapshot = self.workbook.snapshot()
            else:
                initial_snapshot = WorkbookSnapshot(
                    schema_version=SCHEMA_VERSION,
                    spreadsheet_id="dry-run",
                    tabs=initial_tab_values(),
                )
            manifests = load_source_manifests(self.config_dir)
            claims = load_profile_claims(self.config_dir, verified_only=True)
            location_policy = load_location_policy(self.config_dir)
            candidate_profile = load_candidate_profile(self.config_dir)
            preferences = load_search_preferences(self.config_dir)
            batches = self._collect(
                manifests,
                result,
                fixture_dir=fixture_dir,
                allow_live_sources=allow_live_sources,
            )
            manual_batch = _manual_intake_batch(
                initial_snapshot, manifests[SourceKind.MANUAL], self.adapters[SourceKind.MANUAL]
            )
            if manual_batch.records or manual_batch.warnings:
                batches.append(_apply_retention(manual_batch, manifests[SourceKind.MANUAL]))
                result.sources_attempted += 1
                result.sources_succeeded += 1
                result.records_quarantined += len(manual_batch.warnings)
                result.checkpoints[SourceKind.MANUAL.value] = manual_batch.fetched_at.isoformat()
            if write and allow_live_sources:
                result.availability_updates = review_inbox_availability(
                    initial_snapshot,
                    batches,
                    manifests,
                    run_id=str(result.run_id),
                )
            records = [record for batch in batches for record in batch.records]
            result.records_fetched = len(records)
            opportunities = build_opportunities(records)
            result.records_deduplicated = len(opportunities)
            processed: list[Opportunity] = []
            ai_rows = 0
            for opportunity in opportunities:
                eligibility = evaluate_eligibility(opportunity.source_record, location_policy)
                qualification = assess_candidate_qualification(
                    opportunity.source_record, candidate_profile
                )
                overall_eligibility = combine_eligibility(
                    eligibility.eligibility_decision, qualification.decision
                )
                opportunity = opportunity.model_copy(
                    update={
                        "location_band": eligibility.location_band,
                        "location_decision": eligibility.location_decision,
                        "eligibility_decision": overall_eligibility,
                        "hard_fail_reasons": [
                            *eligibility.hard_fail_reasons,
                            *(
                                qualification.reasons
                                if qualification.decision is QualificationDecision.INELIGIBLE
                                else []
                            ),
                        ],
                        "qualification_decision": qualification.decision,
                        "seniority_level": qualification.seniority_level,
                        "mandatory_experience_years": (qualification.mandatory_experience_years),
                        "applicant_cost_decision": (qualification.applicant_cost_decision),
                        "compensation_decision": qualification.compensation_decision,
                        "qualification_reason_codes": qualification.reason_codes,
                        "qualification_reasons": qualification.reasons,
                        "processing_status": ProcessingStatus.NORMALIZED,
                    }
                )
                opportunity = score_opportunity(opportunity, preferences, claims)
                opportunity = opportunity.model_copy(
                    update={"processing_status": _status_after_rules(opportunity)}
                )
                if self._should_analyze(opportunity, ai_rows):
                    try:
                        response = self.ai_analyzer.analyze(opportunity, claims)  # type: ignore[union-attr]
                        opportunity = apply_ai_analysis(opportunity, response)
                        opportunity = opportunity.model_copy(
                            update={
                                "processing_status": (
                                    ProcessingStatus.DRAFTED
                                    if opportunity.draft
                                    else ProcessingStatus.SCORED
                                )
                            }
                        )
                        result.ai_input_tokens += response.input_tokens
                        result.ai_output_tokens += response.output_tokens
                        result.ai_provider_counts[response.provider] = (
                            result.ai_provider_counts.get(response.provider, 0) + 1
                        )
                        if response.fallback_reason:
                            result.ai_fallback_count += 1
                        ai_rows += 1
                    except (
                        AIValidationError,
                        QuotaExceeded,
                        ProviderUnavailable,
                        PermissionError,
                        OSError,
                        RuntimeError,
                    ) as exc:
                        opportunity = opportunity.model_copy(
                            update={
                                "processing_status": ProcessingStatus.NEEDS_MANUAL_REVIEW,
                                "ai_error": _safe_error(exc),
                            }
                        )
                processed.append(opportunity)

            result.records_excluded = sum(
                item.processing_status is ProcessingStatus.SKIPPED for item in processed
            )
            result.records_needing_review = sum(
                item.processing_status is ProcessingStatus.NEEDS_MANUAL_REVIEW for item in processed
            )
            result.records_actionable = (
                len(processed) - result.records_excluded - result.records_needing_review
            )

            result.finished_at = datetime.now(UTC)
            result.status = "partial" if result.errors else "success"
            commit: CommitPlan | None = None
            snapshot_identifier: str | None = None
            if write:
                snapshot_identifier = self.snapshot_store.save(
                    initial_snapshot, reason=f"pre-run-{result.run_id}"
                )
                if self.pending_store:
                    self.pending_store.save(
                        PendingRun(
                            initial_snapshot=initial_snapshot,
                            opportunities=processed,
                            run=result,
                            manifests=list(manifests.values()),
                            availability_updates=result.availability_updates,
                        )
                    )
                commit = self.workbook.commit(
                    initial_snapshot,
                    processed,
                    result,
                    list(manifests.values()),
                )
                result.records_written = commit.records_written
                if self.pending_store:
                    self.pending_store.delete(str(result.run_id))
            return PipelineOutput(result, processed, commit, snapshot_identifier)

    def _collect(
        self,
        manifests: dict[SourceKind, SourceManifest],
        result: RunResult,
        *,
        fixture_dir: Path | None,
        allow_live_sources: bool,
    ) -> list[FetchBatch]:
        batches: list[FetchBatch] = []
        if allow_live_sources and not any(
            manifest.enabled and manifest.owner_approved and source is not SourceKind.MANUAL
            for source, manifest in manifests.items()
        ):
            raise PermissionError(
                "--live requires at least one enabled and owner-approved source manifest"
            )
        if fixture_dir:
            for source, adapter in self.adapters.items():
                path = _fixture_path(fixture_dir, source)
                if not path:
                    continue
                manifest = manifests[source]
                result.sources_attempted += 1
                try:
                    batch = adapter.parse_payload(_read_fixture(path), manifest)
                except Exception as exc:
                    result.errors.append(f"{source.value}: {_safe_error(exc)}")
                    continue
                batch = _apply_retention(batch, manifest)
                batches.append(batch)
                result.sources_succeeded += 1
                result.records_quarantined += len(batch.warnings)
                result.checkpoints[source.value] = batch.fetched_at.isoformat()
        if allow_live_sources:
            for source, manifest in manifests.items():
                if (
                    not manifest.enabled
                    or not manifest.owner_approved
                    or source is SourceKind.MANUAL
                ):
                    continue
                result.sources_attempted += 1
                try:
                    batch = self.adapters[source].fetch(manifest)
                except Exception as exc:
                    result.errors.append(f"{source.value}: {_safe_error(exc)}")
                    continue
                batch = _apply_retention(batch, manifest)
                batches.append(batch)
                result.sources_succeeded += 1
                result.records_quarantined += len(batch.warnings)
                result.checkpoints[source.value] = batch.next_cursor or batch.fetched_at.isoformat()
        return batches

    def _should_analyze(self, opportunity: Opportunity, ai_rows: int) -> bool:
        return bool(
            self.ai_analyzer
            and ai_rows < self.ai_analyzer.max_rows_per_run
            and opportunity.source_record.ai_processing_allowed
            and opportunity.eligibility_decision is EligibilityDecision.ELIGIBLE
            and opportunity.fit_band in {FitBand.STRONG, FitBand.REVIEW}
        )


def _fixture_path(directory: Path, source: SourceKind) -> Path | None:
    extensions = ["xml"] if source is SourceKind.WWR else ["json"]
    for extension in extensions:
        candidate = directory / f"{source.value}.{extension}"
        if candidate.exists():
            return candidate
    return None


def _read_fixture(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    return text if path.suffix.casefold() == ".xml" else json.loads(text)


def _status_after_rules(opportunity: Opportunity) -> ProcessingStatus:
    if opportunity.eligibility_decision is EligibilityDecision.INELIGIBLE:
        return ProcessingStatus.SKIPPED
    # Relevance is evaluated before creating a manual-review task so obviously
    # unrelated listings do not flood the uncertainty queue.
    if opportunity.fit_band is FitBand.SKIP:
        return ProcessingStatus.SKIPPED
    if opportunity.eligibility_decision is EligibilityDecision.MANUAL_REVIEW:
        return ProcessingStatus.NEEDS_MANUAL_REVIEW
    return ProcessingStatus.SCORED


def _safe_error(error: BaseException) -> str:
    return f"{error.__class__.__name__}: {str(error)[:300]}"


def _apply_retention(batch: FetchBatch, manifest: SourceManifest) -> FetchBatch:
    records = [
        record.model_copy(
            update={
                "expires_at": (
                    record.expires_at
                    or record.retrieved_at + timedelta(days=manifest.retention_ttl_days)
                ),
                "applicant_cost": (
                    record.applicant_cost
                    if record.applicant_cost.value != "not_stated"
                    else manifest.default_applicant_cost
                ),
            }
        )
        for record in batch.records
    ]
    return batch.model_copy(update={"records": records})


def _manual_intake_batch(
    snapshot: WorkbookSnapshot,
    manifest: SourceManifest,
    adapter: SourceAdapter,
) -> FetchBatch:
    rows = snapshot.tabs.get("Manual Intake", [])
    if len(rows) < 2:
        return FetchBatch(source=SourceKind.MANUAL, records=[])
    header = [str(value) for value in rows[0]]
    if header != MANUAL_INTAKE_COLUMNS:
        return FetchBatch(
            source=SourceKind.MANUAL,
            records=[],
            warnings=["Manual Intake header does not match the current schema"],
        )
    indexes = {name: index for index, name in enumerate(header)}
    records: list[SourceRecord] = []
    warnings: list[str] = []
    for row_number, row in enumerate(rows[1:], start=2):
        if str(_intake_value(row, indexes, "Import Status")).casefold() == "imported":
            continue
        if not any(item not in (None, "") for item in row):
            continue
        platform = str(_intake_value(row, indexes, "Platform") or "Other").strip()
        url = str(_intake_value(row, indexes, "Listing URL") or "").strip()
        company = str(_intake_value(row, indexes, "Company") or "").strip()
        role = str(_intake_value(row, indexes, "Role") or "").strip()
        if not url or not company or not role:
            warnings.append(f"Manual Intake row {row_number} needs Listing URL, Company, and Role")
            continue
        location = str(_intake_value(row, indexes, "Location") or "").strip()
        raw_cost = str(_intake_value(row, indexes, "Application Cost") or "").strip()
        if not raw_cost:
            raw_cost = (
                ApplicantCostDecision.PAYMENT_REQUIRED.value
                if platform.casefold() == "upwork"
                else ApplicantCostDecision.NOT_STATED.value
            )
        else:
            raw_cost = {
                "free to apply": ApplicantCostDecision.FREE_TO_APPLY.value,
                "not stated": ApplicantCostDecision.NOT_STATED.value,
                "check cost": ApplicantCostDecision.AMBIGUOUS.value,
                "payment required": ApplicantCostDecision.PAYMENT_REQUIRED.value,
            }.get(raw_cost.casefold(), raw_cost)
        payload = {
            "source_record_id": f"manual-sheet-{row_number}",
            "source_url": url,
            "apply_url": url,
            "company": company,
            "title": role,
            "location_text": location,
            "country": infer_country(location),
            "remote_location_restrictions": (
                [location]
                if location
                and location.casefold().strip() not in {"remote", "anywhere", "worldwide", "global"}
                else []
            ),
            "work_arrangement": str(
                _intake_value(row, indexes, "Work Arrangement") or "unknown"
            ).casefold(),
            "opportunity_type": infer_opportunity_type(role, []).value,
            "description_excerpt": str(
                _intake_value(row, indexes, "Description / Requirements") or ""
            ),
            "applicant_cost": raw_cost,
        }
        parsed = adapter.parse_payload(payload, manifest)
        if parsed.records:
            records.extend(
                record.model_copy(
                    update={
                        "source_label": platform,
                        "attribution": f"Manually selected from {platform}; follow Source URL",
                    }
                )
                for record in parsed.records
            )
        warnings.extend(f"Manual Intake row {row_number}: {warning}" for warning in parsed.warnings)
    return FetchBatch(source=SourceKind.MANUAL, records=records, warnings=warnings)


def _intake_value(row: list[Any], indexes: dict[str, int], name: str) -> Any:
    index = indexes[name]
    return row[index] if len(row) > index else ""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SourceKind(StrEnum):
    REMOTEOK = "remoteok"
    WWR = "wwr"
    HIMALAYAS = "himalayas"
    JOBICY = "jobicy"
    REMOTIVE = "remotive"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    MANUAL = "manual"


class OpportunityType(StrEnum):
    TECHNICAL = "technical"
    VA_FREELANCE = "va_freelance"


class WorkArrangement(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class LocationMode(StrEnum):
    REMOTE_FIRST = "remote_first"
    COMMUTE_BANDS = "commute_bands"


class LocationDecision(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    MANUAL_REVIEW = "manual_review"


class EligibilityDecision(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    MANUAL_REVIEW = "manual_review"


class ProcessingStatus(StrEnum):
    NEW = "New"
    NORMALIZED = "Normalized"
    SCORED = "Scored"
    DRAFTED = "Drafted"
    NEEDS_MANUAL_REVIEW = "Needs Manual Review"
    SKIPPED = "Skipped"
    QUARANTINED = "Quarantined"
    ERROR = "Error"


class ReviewStatus(StrEnum):
    NEW = "New"
    NEEDS_REVIEW = "Needs Review"
    APPROVED = "Approved"
    APPLIED_SENT = "Applied/Sent"
    RESPONSE = "Response"
    INTERVIEW = "Interview"
    CLOSED_REJECTED = "Closed/Rejected"


class PipelineStage(StrEnum):
    INBOX = "Inbox"
    SHORTLISTED = "Shortlisted"
    PREPARING = "Preparing"
    SUBMITTED = "Submitted"
    INTERVIEW_CALL = "Interview/Call"
    OFFER = "Offer"
    WON = "Won"
    CLOSED = "Closed"


class DraftReviewStatus(StrEnum):
    NOT_NEEDED = "Not needed"
    NEEDS_REVIEW = "Needs review"
    APPROVED = "Approved"
    DISCARDED = "Discarded"


class ClosedReason(StrEnum):
    REJECTED = "Rejected"
    WITHDRAWN = "Withdrawn"
    NO_RESPONSE = "No response"
    EXPIRED = "Expired"
    DUPLICATE = "Duplicate"
    NOT_PURSUED = "Not pursued"
    OTHER = "Other"


class ListingAvailabilityStatus(StrEnum):
    ACTIVE = "active"
    UNAVAILABLE = "unavailable"
    INCONCLUSIVE = "inconclusive"
    EXPIRED = "expired"


class FitBand(StrEnum):
    STRONG = "Strong Fit"
    REVIEW = "Review"
    LOW = "Low Priority"
    SKIP = "Skip"


class QualificationDecision(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    MANUAL_REVIEW = "manual_review"


class SeniorityLevel(StrEnum):
    INTERNSHIP = "internship"
    ENTRY = "entry"
    JUNIOR = "junior"
    ASSOCIATE = "associate"
    MID = "mid"
    UNSPECIFIED = "unspecified"
    SENIOR = "senior"
    STAFF = "staff"
    PRINCIPAL = "principal"
    LEAD = "lead"
    MANAGER = "manager"
    DIRECTOR = "director"
    EXECUTIVE = "executive"


class ApplicantCostDecision(StrEnum):
    NOT_STATED = "not_stated"
    FREE_TO_APPLY = "free_to_apply"
    PAYMENT_REQUIRED = "payment_required"
    AMBIGUOUS = "ambiguous"


class CompensationDecision(StrEnum):
    PAID = "paid"
    UNPAID = "unpaid"
    NOT_STATED = "not_stated"


class RetentionClass(StrEnum):
    METADATA_ONLY = "metadata_only"
    EXCERPT = "excerpt"
    SOURCE_DEFINED = "source_defined"


class SourceManifest(StrictModel):
    adapter_id: SourceKind
    display_name: str
    enabled: bool = False
    owner_approved: bool = False
    endpoint: str | None = None
    additional_endpoints: list[str] = Field(default_factory=list, max_length=10)
    allowed_hosts: list[str] = Field(default_factory=list)
    terms_url: HttpUrl | None = None
    terms_verified_at: datetime | None = None
    allowed_fields: list[str] = Field(default_factory=list)
    attribution: str
    retention_class: RetentionClass = RetentionClass.METADATA_ONLY
    retention_ttl_days: int = Field(default=30, ge=1, le=365)
    ai_processing_allowed: bool = False
    rate_limit_per_minute: int = Field(default=10, ge=1, le=120)
    max_response_bytes: int = Field(default=2_000_000, ge=1_024, le=10_000_000)
    checkpoint: str | None = None
    board_tokens: list[str] = Field(default_factory=list)
    default_applicant_cost: ApplicantCostDecision = ApplicantCostDecision.NOT_STATED

    @model_validator(mode="after")
    def enabled_requires_approval(self) -> SourceManifest:
        if self.enabled and not self.owner_approved:
            raise ValueError("enabled sources require owner_approved=true")
        if self.adapter_id is not SourceKind.MANUAL and not self.allowed_hosts:
            raise ValueError("network sources require an allowed host")
        endpoints = [value for value in [self.endpoint, *self.additional_endpoints] if value]
        if len(endpoints) != len(set(endpoints)):
            raise ValueError("source endpoints must be unique")
        if self.additional_endpoints and not self.endpoint:
            raise ValueError("additional_endpoints require a primary endpoint")
        allowed = {host.casefold().rstrip(".") for host in self.allowed_hosts}
        for endpoint in endpoints:
            parsed = urlsplit(endpoint)
            host = (parsed.hostname or "").casefold().rstrip(".")
            if parsed.scheme != "https" or not host:
                raise ValueError("source endpoints must be absolute HTTPS URLs")
            if parsed.username or parsed.password or parsed.port not in (None, 443):
                raise ValueError("source endpoints may not contain credentials or custom ports")
            if not any(host == item or host.endswith(f".{item}") for item in allowed):
                raise ValueError(f"source endpoint host {host!r} is not allowed")
        return self

    @property
    def fetch_endpoints(self) -> list[str]:
        return [value for value in [self.endpoint, *self.additional_endpoints] if value]

    @field_validator("board_tokens")
    @classmethod
    def validate_board_tokens(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value or not value.replace("-", "").replace("_", "").isalnum():
                raise ValueError("board tokens may contain only letters, numbers, - and _")
        return values


class ProfileClaim(StrictModel):
    claim_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,63}$")
    statement: str
    evidence_url: HttpUrl
    scope: str
    skills: list[str] = Field(default_factory=list)
    verified: bool = False


class LocationPolicy(StrictModel):
    home_country: str = "Philippines"
    mode: LocationMode
    under_one_hour_areas: list[str] = Field(default_factory=list)
    one_to_two_hour_areas: list[str] = Field(default_factory=list)

    @field_validator("under_one_hour_areas", "one_to_two_hour_areas")
    @classmethod
    def normalize_areas(cls, values: list[str]) -> list[str]:
        return sorted({value.casefold().strip() for value in values if value.strip()})


class SearchPreferences(StrictModel):
    skills: list[str] = Field(default_factory=list)
    preferred_title_terms: list[str] = Field(default_factory=list)
    preferred_engagement_terms: list[str] = Field(default_factory=list)
    niche_terms: list[str] = Field(default_factory=list)

    @field_validator("skills", "preferred_title_terms", "preferred_engagement_terms", "niche_terms")
    @classmethod
    def normalize_terms(cls, values: list[str]) -> list[str]:
        return sorted({value.casefold().strip() for value in values if value.strip()})


class CandidateProfile(StrictModel):
    target_seniority_levels: list[SeniorityLevel] = Field(
        default_factory=lambda: [
            SeniorityLevel.INTERNSHIP,
            SeniorityLevel.ENTRY,
            SeniorityLevel.JUNIOR,
            SeniorityLevel.ASSOCIATE,
        ]
    )
    excluded_seniority_levels: list[SeniorityLevel] = Field(
        default_factory=lambda: [
            SeniorityLevel.SENIOR,
            SeniorityLevel.STAFF,
            SeniorityLevel.PRINCIPAL,
            SeniorityLevel.LEAD,
            SeniorityLevel.MANAGER,
            SeniorityLevel.DIRECTOR,
            SeniorityLevel.EXECUTIVE,
        ]
    )
    maximum_verified_professional_experience_years: float | None = Field(default=None, ge=0, le=80)
    maximum_target_experience_years: float = Field(default=3, ge=0, le=80)
    review_unspecified_seniority: bool = True
    allow_unpaid_roles: bool = False
    require_explicit_compensation: bool = False

    @model_validator(mode="after")
    def seniority_sets_must_be_disjoint(self) -> CandidateProfile:
        overlap = set(self.target_seniority_levels) & set(self.excluded_seniority_levels)
        if overlap:
            values = ", ".join(sorted(item.value for item in overlap))
            raise ValueError(f"target and excluded seniority levels overlap: {values}")
        if SeniorityLevel.UNSPECIFIED in self.excluded_seniority_levels:
            raise ValueError(
                "unspecified seniority must use review_unspecified_seniority, not hard exclusion"
            )
        return self


class QualificationAssessment(StrictModel):
    decision: QualificationDecision
    seniority_level: SeniorityLevel
    mandatory_experience_years: float | None = Field(default=None, ge=0, le=80)
    applicant_cost_decision: ApplicantCostDecision
    compensation_decision: CompensationDecision
    reason_codes: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class GeminiAnalysisConfig(StrictModel):
    enabled: bool = False
    model: str = "gemini-3.7-flash"
    privacy_acknowledged_at: datetime | None = None
    max_rows_per_run: int = Field(default=20, ge=1, le=100)
    max_rows_per_day: int = Field(default=40, ge=1, le=500)
    timeout_seconds: float = Field(default=45, ge=1, le=120)

    @model_validator(mode="after")
    def enforce_model_allowlist(self) -> GeminiAnalysisConfig:
        if self.model != "gemini-3.7-flash":
            raise ValueError("The Gemini model allowlist contains only gemini-3.7-flash")
        if self.enabled and self.privacy_acknowledged_at is None:
            raise ValueError("Enabled Gemini analysis requires privacy_acknowledged_at")
        return self


class OllamaAnalysisConfig(StrictModel):
    enabled: bool = False
    evaluation_approved: bool = False
    model: str = "qwen3:4b-instruct-2507-q4_K_M"
    base_url: str = "http://127.0.0.1:11434"
    timeout_seconds: float = Field(default=90, ge=1, le=300)

    @model_validator(mode="after")
    def enforce_local_approved_model(self) -> OllamaAnalysisConfig:
        if self.model != "qwen3:4b-instruct-2507-q4_K_M":
            raise ValueError("The Ollama model allowlist contains only the evaluated Qwen model")
        if self.base_url != "http://127.0.0.1:11434":
            raise ValueError("Ollama is restricted to http://127.0.0.1:11434")
        if self.enabled and not self.evaluation_approved:
            raise ValueError("Enabled Ollama fallback requires evaluation_approved=true")
        return self


class AnalysisConfig(StrictModel):
    gemini: GeminiAnalysisConfig = Field(default_factory=GeminiAnalysisConfig)
    ollama: OllamaAnalysisConfig = Field(default_factory=OllamaAnalysisConfig)


class SourceRecord(StrictModel):
    source: SourceKind
    source_label: str = ""
    source_record_id: str
    source_url: str
    apply_url: str | None = None
    company: str
    title: str
    location_text: str = ""
    country: str = ""
    remote_location_restrictions: list[str] = Field(default_factory=list)
    work_arrangement: WorkArrangement = WorkArrangement.UNKNOWN
    opportunity_type: OpportunityType = OpportunityType.TECHNICAL
    employment_type: str = ""
    engagement_type: str = ""
    salary_raw: str = ""
    currency: str = ""
    salary_min: float | None = None
    salary_max: float | None = None
    description_excerpt: str = Field(default="", max_length=4_000)
    requirements: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    published_at: datetime | None = None
    expires_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=utc_now)
    attribution: str
    retention_class: RetentionClass
    ai_processing_allowed: bool = False
    is_listed: bool = True
    applicant_cost: ApplicantCostDecision = ApplicantCostDecision.NOT_STATED

    @field_validator("source_url", "apply_url")
    @classmethod
    def validate_source_urls(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("source and apply URLs must be absolute HTTPS URLs")
        if parsed.username or parsed.password:
            raise ValueError("credentials in source and apply URLs are prohibited")
        return value


class ScoreBreakdown(StrictModel):
    required_skill_match: int = Field(ge=0, le=35)
    evidence_relevance: int = Field(ge=0, le=25)
    scope_seniority: int = Field(ge=0, le=15)
    location_eligibility: int = Field(ge=0, le=15)
    engagement_alignment: int = Field(ge=0, le=5)
    listing_completeness: int = Field(ge=0, le=5)

    @property
    def total(self) -> int:
        return sum(
            (
                self.required_skill_match,
                self.evidence_relevance,
                self.scope_seniority,
                self.location_eligibility,
                self.engagement_alignment,
                self.listing_completeness,
            )
        )


class AIAnalysis(StrictModel):
    record_id: UUID
    required_skill_score: int = Field(ge=0, le=35)
    evidence_relevance_score: int = Field(ge=0, le=25)
    scope_seniority_score: int = Field(ge=0, le=15)
    matched_evidence_ids: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    rationale: str = Field(max_length=1_500)
    confidence: float = Field(ge=0, le=1)
    draft: str | None = Field(default=None, max_length=2_500)
    draft_fact_ids: list[str] = Field(default_factory=list)


class Opportunity(StrictModel):
    record_id: UUID = Field(default_factory=uuid4)
    canonical_key: str
    duplicate_group_id: str | None = None
    source_record: SourceRecord
    first_seen_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=utc_now)
    location_band: str = ""
    location_decision: LocationDecision = LocationDecision.MANUAL_REVIEW
    eligibility_decision: EligibilityDecision = EligibilityDecision.MANUAL_REVIEW
    hard_fail_reasons: list[str] = Field(default_factory=list)
    qualification_decision: QualificationDecision = QualificationDecision.MANUAL_REVIEW
    seniority_level: SeniorityLevel = SeniorityLevel.UNSPECIFIED
    mandatory_experience_years: float | None = Field(default=None, ge=0, le=80)
    applicant_cost_decision: ApplicantCostDecision = ApplicantCostDecision.NOT_STATED
    compensation_decision: CompensationDecision = CompensationDecision.NOT_STATED
    qualification_reason_codes: list[str] = Field(default_factory=list)
    qualification_reasons: list[str] = Field(default_factory=list)
    score_breakdown: ScoreBreakdown | None = None
    rule_score: int | None = Field(default=None, ge=0, le=100)
    final_score: int | None = Field(default=None, ge=0, le=100)
    fit_band: FitBand | None = None
    matched_evidence_ids: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    ai_rationale: str = ""
    ai_confidence: float | None = Field(default=None, ge=0, le=1)
    ai_provider: str = ""
    ai_model: str = ""
    ai_fallback_reason: str = ""
    prompt_version: str = ""
    analyzed_at: datetime | None = None
    ai_error: str = ""
    draft: str = ""
    draft_fact_ids: list[str] = Field(default_factory=list)
    draft_verified: bool = False
    drafted_at: datetime | None = None
    processing_status: ProcessingStatus = ProcessingStatus.NEW


class FetchBatch(StrictModel):
    source: SourceKind
    records: list[SourceRecord]
    next_cursor: str | None = None
    fetched_at: datetime = Field(default_factory=utc_now)
    warnings: list[str] = Field(default_factory=list)


class Lead(StrictModel):
    lead_id: UUID = Field(default_factory=uuid4)
    business_name: str
    official_domain: HttpUrl
    country: str = "Philippines"
    area: str = ""
    industry: str = ""
    seeded_by: str
    seeded_at: datetime = Field(default_factory=utc_now)
    verification_source_url: HttpUrl
    verified_at: datetime | None = None
    registry_id: str = ""
    contact_channel: str = ""
    public_contact: str = ""
    contact_person: str = ""
    contact_role: str = ""
    contact_provenance: str = ""
    contains_personal_data: bool = False
    observed_problem: str = ""
    evidence_url: HttpUrl | None = None
    service_hypothesis: str = ""
    ai_relevance_score: int | None = Field(default=None, ge=0, le=100)
    ai_rationale: str = ""
    ai_confidence: float | None = Field(default=None, ge=0, le=1)
    ai_model: str = ""
    prompt_version: str = ""
    analyzed_at: datetime | None = None
    lawful_basis_status: str = "Not assessed"
    lia_reference: str = ""
    privacy_notice_needed: bool = False
    opted_out: bool = False
    suppressed_at: datetime | None = None
    retention_review_at: datetime | None = None
    draft: str = ""
    draft_fact_ids: list[str] = Field(default_factory=list)
    review_status: ReviewStatus = ReviewStatus.NEW
    updated_at: datetime = Field(default_factory=utc_now)


class ListingAvailabilityUpdate(StrictModel):
    record_id: str
    status: ListingAvailabilityStatus
    checked_at: datetime = Field(default_factory=utc_now)
    reason: str = Field(max_length=300)
    http_status: int | None = Field(default=None, ge=100, le=599)
    archive: bool = False

    @model_validator(mode="after")
    def archive_requires_definitive_status(self) -> ListingAvailabilityUpdate:
        if self.archive and self.status not in {
            ListingAvailabilityStatus.UNAVAILABLE,
            ListingAvailabilityStatus.EXPIRED,
        }:
            raise ValueError("Only unavailable or expired listings may be archived")
        return self


class RunResult(StrictModel):
    run_id: UUID = Field(default_factory=uuid4)
    image_digest: str = "local"
    trigger: str = "manual"
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    sources_attempted: int = 0
    sources_succeeded: int = 0
    records_fetched: int = 0
    records_deduplicated: int = 0
    records_actionable: int = 0
    records_needing_review: int = 0
    records_excluded: int = 0
    records_written: int = 0
    records_quarantined: int = 0
    ai_input_tokens: int = 0
    ai_output_tokens: int = 0
    ai_provider_counts: dict[str, int] = Field(default_factory=dict)
    ai_fallback_count: int = 0
    retry_count: int = 0
    status: str = "running"
    errors: list[str] = Field(default_factory=list)
    checkpoints: dict[str, str] = Field(default_factory=dict)
    availability_updates: list[ListingAvailabilityUpdate] = Field(
        default_factory=list,
        exclude=True,
    )


class WorkbookSnapshot(StrictModel):
    schema_version: str
    captured_at: datetime = Field(default_factory=utc_now)
    spreadsheet_id: str
    tabs: dict[str, list[list[Any]]]


class AnalysisQuotaState(StrictModel):
    month: str
    day: str
    daily_rows: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from jobhunt.models import (
    AnalysisConfig,
    CandidateProfile,
    LocationMode,
    LocationPolicy,
    ProfileClaim,
    SearchPreferences,
    SourceKind,
    SourceManifest,
)


class ConfigDiagnostic(BaseModel):
    level: str
    code: str
    message: str


class ConfigurationError(ValueError):
    """Raised when checked-in or runtime configuration is unsafe or invalid."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Unable to read valid JSON from {path}: {exc}") from exc


def _parse[ModelT: BaseModel](model: type[ModelT], value: Any, path: Path) -> ModelT:
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise ConfigurationError(f"Invalid configuration in {path}: {exc}") from exc


def load_source_manifests(config_dir: Path) -> dict[SourceKind, SourceManifest]:
    path = config_dir / "sources.json"
    raw = _read_json(path)
    if not isinstance(raw, list):
        raise ConfigurationError(f"{path} must contain a JSON list")
    manifests: dict[SourceKind, SourceManifest] = {}
    for value in raw:
        manifest = _parse(SourceManifest, value, path)
        if manifest.adapter_id in manifests:
            raise ConfigurationError(f"Duplicate adapter_id {manifest.adapter_id} in {path}")
        manifests[manifest.adapter_id] = manifest
    return manifests


def load_profile_claims(config_dir: Path, *, verified_only: bool = True) -> list[ProfileClaim]:
    path = config_dir / "profile_claims.json"
    raw = _read_json(path)
    if not isinstance(raw, list):
        raise ConfigurationError(f"{path} must contain a JSON list")
    claims = [_parse(ProfileClaim, value, path) for value in raw]
    claim_ids = [claim.claim_id for claim in claims]
    if len(claim_ids) != len(set(claim_ids)):
        raise ConfigurationError(f"Duplicate claim_id in {path}")
    return [claim for claim in claims if claim.verified] if verified_only else claims


def load_location_policy(config_dir: Path) -> LocationPolicy:
    path = config_dir / "location_bands.json"
    return _parse(LocationPolicy, _read_json(path), path)


def load_search_preferences(config_dir: Path) -> SearchPreferences:
    path = config_dir / "preferences.json"
    return _parse(SearchPreferences, _read_json(path), path)


def load_candidate_profile(config_dir: Path) -> CandidateProfile:
    path = config_dir / "candidate_profile.json"
    return _parse(CandidateProfile, _read_json(path), path)


def load_analysis_config(config_dir: Path) -> AnalysisConfig:
    path = config_dir / "analysis.json"
    return _parse(AnalysisConfig, _read_json(path), path)


def validate_all(config_dir: Path) -> list[ConfigDiagnostic]:
    manifests = load_source_manifests(config_dir)
    claims = load_profile_claims(config_dir, verified_only=False)
    policy = load_location_policy(config_dir)
    load_search_preferences(config_dir)
    load_candidate_profile(config_dir)
    load_analysis_config(config_dir)
    diagnostics: list[ConfigDiagnostic] = []
    if not any(manifest.enabled for manifest in manifests.values()):
        diagnostics.append(
            ConfigDiagnostic(
                level="warning",
                code="sources_disabled",
                message="All source adapters are disabled; only fixtures/manual input can run",
            )
        )
    if not any(claim.verified for claim in claims):
        diagnostics.append(
            ConfigDiagnostic(
                level="warning",
                code="claims_unverified",
                message="No profile claims are verified; AI drafting will fail closed",
            )
        )
    if not policy.under_one_hour_areas and not policy.one_to_two_hour_areas:
        level = "info" if policy.mode is LocationMode.REMOTE_FIRST else "warning"
        message = (
            "Commute bands are intentionally unused in remote-first mode"
            if level == "info"
            else "Commute bands are empty; onsite and hybrid records require manual review"
        )
        diagnostics.append(
            ConfigDiagnostic(level=level, code="commute_bands_empty", message=message)
        )
    return diagnostics

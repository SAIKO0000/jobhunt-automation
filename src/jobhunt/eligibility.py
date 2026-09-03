from __future__ import annotations

from dataclasses import dataclass

from jobhunt.models import (
    EligibilityDecision,
    LocationDecision,
    LocationMode,
    LocationPolicy,
    QualificationDecision,
    SourceRecord,
    WorkArrangement,
)


@dataclass(frozen=True)
class EligibilityResult:
    location_band: str
    location_decision: LocationDecision
    eligibility_decision: EligibilityDecision
    hard_fail_reasons: tuple[str, ...] = ()


def combine_eligibility(
    location: EligibilityDecision, qualification: QualificationDecision
) -> EligibilityDecision:
    """Combine independent gates; a hard failure wins, then manual review."""
    if (
        location is EligibilityDecision.INELIGIBLE
        or qualification is QualificationDecision.INELIGIBLE
    ):
        return EligibilityDecision.INELIGIBLE
    if (
        location is EligibilityDecision.MANUAL_REVIEW
        or qualification is QualificationDecision.MANUAL_REVIEW
    ):
        return EligibilityDecision.MANUAL_REVIEW
    return EligibilityDecision.ELIGIBLE


def evaluate_eligibility(record: SourceRecord, policy: LocationPolicy) -> EligibilityResult:
    if not record.is_listed:
        return EligibilityResult(
            "closed/unlisted",
            LocationDecision.INELIGIBLE,
            EligibilityDecision.INELIGIBLE,
            ("Listing is not publicly listed",),
        )

    arrangement = record.work_arrangement
    country = record.country.casefold().strip()
    home_country = policy.home_country.casefold().strip()
    location = record.location_text.casefold().strip()
    is_overseas = bool(country) and country != home_country

    if arrangement is WorkArrangement.REMOTE:
        restriction = _remote_restriction_decision(
            record.remote_location_restrictions, policy.home_country
        )
        if restriction is LocationDecision.INELIGIBLE:
            return EligibilityResult(
                "Remote - location restricted",
                LocationDecision.INELIGIBLE,
                EligibilityDecision.INELIGIBLE,
                ("Remote listing does not include the Philippines or an applicable region",),
            )
        if restriction is LocationDecision.MANUAL_REVIEW:
            return EligibilityResult(
                "Remote - restriction unclear",
                LocationDecision.MANUAL_REVIEW,
                EligibilityDecision.MANUAL_REVIEW,
            )
        band = "Overseas remote" if is_overseas else "Remote"
        return EligibilityResult(
            band,
            LocationDecision.ELIGIBLE,
            EligibilityDecision.ELIGIBLE,
        )

    if is_overseas:
        return EligibilityResult(
            "Overseas non-remote",
            LocationDecision.INELIGIBLE,
            EligibilityDecision.INELIGIBLE,
            ("Overseas opportunities must be explicitly remote",),
        )

    if arrangement is WorkArrangement.UNKNOWN:
        return EligibilityResult(
            "Unknown",
            LocationDecision.MANUAL_REVIEW,
            EligibilityDecision.MANUAL_REVIEW,
        )

    if policy.mode is LocationMode.REMOTE_FIRST:
        return EligibilityResult(
            "Remote-first manual review",
            LocationDecision.MANUAL_REVIEW,
            EligibilityDecision.MANUAL_REVIEW,
        )

    under_one = _matches(location, policy.under_one_hour_areas)
    one_to_two = _matches(location, policy.one_to_two_hour_areas)
    bands_empty = not policy.under_one_hour_areas and not policy.one_to_two_hour_areas

    if arrangement is WorkArrangement.ONSITE:
        if under_one:
            return EligibilityResult(
                "User-defined <1 hour",
                LocationDecision.ELIGIBLE,
                EligibilityDecision.ELIGIBLE,
            )
        if one_to_two:
            return EligibilityResult(
                "User-defined 1-2 hours",
                LocationDecision.INELIGIBLE,
                EligibilityDecision.INELIGIBLE,
                ("Onsite area is outside the user-defined <1 hour band",),
            )
        return EligibilityResult(
            "Unconfigured" if bands_empty else "Unmatched",
            LocationDecision.MANUAL_REVIEW,
            EligibilityDecision.MANUAL_REVIEW,
        )

    if arrangement is WorkArrangement.HYBRID:
        if under_one:
            band = "User-defined <1 hour"
        elif one_to_two:
            band = "User-defined 1-2 hours"
        else:
            return EligibilityResult(
                "Unconfigured" if bands_empty else "Unmatched",
                LocationDecision.MANUAL_REVIEW,
                EligibilityDecision.MANUAL_REVIEW,
            )
        return EligibilityResult(
            band,
            LocationDecision.ELIGIBLE,
            EligibilityDecision.ELIGIBLE,
        )

    return EligibilityResult(
        "Unknown",
        LocationDecision.MANUAL_REVIEW,
        EligibilityDecision.MANUAL_REVIEW,
    )


def _matches(location: str, areas: list[str]) -> bool:
    return any(area in location for area in areas)


def _remote_restriction_decision(restrictions: list[str], home_country: str) -> LocationDecision:
    if not restrictions:
        return LocationDecision.ELIGIBLE
    text = " ".join(restrictions).casefold()
    allowed_markers = (
        home_country.casefold(),
        "philippines",
        "worldwide",
        "anywhere",
        "global",
        "apac",
        "asia pacific",
        "asia-pacific",
        "southeast asia",
        "south-east asia",
        "asia",
    )
    if any(marker in text for marker in allowed_markers):
        return LocationDecision.ELIGIBLE
    known_exclusions = (
        "united states",
        "usa",
        "canada",
        "europe",
        "emea",
        "latin america",
        "latam",
        "australia",
        "new zealand",
        "united kingdom",
        "uk only",
    )
    if any(marker in text for marker in known_exclusions):
        return LocationDecision.INELIGIBLE
    return LocationDecision.MANUAL_REVIEW

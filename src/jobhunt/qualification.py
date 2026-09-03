from __future__ import annotations

import re
from collections.abc import Iterable

from jobhunt.models import (
    ApplicantCostDecision,
    CandidateProfile,
    CompensationDecision,
    QualificationAssessment,
    QualificationDecision,
    SeniorityLevel,
    SourceRecord,
)

_ROLE_NOUN = (
    r"(?:architect|consultant|designer|developer|engineer|manager|specialist|analyst|"
    r"administrator|coordinator|programmer|assistant|accountant|recruiter|writer|editor|"
    r"owner|master)"
)
_ROLE_QUALIFIER = (
    r"(?:software|frontend|front[ -]end|backend|back[ -]end|full[ -]?stack|web|mobile|"
    r"data|devops|cloud|qa|test|automation|systems?|technical|product|project|program|"
    r"operations?|engineering|platform|security|solutions?|application|virtual|executive|"
    r"python|java|javascript|typescript|react|node|php|ruby|rails|golang|android|ios|it|"
    r"digital|marketing|customer|client|business)"
)

_SENIORITY_PATTERNS: tuple[tuple[SeniorityLevel, re.Pattern[str]], ...] = (
    (
        SeniorityLevel.EXECUTIVE,
        re.compile(
            r"\b(?:chief\s+(?:executive|technology|information|operating|product)\s+officer|"
            r"c[etipo]o|vice president|vp\s+(?:of\s+)?|executive\s+(?:director|manager))\b",
            re.IGNORECASE,
        ),
    ),
    (
        SeniorityLevel.DIRECTOR,
        re.compile(r"\b(?:director\s+of|director|head\s+of)\b", re.IGNORECASE),
    ),
    (
        SeniorityLevel.PRINCIPAL,
        re.compile(
            rf"\bprincipal\s+(?:(?:{_ROLE_QUALIFIER})\s+){{0,3}}{_ROLE_NOUN}\b",
            re.IGNORECASE,
        ),
    ),
    (
        SeniorityLevel.STAFF,
        re.compile(
            rf"\bstaff\s+(?:(?:{_ROLE_QUALIFIER})\s+){{0,3}}{_ROLE_NOUN}\b",
            re.IGNORECASE,
        ),
    ),
    (
        SeniorityLevel.SENIOR,
        re.compile(
            rf"\b(?:senior|sr\.?)\s+(?:(?:{_ROLE_QUALIFIER})\s+){{0,3}}{_ROLE_NOUN}\b",
            re.IGNORECASE,
        ),
    ),
    (
        SeniorityLevel.LEAD,
        re.compile(
            rf"(?:\b(?:team|technical|engineering|software|development|project)\s+lead\b|"
            rf"\blead\s+(?:(?:{_ROLE_QUALIFIER})\s+){{0,3}}{_ROLE_NOUN}\b)",
            re.IGNORECASE,
        ),
    ),
    (
        SeniorityLevel.MANAGER,
        re.compile(
            rf"(?:\b(?:(?:{_ROLE_QUALIFIER})\s+){{0,3}}manager\b|\bmanager\s+of\b)",
            re.IGNORECASE,
        ),
    ),
    (SeniorityLevel.JUNIOR, re.compile(r"\b(?:junior|jr\.?)\b", re.IGNORECASE)),
    (
        SeniorityLevel.ENTRY,
        re.compile(
            r"\b(?:entry[ -]level|graduate|new grad(?:uate)?|trainee|apprentice)\b",
            re.IGNORECASE,
        ),
    ),
    (SeniorityLevel.INTERNSHIP, re.compile(r"\b(?:intern|internship)\b", re.IGNORECASE)),
    (SeniorityLevel.ASSOCIATE, re.compile(r"\bassociate\b", re.IGNORECASE)),
    (
        SeniorityLevel.MID,
        re.compile(r"\b(?:mid[ -]level|midweight|intermediate)\b", re.IGNORECASE),
    ),
)

_YEAR_VALUE_RE = r"(?P<years>\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten)"
_MANDATORY_EXPERIENCE_PATTERNS = (
    re.compile(
        rf"\b(?:at least|minimum(?:\s+of)?|min\.?)\s+{_YEAR_VALUE_RE}\+?\s*"
        rf"(?:years?|yrs?)(?:\s+of)?(?:\s+[\w/-]+){{0,3}}\s+experience\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_YEAR_VALUE_RE}\s*(?:-|\u2013|\u2014|to)\s*"
        rf"(?:\d+(?:\.\d+)?|one|two|three|four|five|"
        rf"six|seven|eight|nine|ten)\s*(?:years?|yrs?)(?:\s+of)?"
        rf"(?:\s+[\w/-]+){{0,3}}\s+experience\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{_YEAR_VALUE_RE}\+?\s*(?:years?|yrs?)(?:\s+of)?"
        rf"(?:\s+[\w/-]+){{0,3}}\s+experience\b",
        re.IGNORECASE,
    ),
)
_PREFERRED_MARKERS = re.compile(
    r"\b(?:preferred|ideally|nice[ -]to[ -]have|bonus|a plus|desired|advantageous)\b",
    re.IGNORECASE,
)
_EMPLOYER_HISTORY_MARKERS = re.compile(
    r"\b(?:we|our company|our team|the company|the team)\s+(?:has|have|brings?|offers?)\b",
    re.IGNORECASE,
)
_UPPER_BOUND_MARKERS = re.compile(r"\b(?:up to|no more than|maximum(?:\s+of)?)\b", re.IGNORECASE)
_AMBIGUOUS_EXPERIENCE = re.compile(
    r"\b(?:(?:several|multiple|few)\s+years?(?:\s+of)?\s+experience|"
    r"extensive\s+experience|significant\s+experience|experience\s+(?:is\s+)?required)\b",
    re.IGNORECASE,
)

_NO_FEE = re.compile(
    r"\b(?:no|never)\s+(?:application|registration|recruitment|training|placement)\s+fees?\b|"
    r"\b(?:do not|does not|won't|will not|never)\s+charge\s+"
    r"(?:applicants|candidates|you)(?:\s+(?:any\s+)?"
    r"(?:application|registration|recruitment|training|placement)\s+fees?)?\b|"
    r"\bfree\s+to\s+apply\b",
    re.IGNORECASE,
)
_PAYMENT_REQUIRED = re.compile(
    r"\b(?:pay|payment of|purchase)\b.{0,40}\b(?:to apply|before (?:applying|starting)|"
    r"application|registration|training|equipment|starter kit)\b|"
    r"\b(?:application|registration|recruitment|training|placement)\s+fees?\b|"
    r"\b(?:refundable\s+)?(?:equipment|security)\s+deposit\b",
    re.IGNORECASE,
)
_AMBIGUOUS_COST = re.compile(
    r"\b(?:fees?|costs?|deposit|purchase)\s+(?:may|might|could)\s+(?:apply|be required)\b|"
    r"\bown\s+(?:equipment|computer|laptop)\s+(?:is\s+)?required\b",
    re.IGNORECASE,
)
_UNPAID = re.compile(
    r"\bunpaid\s+(?:role|position|internship|work|opportunity)\b|"
    r"\bvolunteer\s+(?:role|position|internship|opportunity)\b",
    re.IGNORECASE,
)
_EXPLICITLY_PAID = re.compile(
    r"\b(?:paid\s+(?:role|position|internship)|competitive\s+(?:salary|compensation)|"
    r"salary\s+range|compensation\s+range)\b",
    re.IGNORECASE,
)

_WORD_NUMBERS = {
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "six": 6.0,
    "seven": 7.0,
    "eight": 8.0,
    "nine": 9.0,
    "ten": 10.0,
}

# Keep extracted values inside the validated domain. Larger values are almost
# certainly a malformed listing, concatenated field, or parser false positive;
# retaining them as a numeric requirement would make migrations fail validation.
_MAX_PLAUSIBLE_EXPERIENCE_YEARS = 80.0


def assess_candidate_qualification(
    record: SourceRecord, profile: CandidateProfile
) -> QualificationAssessment:
    """Apply conservative, explainable qualification rules without model inference."""

    reasons: list[tuple[QualificationDecision, str, str]] = []
    seniority = infer_seniority(record.title)
    if seniority is SeniorityLevel.UNSPECIFIED:
        seniority = _structured_seniority(record.tags)
    if seniority in profile.excluded_seniority_levels:
        reasons.append(
            (
                QualificationDecision.INELIGIBLE,
                "excluded_seniority",
                f"Title explicitly indicates excluded seniority: {seniority.value}",
            )
        )
    elif seniority is SeniorityLevel.UNSPECIFIED and profile.review_unspecified_seniority:
        reasons.append(
            (
                QualificationDecision.MANUAL_REVIEW,
                "seniority_unspecified",
                "The title does not state an approved entry-level seniority",
            )
        )
    elif seniority not in profile.target_seniority_levels:
        reasons.append(
            (
                QualificationDecision.MANUAL_REVIEW,
                "seniority_not_targeted",
                f"Seniority is not an approved target: {seniority.value}",
            )
        )

    text_parts = [record.description_excerpt, *record.requirements]
    experience_years, ambiguous_experience, implausible_experience = _mandatory_experience(
        text_parts
    )
    if implausible_experience:
        reasons.append(
            (
                QualificationDecision.MANUAL_REVIEW,
                "experience_requirement_implausible",
                "A possible experience requirement exceeded the supported 80-year range; "
                "the extracted value was discarded",
            )
        )
    if experience_years is not None and experience_years > 0:
        verified_maximum = profile.maximum_verified_professional_experience_years
        if experience_years > profile.maximum_target_experience_years:
            reasons.append(
                (
                    QualificationDecision.INELIGIBLE,
                    "experience_requirement_exceeds_target",
                    f"Listing requires at least {experience_years:g} years; the configured "
                    f"early-career search cap is {profile.maximum_target_experience_years:g}",
                )
            )
        elif verified_maximum is None:
            reasons.append(
                (
                    QualificationDecision.MANUAL_REVIEW,
                    "experience_profile_unverified",
                    f"Listing requires at least {experience_years:g} years, but the profile "
                    "maximum "
                    "is not verified",
                )
            )
        elif experience_years > verified_maximum:
            reasons.append(
                (
                    QualificationDecision.INELIGIBLE,
                    "experience_requirement_exceeds_profile",
                    f"Listing requires at least {experience_years:g} years; verified profile "
                    f"maximum is {verified_maximum:g}",
                )
            )
    elif ambiguous_experience:
        reasons.append(
            (
                QualificationDecision.MANUAL_REVIEW,
                "experience_requirement_ambiguous",
                "The listing requires experience but does not provide a reliably comparable "
                "minimum",
            )
        )

    combined_text = "\n".join(part for part in [record.title, *text_parts] if part)
    applicant_cost = _applicant_cost(record.applicant_cost, combined_text)
    if applicant_cost is ApplicantCostDecision.PAYMENT_REQUIRED:
        reasons.append(
            (
                QualificationDecision.INELIGIBLE,
                "applicant_payment_required",
                "The listing or source requires the applicant to pay",
            )
        )
    elif applicant_cost is ApplicantCostDecision.AMBIGUOUS:
        reasons.append(
            (
                QualificationDecision.MANUAL_REVIEW,
                "applicant_cost_ambiguous",
                "Potential applicant cost requires manual verification",
            )
        )

    compensation = _compensation(record, combined_text)
    if compensation is CompensationDecision.UNPAID and not profile.allow_unpaid_roles:
        reasons.append(
            (
                QualificationDecision.INELIGIBLE,
                "unpaid_role",
                "The opportunity is explicitly unpaid",
            )
        )
    elif compensation is CompensationDecision.NOT_STATED and profile.require_explicit_compensation:
        reasons.append(
            (
                QualificationDecision.MANUAL_REVIEW,
                "compensation_not_stated",
                "Compensation is not stated",
            )
        )

    if not record.description_excerpt and not record.requirements:
        reasons.append(
            (
                QualificationDecision.MANUAL_REVIEW,
                "qualification_details_missing",
                "No description or structured requirements are available",
            )
        )

    decision = _combined_decision(item[0] for item in reasons)
    return QualificationAssessment(
        decision=decision,
        seniority_level=seniority,
        mandatory_experience_years=experience_years,
        applicant_cost_decision=applicant_cost,
        compensation_decision=compensation,
        reason_codes=[item[1] for item in reasons],
        reasons=[item[2] for item in reasons],
    )


def infer_seniority(title: str) -> SeniorityLevel:
    normalized = " ".join(title.split())
    for level, pattern in _SENIORITY_PATTERNS:
        if pattern.search(normalized):
            return level
    return SeniorityLevel.UNSPECIFIED


def _structured_seniority(tags: Iterable[str]) -> SeniorityLevel:
    aliases = {
        "intern": SeniorityLevel.INTERNSHIP,
        "internship": SeniorityLevel.INTERNSHIP,
        "entry": SeniorityLevel.ENTRY,
        "entry level": SeniorityLevel.ENTRY,
        "entry-level": SeniorityLevel.ENTRY,
        "junior": SeniorityLevel.JUNIOR,
        "associate": SeniorityLevel.ASSOCIATE,
        "mid": SeniorityLevel.MID,
        "mid level": SeniorityLevel.MID,
        "mid-level": SeniorityLevel.MID,
        "senior": SeniorityLevel.SENIOR,
        "senior level": SeniorityLevel.SENIOR,
        "senior-level": SeniorityLevel.SENIOR,
        "staff": SeniorityLevel.STAFF,
        "principal": SeniorityLevel.PRINCIPAL,
        "lead": SeniorityLevel.LEAD,
        "manager": SeniorityLevel.MANAGER,
        "director": SeniorityLevel.DIRECTOR,
        "executive": SeniorityLevel.EXECUTIVE,
    }
    for tag in tags:
        normalized = " ".join(tag.casefold().replace("_", " ").split())
        if normalized in aliases:
            return aliases[normalized]
    return SeniorityLevel.UNSPECIFIED


def _mandatory_experience(parts: Iterable[str]) -> tuple[float | None, bool, bool]:
    years: list[float] = []
    ambiguous = False
    implausible = False
    for part in parts:
        for segment in re.split(r"[\n\r;.!?]+", part):
            if not segment.strip() or _PREFERRED_MARKERS.search(segment):
                continue
            if _EMPLOYER_HISTORY_MARKERS.search(segment) or _UPPER_BOUND_MARKERS.search(segment):
                continue
            matches: list[re.Match[str]] = []
            occupied: list[tuple[int, int]] = []
            for pattern in _MANDATORY_EXPERIENCE_PATTERNS:
                for match in pattern.finditer(segment):
                    if any(match.start() < end and start < match.end() for start, end in occupied):
                        continue
                    matches.append(match)
                    occupied.append(match.span())
            for match in matches:
                parsed = _parse_years(match.group("years"))
                if parsed <= _MAX_PLAUSIBLE_EXPERIENCE_YEARS:
                    years.append(parsed)
                else:
                    implausible = True
            if not matches and _AMBIGUOUS_EXPERIENCE.search(segment):
                ambiguous = True
    return (max(years) if years else None), ambiguous, implausible


def _parse_years(value: str) -> float:
    return _WORD_NUMBERS.get(value.casefold(), float(value) if value[0].isdigit() else 0.0)


def _applicant_cost(explicit: ApplicantCostDecision, text: str) -> ApplicantCostDecision:
    if explicit in {
        ApplicantCostDecision.PAYMENT_REQUIRED,
        ApplicantCostDecision.AMBIGUOUS,
    }:
        return explicit
    explicit_no_fee = bool(_NO_FEE.search(text))
    cost_text = _NO_FEE.sub("", text)
    if _PAYMENT_REQUIRED.search(cost_text):
        return ApplicantCostDecision.PAYMENT_REQUIRED
    if _AMBIGUOUS_COST.search(cost_text):
        return ApplicantCostDecision.AMBIGUOUS
    if explicit is ApplicantCostDecision.FREE_TO_APPLY or explicit_no_fee:
        return ApplicantCostDecision.FREE_TO_APPLY
    return explicit


def _compensation(record: SourceRecord, text: str) -> CompensationDecision:
    if _UNPAID.search(text):
        return CompensationDecision.UNPAID
    if (
        record.salary_raw
        or record.salary_min is not None
        or record.salary_max is not None
        or _EXPLICITLY_PAID.search(text)
    ):
        return CompensationDecision.PAID
    return CompensationDecision.NOT_STATED


def _combined_decision(decisions: Iterable[QualificationDecision]) -> QualificationDecision:
    values = set(decisions)
    if QualificationDecision.INELIGIBLE in values:
        return QualificationDecision.INELIGIBLE
    if QualificationDecision.MANUAL_REVIEW in values:
        return QualificationDecision.MANUAL_REVIEW
    return QualificationDecision.ELIGIBLE

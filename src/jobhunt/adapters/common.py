from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from jobhunt.models import (
    OpportunityType,
    RetentionClass,
    SourceManifest,
    WorkArrangement,
)
from jobhunt.security import html_to_text


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1_000
        return datetime.fromtimestamp(seconds, tz=UTC)
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            return None


def infer_work_arrangement(*values: str) -> WorkArrangement:
    text = " ".join(values).casefold()
    if re.search(r"\bhybrid\b", text):
        return WorkArrangement.HYBRID
    if re.search(r"\b(remote|work from home|wfh|distributed)\b", text):
        return WorkArrangement.REMOTE
    if re.search(r"\b(on[ -]?site|office-based|in office)\b", text):
        return WorkArrangement.ONSITE
    return WorkArrangement.UNKNOWN


def infer_country(location: str) -> str:
    lowered = location.casefold()
    if "philippines" in lowered or re.search(r"\bph\b", lowered):
        return "Philippines"
    known = {
        "united states": "United States",
        "usa": "United States",
        "canada": "Canada",
        "australia": "Australia",
        "singapore": "Singapore",
        "united kingdom": "United Kingdom",
        "uk": "United Kingdom",
        "japan": "Japan",
    }
    for marker, country in known.items():
        if marker in lowered:
            return country
    return ""


def infer_opportunity_type(title: str, tags: list[str]) -> OpportunityType:
    text = " ".join([title, *tags]).casefold()
    va_terms = (
        "virtual assistant",
        "executive assistant",
        "administrative assistant",
        "admin assistant",
        "operations assistant",
        "project assistant",
        "technical assistant",
        "sales assistant",
        "marketing assistant",
        "social media assistant",
        "accounting assistant",
        "bookkeeping assistant",
        "bookkeeper",
        "medical assistant",
        "medical biller",
        "medical billing",
        "patient coordinator",
        "appointment setter",
        "data entry",
        "personal assistant",
        "operations coordinator",
        "project coordinator",
        "technical operations",
        "freelance",
    )
    flexible_va_patterns = (
        re.compile(r"\bvirtual\b.*\bassistant\b"),
        re.compile(r"\bassistant\b.*\bvirtual\b"),
    )
    return (
        OpportunityType.VA_FREELANCE
        if any(term in text for term in va_terms)
        or any(pattern.search(text) for pattern in flexible_va_patterns)
        else OpportunityType.TECHNICAL
    )


def permitted_excerpt(value: str, manifest: SourceManifest) -> str:
    if manifest.retention_class is RetentionClass.METADATA_ONLY:
        return ""
    return html_to_text(value, limit=4_000)


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,;|]", value) if item.strip()]
    return [str(value).strip()]

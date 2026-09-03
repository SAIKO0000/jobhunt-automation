from __future__ import annotations

from typing import Any

from jobhunt.adapters.base import SourceAdapter
from jobhunt.adapters.common import (
    infer_country,
    infer_opportunity_type,
    parse_datetime,
    permitted_excerpt,
    string_list,
)
from jobhunt.models import (
    FetchBatch,
    SourceKind,
    SourceManifest,
    SourceRecord,
    WorkArrangement,
)


class HimalayasAdapter(SourceAdapter):
    source = SourceKind.HIMALAYAS

    def parse_payload(self, payload: Any, manifest: SourceManifest) -> FetchBatch:
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            raise ValueError("Himalayas payload must contain a jobs list")

        records: list[SourceRecord] = []
        warnings: list[str] = []
        for item in jobs:
            if not isinstance(item, dict):
                warnings.append("Himalayas record skipped: record must be an object")
                continue
            try:
                source_record_id = str(item["guid"]).strip()
                source_url = str(item["applicationLink"]).strip()
                if not source_record_id:
                    raise ValueError("missing guid")
                if not source_url:
                    raise ValueError("missing applicationLink")

                categories = string_list(item.get("categories"))
                seniority = string_list(item.get("seniority"))
                tags = [*categories, *string_list(item.get("parentCategories")), *seniority]
                location_restrictions = _location_restrictions(item.get("locationRestrictions"))
                location = _location_text(location_restrictions)
                employment_type = str(item.get("employmentType") or "")
                title = str(item.get("title") or "Untitled role")
                salary_min = _float_or_none(item.get("minSalary"))
                salary_max = _float_or_none(item.get("maxSalary"))
                currency = str(item.get("currency") or "")
                salary_period = str(item.get("salaryPeriod") or "")

                records.append(
                    SourceRecord(
                        source=self.source,
                        source_record_id=source_record_id,
                        source_url=source_url,
                        apply_url=source_url,
                        company=str(item.get("companyName") or "Unknown company"),
                        title=title,
                        location_text=location,
                        country=infer_country(location),
                        remote_location_restrictions=location_restrictions,
                        work_arrangement=WorkArrangement.REMOTE,
                        opportunity_type=infer_opportunity_type(title, tags),
                        employment_type=employment_type,
                        engagement_type=employment_type,
                        salary_raw=_salary_text(salary_min, salary_max, currency, salary_period),
                        currency=currency,
                        salary_min=salary_min,
                        salary_max=salary_max,
                        description_excerpt=permitted_excerpt(
                            str(item.get("description") or item.get("excerpt") or ""), manifest
                        ),
                        tags=tags,
                        published_at=parse_datetime(item.get("pubDate")),
                        expires_at=parse_datetime(item.get("expiryDate")),
                        attribution=manifest.attribution,
                        retention_class=manifest.retention_class,
                        ai_processing_allowed=manifest.ai_processing_allowed,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                warnings.append(f"Himalayas record skipped: {exc}")

        raw_cursor = payload.get("nextCursor")
        next_cursor = raw_cursor if isinstance(raw_cursor, str) and raw_cursor else None
        if raw_cursor not in (None, "") and next_cursor is None:
            warnings.append("Himalayas cursor ignored: nextCursor must be a string")
        return FetchBatch(
            source=self.source,
            records=records,
            next_cursor=next_cursor,
            warnings=warnings,
        )


def _location_restrictions(value: Any) -> list[str]:
    names: list[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if name:
                    names.append(name)
    return names


def _location_text(names: list[str]) -> str:
    return f"Remote - {', '.join(names)}" if names else "Remote - Worldwide"


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _salary_text(minimum: float | None, maximum: float | None, currency: str, period: str) -> str:
    if minimum is None and maximum is None:
        return ""
    bounds = (
        f"{minimum:g}-{maximum:g}"
        if minimum is not None and maximum is not None
        else f"{minimum if minimum is not None else maximum:g}"
    )
    return " ".join(part for part in (currency, bounds, period) if part)

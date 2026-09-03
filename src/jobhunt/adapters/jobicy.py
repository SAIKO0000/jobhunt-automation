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


class JobicyAdapter(SourceAdapter):
    source = SourceKind.JOBICY

    def parse_payload(self, payload: Any, manifest: SourceManifest) -> FetchBatch:
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            raise ValueError("Jobicy payload must contain a jobs list")

        records: list[SourceRecord] = []
        warnings: list[str] = []
        for item in jobs:
            if not isinstance(item, dict):
                warnings.append("Jobicy record skipped: record must be an object")
                continue
            try:
                source_record_id = str(item["id"]).strip()
                source_url = str(item["url"]).strip()
                if not source_record_id:
                    raise ValueError("missing id")
                if not source_url:
                    raise ValueError("missing URL")

                employment_types = string_list(item.get("jobType"))
                level = str(item.get("jobLevel") or "").strip()
                tags = [*string_list(item.get("jobIndustry")), *([level] if level else [])]
                location = str(item.get("jobGeo") or "Anywhere").strip()
                location_restrictions = (
                    [] if location.casefold() in {"anywhere", "worldwide", "global"} else [location]
                )
                title = str(item.get("jobTitle") or "Untitled role")
                salary_min = _float_or_none(item.get("salaryMin"))
                salary_max = _float_or_none(item.get("salaryMax"))
                currency = str(item.get("salaryCurrency") or "")
                salary_period = str(item.get("salaryPeriod") or "")
                employment_type = ", ".join(employment_types)

                records.append(
                    SourceRecord(
                        source=self.source,
                        source_record_id=source_record_id,
                        source_url=source_url,
                        apply_url=source_url,
                        company=str(item.get("companyName") or "Unknown company"),
                        title=title,
                        location_text=f"Remote - {location}",
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
                            str(item.get("jobDescription") or item.get("jobExcerpt") or ""),
                            manifest,
                        ),
                        tags=tags,
                        published_at=parse_datetime(item.get("pubDate")),
                        attribution=manifest.attribution,
                        retention_class=manifest.retention_class,
                        ai_processing_allowed=manifest.ai_processing_allowed,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                warnings.append(f"Jobicy record skipped: {exc}")
        return FetchBatch(source=self.source, records=records, warnings=warnings)


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

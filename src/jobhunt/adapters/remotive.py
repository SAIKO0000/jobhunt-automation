from __future__ import annotations

from typing import Any

from jobhunt.adapters.base import SourceAdapter
from jobhunt.adapters.common import (
    infer_country,
    infer_opportunity_type,
    parse_datetime,
    permitted_excerpt,
)
from jobhunt.models import (
    FetchBatch,
    SourceKind,
    SourceManifest,
    SourceRecord,
    WorkArrangement,
)


class RemotiveAdapter(SourceAdapter):
    source = SourceKind.REMOTIVE

    def parse_payload(self, payload: Any, manifest: SourceManifest) -> FetchBatch:
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            raise ValueError("Remotive payload must contain a jobs list")

        records: list[SourceRecord] = []
        warnings: list[str] = []
        for item in jobs:
            if not isinstance(item, dict):
                warnings.append("Remotive record skipped: record must be an object")
                continue
            try:
                source_record_id = str(item["id"]).strip()
                source_url = str(item["url"]).strip()
                if not source_record_id:
                    raise ValueError("missing id")
                if not source_url:
                    raise ValueError("missing URL")

                category = str(item.get("category") or "").strip()
                job_type = str(item.get("job_type") or "").strip()
                tags = [value for value in (category, job_type) if value]
                location = str(item.get("candidate_required_location") or "Worldwide")
                location_restrictions = (
                    [] if location.casefold() in {"anywhere", "worldwide", "global"} else [location]
                )
                title = str(item.get("title") or "Untitled role")
                records.append(
                    SourceRecord(
                        source=self.source,
                        source_record_id=source_record_id,
                        source_url=source_url,
                        apply_url=source_url,
                        company=str(item.get("company_name") or "Unknown company"),
                        title=title,
                        location_text=f"Remote - {location}",
                        country=infer_country(location),
                        remote_location_restrictions=location_restrictions,
                        work_arrangement=WorkArrangement.REMOTE,
                        opportunity_type=infer_opportunity_type(title, tags),
                        employment_type=job_type,
                        engagement_type=job_type,
                        salary_raw=str(item.get("salary") or ""),
                        description_excerpt=permitted_excerpt(
                            str(item.get("description") or ""), manifest
                        ),
                        tags=tags,
                        published_at=parse_datetime(item.get("publication_date")),
                        attribution=manifest.attribution,
                        retention_class=manifest.retention_class,
                        ai_processing_allowed=manifest.ai_processing_allowed,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                warnings.append(f"Remotive record skipped: {exc}")
        return FetchBatch(source=self.source, records=records, warnings=warnings)

from __future__ import annotations

from typing import Any

from jobhunt.adapters.base import SourceAdapter
from jobhunt.adapters.common import (
    infer_country,
    infer_opportunity_type,
    infer_work_arrangement,
    parse_datetime,
    permitted_excerpt,
    string_list,
)
from jobhunt.models import FetchBatch, SourceKind, SourceManifest, SourceRecord, WorkArrangement


class RemoteOkAdapter(SourceAdapter):
    source = SourceKind.REMOTEOK

    def parse_payload(self, payload: Any, manifest: SourceManifest) -> FetchBatch:
        if not isinstance(payload, list):
            raise ValueError("Remote OK payload must be a list")
        records: list[SourceRecord] = []
        warnings: list[str] = []
        for item in payload:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            try:
                tags = string_list(item.get("tags"))
                location = str(item.get("location") or "Remote")
                source_url = str(item.get("url") or item.get("apply_url") or "")
                if not source_url:
                    raise ValueError("missing source URL")
                records.append(
                    SourceRecord(
                        source=self.source,
                        source_record_id=str(item["id"]),
                        source_url=source_url,
                        apply_url=item.get("apply_url") or source_url,
                        company=str(item.get("company") or "Unknown company"),
                        title=str(item.get("position") or "Untitled role"),
                        location_text=location,
                        country=infer_country(location),
                        work_arrangement=infer_work_arrangement(location, "remote")
                        or WorkArrangement.REMOTE,
                        opportunity_type=infer_opportunity_type(
                            str(item.get("position") or ""), tags
                        ),
                        employment_type="",
                        salary_min=_float_or_none(item.get("salary_min")),
                        salary_max=_float_or_none(item.get("salary_max")),
                        description_excerpt=permitted_excerpt(
                            str(item.get("description") or ""), manifest
                        ),
                        tags=tags,
                        published_at=parse_datetime(item.get("date") or item.get("epoch")),
                        attribution=manifest.attribution,
                        retention_class=manifest.retention_class,
                        ai_processing_allowed=manifest.ai_processing_allowed,
                    )
                )
            except (TypeError, ValueError) as exc:
                warnings.append(f"Remote OK record skipped: {exc}")
        return FetchBatch(source=self.source, records=records, warnings=warnings)


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None

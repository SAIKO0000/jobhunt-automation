from __future__ import annotations

from typing import Any

from jobhunt.adapters.base import SourceAdapter
from jobhunt.adapters.common import (
    infer_country,
    infer_opportunity_type,
    infer_work_arrangement,
    parse_datetime,
    permitted_excerpt,
)
from jobhunt.models import FetchBatch, SourceKind, SourceManifest, SourceRecord
from jobhunt.security import SafeHttpClient


class GreenhouseAdapter(SourceAdapter):
    source = SourceKind.GREENHOUSE

    def _fetch_with_client(self, client: SafeHttpClient, manifest: SourceManifest) -> FetchBatch:
        records: list[SourceRecord] = []
        warnings: list[str] = []
        for token in manifest.board_tokens:
            endpoint = (manifest.endpoint or "").format(board_token=token)
            batch = self.parse_payload(client.get_json(endpoint), manifest)
            records.extend(batch.records)
            warnings.extend(batch.warnings)
        return FetchBatch(source=self.source, records=records, warnings=warnings)

    def parse_payload(self, payload: Any, manifest: SourceManifest) -> FetchBatch:
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            raise ValueError("Greenhouse payload must contain a jobs list")
        records: list[SourceRecord] = []
        warnings: list[str] = []
        for item in jobs:
            try:
                location = str((item.get("location") or {}).get("name") or "")
                title = str(item.get("title") or "Untitled role")
                url = str(item.get("absolute_url") or "")
                company = _company_from_url(url)
                records.append(
                    SourceRecord(
                        source=self.source,
                        source_record_id=str(item["id"]),
                        source_url=url,
                        apply_url=url,
                        company=company,
                        title=title,
                        location_text=location,
                        country=infer_country(location),
                        work_arrangement=infer_work_arrangement(location, title),
                        opportunity_type=infer_opportunity_type(title, []),
                        description_excerpt=permitted_excerpt(
                            str(item.get("content") or ""), manifest
                        ),
                        published_at=parse_datetime(item.get("updated_at")),
                        attribution=manifest.attribution,
                        retention_class=manifest.retention_class,
                        ai_processing_allowed=manifest.ai_processing_allowed,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                warnings.append(f"Greenhouse record skipped: {exc}")
        return FetchBatch(source=self.source, records=records, warnings=warnings)


def _company_from_url(url: str) -> str:
    parts = [part for part in url.split("/") if part]
    try:
        index = next(i for i, part in enumerate(parts) if part.endswith("greenhouse.io"))
        return parts[index + 1].replace("-", " ").title()
    except (StopIteration, IndexError):
        return "Unknown company"

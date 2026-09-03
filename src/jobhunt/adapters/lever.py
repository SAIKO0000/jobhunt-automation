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


class LeverAdapter(SourceAdapter):
    source = SourceKind.LEVER

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
        if not isinstance(payload, list):
            raise ValueError("Lever payload must be a list")
        records: list[SourceRecord] = []
        warnings: list[str] = []
        for item in payload:
            try:
                categories = item.get("categories") or {}
                location = str(categories.get("location") or "")
                title = str(item.get("text") or "Untitled role")
                hosted_url = str(item.get("hostedUrl") or "")
                company = _company_from_url(hosted_url)
                records.append(
                    SourceRecord(
                        source=self.source,
                        source_record_id=str(item["id"]),
                        source_url=hosted_url,
                        apply_url=item.get("applyUrl") or hosted_url,
                        company=company,
                        title=title,
                        location_text=location,
                        country=infer_country(location),
                        work_arrangement=infer_work_arrangement(
                            location, str(categories.get("commitment") or "")
                        ),
                        opportunity_type=infer_opportunity_type(title, []),
                        employment_type=str(categories.get("commitment") or ""),
                        description_excerpt=permitted_excerpt(
                            str(item.get("descriptionPlain") or ""), manifest
                        ),
                        published_at=parse_datetime(item.get("createdAt")),
                        attribution=manifest.attribution,
                        retention_class=manifest.retention_class,
                        ai_processing_allowed=manifest.ai_processing_allowed,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                warnings.append(f"Lever record skipped: {exc}")
        return FetchBatch(source=self.source, records=records, warnings=warnings)


def _company_from_url(url: str) -> str:
    parts = [part for part in url.split("/") if part]
    try:
        index = next(i for i, part in enumerate(parts) if part.endswith("lever.co"))
        return parts[index + 1].replace("-", " ").title()
    except (StopIteration, IndexError):
        return "Unknown company"

from __future__ import annotations

from typing import Any

from defusedxml import ElementTree

from jobhunt.adapters.base import SourceAdapter
from jobhunt.adapters.common import (
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
from jobhunt.security import SafeHttpClient


class WeWorkRemotelyAdapter(SourceAdapter):
    source = SourceKind.WWR

    def _fetch_with_client(self, client: SafeHttpClient, manifest: SourceManifest) -> FetchBatch:
        return self.parse_payload(client.get_text(manifest.endpoint or ""), manifest)

    def parse_payload(self, payload: Any, manifest: SourceManifest) -> FetchBatch:
        if not isinstance(payload, (str, bytes)):
            raise ValueError("WWR payload must be XML text")
        root = ElementTree.fromstring(payload)
        records: list[SourceRecord] = []
        warnings: list[str] = []
        for item in root.findall(".//item"):
            title_text = _text(item, "title") or "Untitled role"
            company, title = _split_title(title_text)
            link = _text(item, "link")
            if not link:
                warnings.append("WWR record skipped: missing link")
                continue
            categories = [node.text.strip() for node in item.findall("category") if node.text]
            description = _text(item, "description")
            records.append(
                SourceRecord(
                    source=self.source,
                    source_record_id=_text(item, "guid") or link,
                    source_url=link,
                    apply_url=link,
                    company=company,
                    title=title,
                    location_text="Remote",
                    work_arrangement=WorkArrangement.REMOTE,
                    opportunity_type=infer_opportunity_type(title, categories),
                    description_excerpt=permitted_excerpt(description, manifest),
                    tags=string_list(categories),
                    published_at=parse_datetime(_text(item, "pubDate")),
                    attribution=manifest.attribution,
                    retention_class=manifest.retention_class,
                    ai_processing_allowed=manifest.ai_processing_allowed,
                )
            )
        return FetchBatch(source=self.source, records=records, warnings=warnings)


def _text(item: Any, name: str) -> str:
    node = item.find(name)
    return (node.text or "").strip() if node is not None else ""


def _split_title(value: str) -> tuple[str, str]:
    if ":" in value:
        company, title = value.split(":", 1)
        return company.strip(), title.strip()
    return "Unknown company", value.strip()

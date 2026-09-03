from __future__ import annotations

from typing import Any

from jobhunt.adapters.base import SourceAdapter
from jobhunt.adapters.common import permitted_excerpt
from jobhunt.models import FetchBatch, SourceKind, SourceManifest, SourceRecord


class ManualAdapter(SourceAdapter):
    source = SourceKind.MANUAL

    def fetch(self, manifest: SourceManifest) -> FetchBatch:
        raise PermissionError("Manual records must be supplied explicitly; they are never fetched")

    def parse_payload(self, payload: Any, manifest: SourceManifest) -> FetchBatch:
        values = payload if isinstance(payload, list) else [payload]
        records: list[SourceRecord] = []
        warnings: list[str] = []
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                warnings.append(f"Manual record {index} skipped: expected an object")
                continue
            try:
                safe = {
                    key: value
                    for key, value in item.items()
                    if key in set(manifest.allowed_fields) | {"source_record_id"}
                }
                safe["description_excerpt"] = permitted_excerpt(
                    str(safe.get("description_excerpt") or ""), manifest
                )
                safe.update(
                    {
                        "source": self.source,
                        "source_record_id": str(safe.get("source_record_id") or f"manual-{index}"),
                        "attribution": manifest.attribution,
                        "retention_class": manifest.retention_class,
                        "ai_processing_allowed": manifest.ai_processing_allowed,
                    }
                )
                records.append(SourceRecord.model_validate(safe))
            except (TypeError, ValueError) as exc:
                warnings.append(f"Manual record {index} skipped: {exc}")
        return FetchBatch(source=self.source, records=records, warnings=warnings)

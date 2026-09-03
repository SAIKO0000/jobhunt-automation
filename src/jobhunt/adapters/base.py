from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from jobhunt.models import FetchBatch, SourceKind, SourceManifest
from jobhunt.security import SafeHttpClient


class SourceAdapter(ABC):
    source: SourceKind

    @abstractmethod
    def parse_payload(self, payload: Any, manifest: SourceManifest) -> FetchBatch:
        """Parse an already-retrieved payload. Used by fixture-first tests."""

    def fetch(self, manifest: SourceManifest) -> FetchBatch:
        if not manifest.enabled or not manifest.owner_approved:
            raise PermissionError(f"Source {manifest.adapter_id} is not enabled and owner-approved")
        if not manifest.fetch_endpoints:
            raise ValueError(f"Source {manifest.adapter_id} has no endpoint")
        with SafeHttpClient(
            allowed_hosts=set(manifest.allowed_hosts),
            max_response_bytes=manifest.max_response_bytes,
            requests_per_minute=manifest.rate_limit_per_minute,
        ) as client:
            return self._fetch_with_client(client, manifest)

    def _fetch_with_client(self, client: SafeHttpClient, manifest: SourceManifest) -> FetchBatch:
        records = []
        warnings: list[str] = []
        succeeded = 0
        for query_number, endpoint in enumerate(manifest.fetch_endpoints, start=1):
            try:
                batch = self.parse_payload(client.get_json(endpoint), manifest)
            except Exception as exc:
                warnings.append(
                    f"{manifest.display_name} query {query_number} failed: "
                    f"{exc.__class__.__name__}: {str(exc)[:200]}"
                )
                continue
            succeeded += 1
            records.extend(batch.records)
            warnings.extend(batch.warnings)
        if succeeded == 0:
            raise RuntimeError(f"All configured queries failed for {manifest.adapter_id.value}")
        return FetchBatch(source=self.source, records=records, warnings=warnings)

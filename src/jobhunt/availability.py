from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import Any

from jobhunt.himalayas_availability import (
    ExactJobCheck,
    ExactJobStatus,
    HimalayasExactVerifier,
)
from jobhunt.models import (
    FetchBatch,
    ListingAvailabilityStatus,
    ListingAvailabilityUpdate,
    SourceKind,
    SourceManifest,
    WorkbookSnapshot,
)
from jobhunt.security import SafeHttpClient
from jobhunt.workbook.schema import OPPORTUNITY_COLUMNS, SYSTEM_EVENT_COLUMNS

MAX_LISTING_PROBES_PER_RUN = 10
_SUPPORTED_DIRECT_CHECKS = {SourceKind.HIMALAYAS, SourceKind.JOBICY}
_AVAILABILITY_EVENT_TYPES = {
    "listing_available_observed",
    "listing_check_inconclusive",
    "listing_unavailable_observed",
    "listing_inactive_archived",
    "listing_expired_archived",
    "himalayas_exact_not_found_observed",
    "himalayas_exact_not_found_archived",
}
_MIN_CONFIRMATION_INTERVAL = timedelta(hours=20)


def review_inbox_availability(
    snapshot: WorkbookSnapshot,
    batches: list[FetchBatch],
    manifests: dict[SourceKind, SourceManifest],
    *,
    now: datetime | None = None,
    max_probes: int = MAX_LISTING_PROBES_PER_RUN,
    probe: Callable[[str, SourceManifest], int] | None = None,
    exact_probe: Callable[[str, SourceManifest], ExactJobCheck] | None = None,
    run_id: str = "",
) -> list[ListingAvailabilityUpdate]:
    """Plan conservative availability updates for existing Inbox listings.

    A filtered API response is never treated as proof that a listing closed.
    Himalayas uses its public exact-job tool, never job-page HEAD requests.
    Two independent negative exact checks are required before archiving.
    """
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    successful_sources = {batch.source for batch in batches}
    current_ids = {
        source: {
            record.source_record_id
            for batch in batches
            if batch.source is source
            for record in batch.records
        }
        for source in successful_sources
    }
    manifest_by_label = {
        manifest.display_name.casefold(): manifest for manifest in manifests.values()
    }
    manifest_by_label.update(
        {manifest.adapter_id.value: manifest for manifest in manifests.values()}
    )
    latest_events = _latest_availability_events(snapshot)
    rows = snapshot.tabs.get("Opportunities", [])
    if len(rows) < 2:
        return []

    updates: list[ListingAvailabilityUpdate] = []
    candidates: list[tuple[str, str, SourceManifest, tuple[str, datetime, str] | None]] = []
    for row in rows[1:]:
        if _cell(row, "Pipeline Stage") != "Inbox":
            continue
        record_id = str(_cell(row, "Record ID") or "")
        source_record_id = str(_cell(row, "Source Record ID") or "")
        manifest = manifest_by_label.get(str(_cell(row, "Source")).casefold())
        if (
            not record_id
            or not source_record_id
            or manifest is None
            or manifest.adapter_id not in _SUPPORTED_DIRECT_CHECKS
            or manifest.adapter_id not in successful_sources
        ):
            continue

        source_deadline = _as_datetime(_cell(row, "Source Deadline"))
        if (
            manifest.adapter_id is SourceKind.HIMALAYAS
            and source_deadline is not None
            and source_deadline <= observed_at
        ):
            updates.append(
                ListingAvailabilityUpdate(
                    record_id=record_id,
                    status=ListingAvailabilityStatus.EXPIRED,
                    checked_at=observed_at,
                    reason="Himalayas source expiry has passed",
                    archive=True,
                )
            )
            continue

        if manifest.adapter_id is SourceKind.JOBICY and source_record_id in current_ids.get(
            manifest.adapter_id, set()
        ):
            previous_observation = latest_events.get(record_id)
            if previous_observation and previous_observation[0] == "listing_unavailable_observed":
                updates.append(
                    ListingAvailabilityUpdate(
                        record_id=record_id,
                        status=ListingAvailabilityStatus.ACTIVE,
                        checked_at=observed_at,
                        reason="Listing appeared in the current successful source response",
                    )
                )
            continue

        previous = latest_events.get(record_id)
        if (
            manifest.adapter_id is SourceKind.HIMALAYAS
            and previous is not None
            and previous[0] == "himalayas_exact_not_found_observed"
            and observed_at - previous[1] < _MIN_CONFIRMATION_INTERVAL
        ):
            # A same-day repeat cannot confirm closure; keep the original
            # observation eligible for the next scheduled run.
            continue

        candidates.append(
            (
                record_id,
                str(_cell(row, "Source URL") or ""),
                manifest,
                previous,
            )
        )

    candidates.sort(
        key=lambda candidate: (
            0
            if candidate[2].adapter_id is SourceKind.HIMALAYAS
            and candidate[3] is not None
            and candidate[3][0] == "himalayas_exact_not_found_observed"
            else 1,
            candidate[3][1] if candidate[3] else datetime.min.replace(tzinfo=UTC),
        )
    )

    with ExitStack() as stack:
        clients = {}
        if probe is None and any(
            candidate[2].adapter_id is SourceKind.JOBICY for candidate in candidates[:max_probes]
        ):
            clients[SourceKind.JOBICY] = stack.enter_context(
                SafeHttpClient(
                    allowed_hosts=set(manifests[SourceKind.JOBICY].allowed_hosts),
                    max_response_bytes=1_024,
                    timeout_seconds=15,
                    requests_per_minute=manifests[SourceKind.JOBICY].rate_limit_per_minute,
                )
            )
        verifier = None
        if exact_probe is None and any(
            candidate[2].adapter_id is SourceKind.HIMALAYAS for candidate in candidates[:max_probes]
        ):
            endpoint = manifests[SourceKind.HIMALAYAS].availability_endpoint
            if endpoint:
                verifier = stack.enter_context(
                    HimalayasExactVerifier(
                        endpoint=endpoint,
                        requests_per_minute=min(
                            4, manifests[SourceKind.HIMALAYAS].rate_limit_per_minute
                        ),
                    )
                )

        def safe_probe(url: str, manifest: SourceManifest) -> int:
            return clients[manifest.adapter_id].head_status(url)

        def safe_exact_probe(url: str, _manifest: SourceManifest) -> ExactJobCheck:
            if verifier is None:
                return ExactJobCheck(ExactJobStatus.INCONCLUSIVE, "Exact-job verifier is disabled")
            return verifier.check(url)

        for record_id, url, manifest, previous in candidates[:max_probes]:
            if manifest.adapter_id is SourceKind.HIMALAYAS:
                updates.append(
                    _exact_update(
                        record_id,
                        url,
                        manifest,
                        previous,
                        observed_at,
                        run_id,
                        exact_probe or safe_exact_probe,
                    )
                )
            else:
                updates.append(
                    _probe_update(
                        record_id,
                        url,
                        manifest,
                        previous[0] if previous else None,
                        observed_at,
                        probe or safe_probe,
                    )
                )
    return updates


def _exact_update(
    record_id: str,
    url: str,
    manifest: SourceManifest,
    previous: tuple[str, datetime, str] | None,
    checked_at: datetime,
    run_id: str,
    probe: Callable[[str, SourceManifest], ExactJobCheck],
) -> ListingAvailabilityUpdate:
    try:
        result = probe(url, manifest)
    except Exception as exc:
        result = ExactJobCheck(
            ExactJobStatus.INCONCLUSIVE, f"Exact-job check failed ({exc.__class__.__name__})"
        )
    status = {
        ExactJobStatus.ACTIVE: ListingAvailabilityStatus.ACTIVE,
        ExactJobStatus.NOT_FOUND: ListingAvailabilityStatus.UNAVAILABLE,
        ExactJobStatus.INCONCLUSIVE: ListingAvailabilityStatus.INCONCLUSIVE,
    }[result.status]
    archive = bool(
        result.status is ExactJobStatus.NOT_FOUND
        and previous is not None
        and previous[0] == "himalayas_exact_not_found_observed"
        and previous[2]
        and run_id
        and previous[2] != run_id
        and checked_at - previous[1] >= _MIN_CONFIRMATION_INTERVAL
    )
    return ListingAvailabilityUpdate(
        record_id=record_id,
        status=status,
        checked_at=checked_at,
        reason=result.reason,
        evidence_kind="himalayas_exact",
        archive=archive,
    )


def _probe_update(
    record_id: str,
    url: str,
    manifest: SourceManifest,
    previous_event: Any,
    checked_at: datetime,
    probe: Callable[[str, SourceManifest], int],
) -> ListingAvailabilityUpdate:
    try:
        status = probe(url, manifest)
    except Exception as exc:
        return ListingAvailabilityUpdate(
            record_id=record_id,
            status=ListingAvailabilityStatus.INCONCLUSIVE,
            checked_at=checked_at,
            reason=f"Availability check was inconclusive ({exc.__class__.__name__})",
        )
    if status in {404, 410}:
        archive = previous_event == "listing_unavailable_observed"
        return ListingAvailabilityUpdate(
            record_id=record_id,
            status=ListingAvailabilityStatus.UNAVAILABLE,
            checked_at=checked_at,
            reason=f"Source listing returned HTTP {status}",
            http_status=status,
            archive=archive,
        )
    if 200 <= status < 300:
        return ListingAvailabilityUpdate(
            record_id=record_id,
            status=ListingAvailabilityStatus.ACTIVE,
            checked_at=checked_at,
            reason=f"Source listing returned HTTP {status}",
            http_status=status,
        )
    return ListingAvailabilityUpdate(
        record_id=record_id,
        status=ListingAvailabilityStatus.INCONCLUSIVE,
        checked_at=checked_at,
        reason=f"Source listing returned non-definitive HTTP {status}",
        http_status=status,
    )


def _latest_availability_events(snapshot: WorkbookSnapshot) -> dict[str, tuple[str, datetime, str]]:
    rows = snapshot.tabs.get("System Events", [])
    if not rows:
        return {}
    header = [str(value) for value in rows[0]]
    if header != SYSTEM_EVENT_COLUMNS:
        return {}
    record_index = header.index("Record ID")
    type_index = header.index("Event Type")
    observed_index = header.index("Observed At")
    run_index = header.index("Run ID")
    latest: dict[str, tuple[str, datetime, str]] = {}
    for row in rows[1:]:
        record_id = str(row[record_index]) if len(row) > record_index else ""
        event_type = str(row[type_index]) if len(row) > type_index else ""
        if record_id and event_type in _AVAILABILITY_EVENT_TYPES:
            observed_at = _as_datetime(row[observed_index] if len(row) > observed_index else "")
            latest[record_id] = (
                event_type,
                observed_at or datetime.min.replace(tzinfo=UTC),
                str(row[run_index]) if len(row) > run_index else "",
            )
    return latest


def _cell(row: list[Any], column: str) -> Any:
    index = OPPORTUNITY_COLUMNS.index(column)
    return row[index] if index < len(row) else ""


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = datetime(1899, 12, 30, tzinfo=UTC) + timedelta(days=float(value))
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.fromisoformat(f"{value.strip()}T00:00:00+00:00")
            except ValueError:
                return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)

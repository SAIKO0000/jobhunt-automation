from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobhunt.availability import review_inbox_availability
from jobhunt.config import load_source_manifests
from jobhunt.models import (
    FetchBatch,
    ListingAvailabilityStatus,
    ListingAvailabilityUpdate,
    RetentionClass,
    RunResult,
    SourceKind,
    SourceRecord,
    WorkbookSnapshot,
)
from jobhunt.workbook.gateway import LocalJsonWorkbook
from jobhunt.workbook.schema import (
    DEDUPE_COLUMNS,
    OPPORTUNITY_COLUMNS,
    SYSTEM_EVENT_COLUMNS,
    initial_tab_values,
)


def _row(**values: object) -> list[object]:
    defaults: dict[str, object] = {
        "Pipeline Stage": "Inbox",
        "Company": "Example Co",
        "Role": "Junior Developer",
        "Source": "Jobicy",
        "Source Record ID": "job-1",
        "Source URL": "https://jobicy.com/jobs/example",
        "Apply URL": "https://jobicy.com/jobs/example",
        "Record ID": "record-1",
        "Notes": "keep this note",
    }
    defaults.update(values)
    return [defaults.get(column, "") for column in OPPORTUNITY_COLUMNS]


def _snapshot(row: list[object], *events: str) -> WorkbookSnapshot:
    tabs = initial_tab_values()
    tabs["Opportunities"].append(row)
    for event_type in events:
        values = {
            "Event ID": f"event-{len(tabs['System Events'])}",
            "Run ID": "earlier-run",
            "Observed At": "2026-09-14T00:00:00Z",
            "Severity": "info",
            "Event Type": event_type,
            "Record ID": "record-1",
            "Message": "earlier observation",
        }
        tabs["System Events"].append([values.get(column, "") for column in SYSTEM_EVENT_COLUMNS])
    return WorkbookSnapshot(
        schema_version="3.2.0",
        spreadsheet_id="local-workbook",
        tabs=tabs,
    )


def _manifests():
    return load_source_manifests(Path("config"))


def _successful_batch(source: SourceKind, records: list[SourceRecord] | None = None) -> FetchBatch:
    return FetchBatch(source=source, records=records or [])


def _record(source_record_id: str = "job-1") -> SourceRecord:
    return SourceRecord(
        source=SourceKind.JOBICY,
        source_label="Jobicy",
        source_record_id=source_record_id,
        source_url="https://jobicy.com/jobs/example",
        apply_url="https://jobicy.com/jobs/example",
        company="Example Co",
        title="Junior Developer",
        attribution="Jobicy",
        retention_class=RetentionClass.EXCERPT,
    )


def test_filtered_batch_absence_needs_two_direct_not_found_observations() -> None:
    manifests = _manifests()
    now = datetime(2026, 9, 15, tzinfo=UTC)
    first = review_inbox_availability(
        _snapshot(_row()),
        [_successful_batch(SourceKind.JOBICY)],
        manifests,
        now=now,
        probe=lambda _url, _manifest: 404,
    )
    assert len(first) == 1
    assert first[0].status is ListingAvailabilityStatus.UNAVAILABLE
    assert first[0].archive is False

    second = review_inbox_availability(
        _snapshot(_row(), "listing_unavailable_observed"),
        [_successful_batch(SourceKind.JOBICY)],
        manifests,
        now=now + timedelta(days=1),
        probe=lambda _url, _manifest: 410,
    )
    assert second[0].archive is True


def test_inconclusive_status_never_archives_and_breaks_confirmation() -> None:
    updates = review_inbox_availability(
        _snapshot(_row(), "listing_unavailable_observed", "listing_check_inconclusive"),
        [_successful_batch(SourceKind.JOBICY)],
        _manifests(),
        probe=lambda _url, _manifest: 503,
    )
    assert updates[0].status is ListingAvailabilityStatus.INCONCLUSIVE
    assert updates[0].archive is False


def test_current_source_presence_resets_a_prior_unavailable_observation() -> None:
    updates = review_inbox_availability(
        _snapshot(_row(), "listing_unavailable_observed"),
        [_successful_batch(SourceKind.JOBICY, [_record()])],
        _manifests(),
        probe=lambda _url, _manifest: 404,
    )
    assert len(updates) == 1
    assert updates[0].status is ListingAvailabilityStatus.ACTIVE


def test_himalayas_source_expiry_archives_without_a_network_probe() -> None:
    called = False

    def probe(_url, _manifest):
        nonlocal called
        called = True
        return 200

    updates = review_inbox_availability(
        _snapshot(
            _row(
                Source="Himalayas",
                **{
                    "Source Record ID": "himalayas-1",
                    "Source URL": "https://himalayas.app/jobs/example",
                    "Source Deadline": "2026-09-14T00:00:00Z",
                },
            )
        ),
        [_successful_batch(SourceKind.HIMALAYAS)],
        _manifests(),
        now=datetime(2026, 9, 15, tzinfo=UTC),
        probe=probe,
    )
    assert updates[0].status is ListingAvailabilityStatus.EXPIRED
    assert updates[0].archive is True
    assert called is False


def test_probe_limit_rotates_to_the_least_recently_checked_listing() -> None:
    snapshot = _snapshot(_row())
    snapshot.tabs["Opportunities"].append(
        _row(
            **{
                "Record ID": "record-2",
                "Source Record ID": "job-2",
                "Source URL": "https://jobicy.com/jobs/unchecked",
            }
        )
    )
    snapshot.tabs["System Events"].append(
        [
            {
                "Event ID": "recent-event",
                "Run ID": "earlier-run",
                "Observed At": "2026-09-14T00:00:00Z",
                "Severity": "info",
                "Event Type": "listing_available_observed",
                "Record ID": "record-1",
                "Message": "recent check",
            }.get(column, "")
            for column in SYSTEM_EVENT_COLUMNS
        ]
    )
    updates = review_inbox_availability(
        snapshot,
        [_successful_batch(SourceKind.JOBICY)],
        _manifests(),
        max_probes=1,
        probe=lambda _url, _manifest: 200,
    )
    assert [update.record_id for update in updates] == ["record-2"]


def test_archiving_moves_lossless_row_and_updates_dedupe_location(tmp_path) -> None:
    snapshot = _snapshot(_row())
    snapshot.tabs["Dedupe Index"].append(
        [
            {
                "Canonical Key": "key-1",
                "Record ID": "record-1",
                "Primary Tab": "Opportunities",
                "Primary Row": 2,
            }.get(column, "")
            for column in DEDUPE_COLUMNS
        ]
    )
    path = tmp_path / "workbook.json"
    path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    workbook = LocalJsonWorkbook(path)
    run = RunResult(
        status="success",
        availability_updates=[
            ListingAvailabilityUpdate(
                record_id="record-1",
                status=ListingAvailabilityStatus.UNAVAILABLE,
                reason="Source listing returned HTTP 410",
                http_status=410,
                archive=True,
            )
        ],
    )
    workbook.commit(snapshot, [], run, [])
    current = workbook.snapshot()

    assert not any(value not in (None, "") for value in current.tabs["Opportunities"][1])
    excluded = current.tabs["Excluded"][1]
    assert excluded[OPPORTUNITY_COLUMNS.index("Notes")] == "keep this note"
    assert excluded[OPPORTUNITY_COLUMNS.index("Processing Status")] == "Skipped"
    assert "HTTP 410" in excluded[OPPORTUNITY_COLUMNS.index("Blockers")]
    dedupe = current.tabs["Dedupe Index"][1]
    assert dedupe[DEDUPE_COLUMNS.index("Primary Tab")] == "Excluded"
    assert dedupe[DEDUPE_COLUMNS.index("Primary Row")] == 2
    assert current.tabs["System Events"][-1][SYSTEM_EVENT_COLUMNS.index("Event Type")] == (
        "listing_inactive_archived"
    )

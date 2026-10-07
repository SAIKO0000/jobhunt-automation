from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from conftest import make_opportunity

from jobhunt.availability import review_inbox_availability
from jobhunt.config import load_source_manifests
from jobhunt.himalayas_availability import (
    ExactJobCheck,
    ExactJobStatus,
    HimalayasExactVerifier,
    job_slugs,
)
from jobhunt.models import (
    FetchBatch,
    ListingAvailabilityStatus,
    ListingAvailabilityUpdate,
    RunResult,
    SourceKind,
    WorkbookSnapshot,
)
from jobhunt.workbook.gateway import LocalJsonWorkbook
from jobhunt.workbook.schema import OPPORTUNITY_COLUMNS, SYSTEM_EVENT_COLUMNS, initial_tab_values

BROKEN_URL = "https://himalayas.app/companies/wing-assistant/jobs/social-media-admin-assistant"


def _mcp_response(message: str, *, status: int = 200) -> httpx.Response:
    data = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": message}]}}
    return httpx.Response(
        status,
        headers={"content-type": "text/event-stream"},
        text=f"event: message\ndata: {json.dumps(data)}\n\n",
    )


def test_exact_verifier_calls_only_public_read_only_job_tool() -> None:
    calls: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _mcp_response(
            f"# Social Media & Admin Assistant\nApply: {BROKEN_URL}?utm_source=mcp"
        )

    with HimalayasExactVerifier(
        transport=httpx.MockTransport(reply), requests_per_minute=100_000
    ) as verifier:
        result = verifier.check(BROKEN_URL)
    assert result.status is ExactJobStatus.ACTIVE
    assert len(calls) == 1
    assert str(calls[0].url) == "https://mcp.himalayas.app/mcp"
    assert calls[0].method == "POST"
    payload = json.loads(calls[0].content)
    assert payload == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "get_job_details",
            "arguments": {
                "company_slug": "wing-assistant",
                "job_slug": "social-media-admin-assistant",
            },
        },
    }


def test_exact_verifier_recognizes_matching_not_found_response() -> None:
    message = (
        "Job not found. Check the company slug ('wing-assistant') "
        "and job slug ('social-media-admin-assistant')."
    )
    with HimalayasExactVerifier(
        transport=httpx.MockTransport(lambda _request: _mcp_response(message)),
        requests_per_minute=100_000,
    ) as verifier:
        assert verifier.check(BROKEN_URL).status is ExactJobStatus.NOT_FOUND


def test_exact_verifier_does_not_accept_a_different_job_with_a_matching_prefix() -> None:
    with HimalayasExactVerifier(
        transport=httpx.MockTransport(
            lambda _request: _mcp_response(f"# Different job\nApply: {BROKEN_URL}-senior")
        ),
        requests_per_minute=100_000,
    ) as verifier:
        assert verifier.check(BROKEN_URL).status is ExactJobStatus.INCONCLUSIVE


def test_exact_verifier_fails_closed_on_invalid_urls_and_unrecognized_responses() -> None:
    invalid = [
        "https://himalayas.app/jobs",
        "https://evil.example/companies/wing-assistant/jobs/social-media-admin-assistant",
        "http://himalayas.app/companies/wing-assistant/jobs/social-media-admin-assistant",
    ]
    assert all(job_slugs(url) is None for url in invalid)
    calls = 0

    def reply(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _mcp_response("Job not found, perhaps. Check the listing.")

    with HimalayasExactVerifier(
        transport=httpx.MockTransport(reply), requests_per_minute=100_000
    ) as verifier:
        assert verifier.check(invalid[0]).status is ExactJobStatus.INCONCLUSIVE
        assert verifier.check(BROKEN_URL).status is ExactJobStatus.INCONCLUSIVE
    assert calls == 1


def test_exact_verifier_treats_quota_malformed_and_oversize_as_inconclusive() -> None:
    responses = [
        httpx.Response(429),
        httpx.Response(308, headers={"location": "https://himalayas.app/jobs"}),
        httpx.Response(200, headers={"content-type": "text/event-stream"}, text="bad"),
        httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text="x" * (128 * 1024 + 1)
        ),
    ]
    for response in responses:
        with HimalayasExactVerifier(
            transport=httpx.MockTransport(lambda _request, result=response: result),
            requests_per_minute=100_000,
        ) as verifier:
            assert verifier.check(BROKEN_URL).status is ExactJobStatus.INCONCLUSIVE


def test_exact_verifier_treats_transport_timeout_as_inconclusive() -> None:
    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    with HimalayasExactVerifier(
        transport=httpx.MockTransport(timeout), requests_per_minute=100_000
    ) as verifier:
        assert verifier.check(BROKEN_URL).status is ExactJobStatus.INCONCLUSIVE


def _snapshot(*events: tuple[str, str, datetime]) -> WorkbookSnapshot:
    tabs = initial_tab_values()
    values = {
        "Record ID": "broken-record",
        "Pipeline Stage": "Inbox",
        "Source": "himalayas",
        "Source Record ID": "source-1",
        "Source URL": BROKEN_URL,
        "Apply URL": BROKEN_URL,
    }
    tabs["Opportunities"].append([values.get(column, "") for column in OPPORTUNITY_COLUMNS])
    for event_type, run_id, observed_at in events:
        event = {
            "Event ID": f"event-{len(tabs['System Events'])}",
            "Run ID": run_id,
            "Observed At": observed_at.isoformat(),
            "Severity": "info",
            "Event Type": event_type,
            "Record ID": "broken-record",
            "Message": "Exact check",
        }
        tabs["System Events"].append([event.get(column, "") for column in SYSTEM_EVENT_COLUMNS])
    return WorkbookSnapshot(schema_version="3.2.0", spreadsheet_id="test", tabs=tabs)


def _check(
    snapshot: WorkbookSnapshot,
    *,
    now: datetime,
    run_id: str,
    status: ExactJobStatus = ExactJobStatus.NOT_FOUND,
):
    manifests = load_source_manifests(Path("config"))
    batch = FetchBatch(source=SourceKind.HIMALAYAS, records=[])
    return review_inbox_availability(
        snapshot,
        [batch],
        manifests,
        now=now,
        run_id=run_id,
        exact_probe=lambda _url, _manifest: ExactJobCheck(status, "Exact check"),
    )[0]


def test_exact_not_found_needs_separate_runs_at_least_20_hours_apart() -> None:
    start = datetime(2026, 10, 7, 0, 0, tzinfo=UTC)
    first = _check(_snapshot(), now=start, run_id="run-1")
    assert first.status is ListingAvailabilityStatus.UNAVAILABLE
    assert first.archive is False

    previous = _snapshot(("himalayas_exact_not_found_observed", "run-1", start))
    assert (
        review_inbox_availability(
            previous,
            [FetchBatch(source=SourceKind.HIMALAYAS, records=[])],
            load_source_manifests(Path("config")),
            now=start + timedelta(hours=19),
            run_id="run-2",
            exact_probe=lambda _url, _manifest: ExactJobCheck(
                ExactJobStatus.NOT_FOUND, "Exact check"
            ),
        )
        == []
    )
    assert _check(previous, now=start + timedelta(hours=24), run_id="run-1").archive is False
    assert _check(previous, now=start + timedelta(hours=24), run_id="run-2").archive is True


def test_pending_negative_waits_twenty_hours_then_takes_priority() -> None:
    start = datetime(2026, 10, 7, 0, 0, tzinfo=UTC)
    snapshot = _snapshot(("himalayas_exact_not_found_observed", "run-1", start))
    other = {
        "Record ID": "never-checked",
        "Pipeline Stage": "Inbox",
        "Source": "himalayas",
        "Source Record ID": "source-2",
        "Source URL": "https://himalayas.app/companies/example/jobs/other-job",
    }
    snapshot.tabs["Opportunities"].append([other.get(column, "") for column in OPPORTUNITY_COLUMNS])
    manifests = load_source_manifests(Path("config"))
    batch = FetchBatch(source=SourceKind.HIMALAYAS, records=[])
    calls: list[str] = []

    def negative(url, _manifest):
        calls.append(url)
        return ExactJobCheck(ExactJobStatus.NOT_FOUND, "Exact check")

    early = review_inbox_availability(
        snapshot,
        [batch],
        manifests,
        now=start + timedelta(hours=19),
        run_id="run-2",
        max_probes=1,
        exact_probe=negative,
    )
    assert [item.record_id for item in early] == ["never-checked"]
    assert calls == [other["Source URL"]]

    calls.clear()
    due = review_inbox_availability(
        snapshot,
        [batch],
        manifests,
        now=start + timedelta(hours=20),
        run_id="run-3",
        max_probes=1,
        exact_probe=negative,
    )
    assert [item.record_id for item in due] == ["broken-record"]
    assert due[0].archive is True
    assert calls == [BROKEN_URL]


def test_positive_or_inconclusive_observation_breaks_confirmation() -> None:
    start = datetime(2026, 10, 7, 0, 0, tzinfo=UTC)
    for last_event in ("listing_available_observed", "listing_check_inconclusive"):
        snapshot = _snapshot(
            ("himalayas_exact_not_found_observed", "run-1", start),
            (last_event, "run-2", start + timedelta(hours=24)),
        )
        assert _check(snapshot, now=start + timedelta(hours=48), run_id="run-3").archive is False


def test_himalayas_is_checked_even_when_cached_search_contains_its_id() -> None:
    from jobhunt.models import RetentionClass, SourceRecord

    snapshot = _snapshot()
    record = SourceRecord(
        source=SourceKind.HIMALAYAS,
        source_record_id="source-1",
        source_url=BROKEN_URL,
        title="Social Media & Admin Assistant",
        company="Wing Assistant",
        attribution="Himalayas",
        retention_class=RetentionClass.EXCERPT,
    )
    checks: list[str] = []

    def forbidden_head(_url, _manifest):
        raise AssertionError("Himalayas job pages must not receive HEAD checks")

    updates = review_inbox_availability(
        snapshot,
        [FetchBatch(source=SourceKind.HIMALAYAS, records=[record])],
        load_source_manifests(Path("config")),
        run_id="run-1",
        probe=forbidden_head,
        exact_probe=lambda url, _manifest: (
            checks.append(url) or ExactJobCheck(ExactJobStatus.NOT_FOUND, "Exact check")
        ),
    )
    assert checks == [BROKEN_URL]
    assert updates[0].status is ListingAvailabilityStatus.UNAVAILABLE


def test_exact_probe_limit_is_ten_even_with_many_inbox_rows() -> None:
    snapshot = _snapshot()
    for index in range(2, 18):
        values = {
            "Record ID": f"record-{index}",
            "Pipeline Stage": "Inbox",
            "Source": "himalayas",
            "Source Record ID": f"source-{index}",
            "Source URL": BROKEN_URL,
        }
        snapshot.tabs["Opportunities"].append(
            [values.get(column, "") for column in OPPORTUNITY_COLUMNS]
        )
    checks: list[str] = []
    updates = review_inbox_availability(
        snapshot,
        [FetchBatch(source=SourceKind.HIMALAYAS, records=[])],
        load_source_manifests(Path("config")),
        run_id="run-1",
        exact_probe=lambda url, _manifest: (
            checks.append(url) or ExactJobCheck(ExactJobStatus.ACTIVE, "Exact check")
        ),
    )
    assert len(checks) == len(updates) == 10


def test_confirmed_archive_preserves_human_fields_and_cached_result_cannot_restore(
    tmp_path,
) -> None:
    workbook = LocalJsonWorkbook(tmp_path / "workbook.json")
    workbook.ensure_schema()
    opportunity = make_opportunity()
    workbook.commit(workbook.snapshot(), [opportunity], RunResult(), [])
    before = workbook.snapshot()
    record_id = str(opportunity.record_id)
    notes_index = OPPORTUNITY_COLUMNS.index("Notes")
    before.tabs["Opportunities"][1][notes_index] = "Keep my review note"
    (tmp_path / "workbook.json").write_text(before.model_dump_json(), encoding="utf-8")
    before = workbook.snapshot()

    update = ListingAvailabilityUpdate(
        record_id=record_id,
        status=ListingAvailabilityStatus.UNAVAILABLE,
        evidence_kind="himalayas_exact",
        reason="Himalayas exact job was not found",
        archive=True,
    )
    workbook.commit(
        before,
        [opportunity],
        RunResult(availability_updates=[update]),
        [],
    )
    archived = workbook.snapshot()
    assert archived.tabs["Excluded"][1][notes_index] == "Keep my review note"
    assert archived.tabs["System Events"][-1][SYSTEM_EVENT_COLUMNS.index("Event Type")] == (
        "himalayas_exact_not_found_archived"
    )

    workbook.commit(archived, [opportunity], RunResult(), [])
    after = workbook.snapshot()
    assert len(after.tabs["Excluded"]) == 2
    assert all(not any(row) for row in after.tabs["Opportunities"][1:])


def test_human_edit_during_exact_check_defers_archive(tmp_path) -> None:
    workbook = LocalJsonWorkbook(tmp_path / "workbook.json")
    workbook.ensure_schema()
    opportunity = make_opportunity()
    workbook.commit(workbook.snapshot(), [opportunity], RunResult(), [])
    before = workbook.snapshot()
    edited = before.model_copy(deep=True)
    notes_index = OPPORTUNITY_COLUMNS.index("Notes")
    edited.tabs["Opportunities"][1][notes_index] = "New human edit"
    (tmp_path / "workbook.json").write_text(edited.model_dump_json(), encoding="utf-8")

    update = ListingAvailabilityUpdate(
        record_id=str(opportunity.record_id),
        status=ListingAvailabilityStatus.UNAVAILABLE,
        evidence_kind="himalayas_exact",
        reason="Himalayas exact job was not found",
        archive=True,
    )
    plan = workbook.commit(
        before,
        [opportunity],
        RunResult(availability_updates=[update]),
        [],
    )
    after = workbook.snapshot()
    assert plan.conflicts
    assert len(after.tabs["Excluded"]) == 1
    assert after.tabs["Opportunities"][1][notes_index] == "New human edit"

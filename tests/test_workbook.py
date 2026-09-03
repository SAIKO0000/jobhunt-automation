from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import google_auth_httplib2
import googleapiclient.discovery
import googleapiclient.discovery_cache
import httplib2
import pytest
from conftest import make_opportunity

from jobhunt.backup import LocalSnapshotStore
from jobhunt.config import load_source_manifests
from jobhunt.models import FitBand, ProcessingStatus, RunResult, WorkbookSnapshot
from jobhunt.workbook.gateway import (
    GOOGLE_HTTP_TIMEOUT_SECONDS,
    GOOGLE_READ_RETRIES,
    GoogleSheetsWorkbook,
    LocalJsonWorkbook,
    WorkbookConflict,
    _collapsed_column_group_requests,
    _dashboard_chart_requests,
    _presentation_cleanup_requests,
    _sanitized_google_error,
    build_v3_migration,
    run_is_logged,
    user_fields_hash,
)
from jobhunt.workbook.schema import (
    APPLICATION_DETAIL_COLUMNS,
    DAILY_OPPORTUNITY_COLUMNS,
    DEDUPE_COLUMNS,
    LEGACY_OPPORTUNITY_COLUMNS_V2,
    OPPORTUNITY_COLUMNS,
    OPPORTUNITY_COLUMNS_V3_0,
    OPPORTUNITY_COLUMNS_V3_1,
    REVIEW_DETAIL_COLUMNS,
    RUN_LOG_COLUMNS,
    SCHEMA_VERSION,
    SOURCE_CONFIG_COLUMNS,
    SYSTEM_DETAIL_COLUMNS,
    TAB_SCHEMAS,
    dashboard_values,
    initial_tab_values,
)


def _assert_matches_discovery_schema(
    value: Any, schema: dict[str, Any], schemas: dict[str, dict[str, Any]], path: str
) -> None:
    if "$ref" in schema:
        _assert_matches_discovery_schema(value, schemas[schema["$ref"]], schemas, path)
        return
    if schema.get("type") == "array":
        assert isinstance(value, list), f"{path} must be an array"
        for index, item in enumerate(value):
            _assert_matches_discovery_schema(item, schema["items"], schemas, f"{path}[{index}]")
        return
    if schema.get("type") != "object":
        if "enum" in schema:
            assert value in schema["enum"], f"{path} has invalid enum value {value!r}"
        return
    assert isinstance(value, dict), f"{path} must be an object"
    properties = schema.get("properties", {})
    additional = schema.get("additionalProperties")
    for key, item in value.items():
        child_schema = properties.get(key, additional)
        assert child_schema is not None, f"{path}.{key} is absent from Sheets discovery schema"
        _assert_matches_discovery_schema(item, child_schema, schemas, f"{path}.{key}")


def _assert_sheets_requests_match_discovery(requests: list[dict[str, object]]) -> None:
    document_path = (
        Path(googleapiclient.discovery_cache.__file__).parent / "documents" / "sheets.v4.json"
    )
    document = json.loads(document_path.read_text(encoding="utf-8"))
    schemas = document["schemas"]
    for index, request in enumerate(requests):
        assert len(request) == 1, f"requests[{index}] must contain exactly one request kind"
        _assert_matches_discovery_schema(request, schemas["Request"], schemas, f"requests[{index}]")


def test_local_workbook_bootstrap_commit_and_schema(tmp_path, config_dir) -> None:
    workbook = LocalJsonWorkbook(tmp_path / "workbook.json")
    workbook.ensure_schema()
    assert workbook.validate() == []
    initial = workbook.snapshot()
    opportunity = make_opportunity()
    run = RunResult(finished_at=datetime.now(UTC), status="success", records_fetched=1)
    plan = workbook.commit(
        initial,
        [opportunity],
        run,
        list(load_source_manifests(config_dir).values()),
    )
    assert plan.records_written == 1
    current = workbook.snapshot()
    rows = current.tabs["Opportunities"]
    assert rows[1][OPPORTUNITY_COLUMNS.index("Record ID")] == str(opportunity.record_id)
    assert rows[1][OPPORTUNITY_COLUMNS.index("Pipeline Stage")] == "Inbox"
    assert len(current.tabs["Dedupe Index"]) == 2
    assert len(current.tabs["Run Log"]) == 2
    assert len(current.tabs["Source Config"]) == 10
    dashboard_text = json.dumps(current.tabs["Dashboard"])
    assert "FILTER('Run Log'" in dashboard_text
    assert "NEXT 7 DAYS" in dashboard_text
    assert "Weekly Submissions" not in dashboard_text
    assert "Strong Fits" in dashboard_text


def test_skipped_records_are_archived_outside_the_visible_queue(tmp_path, config_dir) -> None:
    workbook = LocalJsonWorkbook(tmp_path / "workbook.json")
    workbook.ensure_schema()
    skipped = make_opportunity().model_copy(
        update={"fit_band": FitBand.SKIP, "processing_status": ProcessingStatus.SKIPPED}
    )

    workbook.commit(
        workbook.snapshot(),
        [skipped],
        RunResult(),
        list(load_source_manifests(config_dir).values()),
    )

    current = workbook.snapshot()
    assert len(current.tabs["Opportunities"]) == 1
    assert len(current.tabs["Excluded"]) == 2
    assert current.tabs["Excluded"][1][OPPORTUNITY_COLUMNS.index("Record ID")] == str(
        skipped.record_id
    )
    assert current.tabs["Dedupe Index"][1][4] == "Excluded"


def test_reclassification_moves_record_and_preserves_human_notes(tmp_path, config_dir) -> None:
    workbook = LocalJsonWorkbook(tmp_path / "workbook.json")
    workbook.ensure_schema()
    opportunity = make_opportunity()
    manifests = list(load_source_manifests(config_dir).values())
    workbook.commit(workbook.snapshot(), [opportunity], RunResult(), manifests)
    edited = workbook.snapshot()
    edited.tabs["Opportunities"][1][OPPORTUNITY_COLUMNS.index("Notes")] = "Keep this"
    (tmp_path / "workbook.json").write_text(edited.model_dump_json(indent=2), encoding="utf-8")

    initial = workbook.snapshot()
    skipped = opportunity.model_copy(
        update={"fit_band": FitBand.SKIP, "processing_status": ProcessingStatus.SKIPPED}
    )
    workbook.commit(initial, [skipped], RunResult(), manifests)

    current = workbook.snapshot()
    assert not any(current.tabs["Opportunities"][1])
    archived = current.tabs["Excluded"][1]
    assert archived[OPPORTUNITY_COLUMNS.index("Notes")] == "Keep this"


def test_human_edit_race_is_quarantined_and_preserved(tmp_path, config_dir) -> None:
    workbook = LocalJsonWorkbook(tmp_path / "workbook.json")
    workbook.ensure_schema()
    opportunity = make_opportunity()
    manifests = list(load_source_manifests(config_dir).values())
    workbook.commit(workbook.snapshot(), [opportunity], RunResult(), manifests)
    initial = workbook.snapshot()

    edited = initial.model_copy(deep=True)
    notes_index = OPPORTUNITY_COLUMNS.index("Notes")
    edited.tabs["Opportunities"][1][notes_index] = "Human edit during run"
    (tmp_path / "workbook.json").write_text(edited.model_dump_json(indent=2), encoding="utf-8")

    plan = workbook.commit(initial, [opportunity], RunResult(), manifests)
    assert len(plan.conflicts) == 1
    current = workbook.snapshot()
    assert current.tabs["Opportunities"][1][notes_index] == "Human edit during run"
    assert len(current.tabs["System Events"]) == 2


def test_missing_pipeline_dates_are_reported_as_warnings(tmp_path, config_dir) -> None:
    workbook = LocalJsonWorkbook(tmp_path / "workbook.json")
    workbook.ensure_schema()
    manifests = list(load_source_manifests(config_dir).values())
    workbook.commit(workbook.snapshot(), [make_opportunity()], RunResult(), manifests)
    snapshot = workbook.snapshot()
    status_index = OPPORTUNITY_COLUMNS.index("Pipeline Stage")
    snapshot.tabs["Opportunities"][1][status_index] = "Submitted"
    (tmp_path / "workbook.json").write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    issues = workbook.validate()
    assert any(issue.startswith("warning:") and "Submitted At" in issue for issue in issues)


def test_pipeline_change_gets_observed_event_without_requiring_ai(tmp_path, config_dir) -> None:
    workbook = LocalJsonWorkbook(tmp_path / "workbook.json")
    workbook.ensure_schema()
    manifests = list(load_source_manifests(config_dir).values())
    opportunity = make_opportunity()
    workbook.commit(workbook.snapshot(), [opportunity], RunResult(), manifests)

    edited = workbook.snapshot()
    row = edited.tabs["Opportunities"][1]
    row[OPPORTUNITY_COLUMNS.index("Pipeline Stage")] = "Shortlisted"
    (tmp_path / "workbook.json").write_text(edited.model_dump_json(indent=2), encoding="utf-8")

    initial = workbook.snapshot()
    run = RunResult(status="success")
    workbook.commit(initial, [opportunity], run, manifests)
    current = workbook.snapshot()
    assert any(
        "human_workflow_change_observed" in event for event in current.tabs["System Events"][1:]
    )
    assert current.tabs["Opportunities"][1][OPPORTUNITY_COLUMNS.index("Pipeline Stage")] == (
        "Shortlisted"
    )
    assert run_is_logged(current, str(run.run_id))


def test_row_reorder_resolves_machine_updates_by_uuid(tmp_path, config_dir) -> None:
    workbook = LocalJsonWorkbook(tmp_path / "workbook.json")
    workbook.ensure_schema()
    manifests = list(load_source_manifests(config_dir).values())
    first = make_opportunity(source_record_id="first")
    second = make_opportunity(
        source_record_id="second",
        source_url="https://example.com/jobs/2",
        apply_url="https://example.com/jobs/2/apply",
    )
    workbook.commit(workbook.snapshot(), [first, second], RunResult(), manifests)
    initial = workbook.snapshot()
    reordered = initial.model_copy(deep=True)
    reordered.tabs["Opportunities"][1:3] = reversed(reordered.tabs["Opportunities"][1:3])
    (tmp_path / "workbook.json").write_text(reordered.model_dump_json(indent=2), encoding="utf-8")

    updated_first = first.model_copy(
        update={"ai_rationale": "Updated after UUID-based row resolution"}
    )
    plan = workbook.commit(initial, [updated_first], RunResult(), manifests)
    assert plan.conflicts == []
    rows = workbook.snapshot().tabs["Opportunities"]
    id_index = OPPORTUNITY_COLUMNS.index("Record ID")
    rationale_index = OPPORTUNITY_COLUMNS.index("Match Rationale")
    target = next(row for row in rows[1:] if row[id_index] == str(first.record_id))
    assert target[rationale_index] == "Updated after UUID-based row resolution"


def test_backup_restore_round_trip_to_blank_workbook(tmp_path) -> None:
    source = LocalJsonWorkbook(tmp_path / "source.json")
    source.ensure_schema()
    snapshot = source.snapshot()
    store = LocalSnapshotStore(tmp_path / "snapshots")
    identifier = store.save(snapshot, reason="test")
    loaded = store.load(identifier)

    destination = LocalJsonWorkbook(tmp_path / "destination.json")
    destination.restore(loaded, blank_only=True)
    restored = destination.snapshot()
    assert restored.schema_version == snapshot.schema_version
    assert set(restored.tabs) == set(TAB_SCHEMAS)

    with pytest.raises(WorkbookConflict):
        populated = restored.model_copy(deep=True)
        populated.tabs["Opportunities"].append(["populated"])
        (tmp_path / "destination.json").write_text(
            populated.model_dump_json(indent=2), encoding="utf-8"
        )
        destination.restore(loaded, blank_only=True)


def test_snapshot_existing_is_blank_before_local_bootstrap(tmp_path) -> None:
    workbook = LocalJsonWorkbook(tmp_path / "workbook.json")
    snapshot = workbook.snapshot_existing()
    assert snapshot.tabs == {}
    assert not (tmp_path / "workbook.json").exists()


def test_google_bootstrap_resumes_after_tabs_were_created_but_left_empty(monkeypatch) -> None:
    metadata = {
        "sheets": [
            {"properties": {"title": title, "sheetId": index}}
            for index, title in enumerate(TAB_SCHEMAS, start=1)
        ]
    }
    empty = WorkbookSnapshot(
        schema_version=SCHEMA_VERSION,
        spreadsheet_id="test-sheet",
        tabs={title: [] for title in TAB_SCHEMAS},
    )
    batches: list[list[dict[str, object]]] = []
    workbook = GoogleSheetsWorkbook("test-sheet", object(), service=object())
    monkeypatch.setattr(workbook, "_metadata", lambda: metadata)
    monkeypatch.setattr(workbook, "snapshot", lambda: empty)
    monkeypatch.setattr(workbook, "_batch", lambda requests: batches.append(requests))

    workbook.ensure_schema()

    assert len(batches) == 1
    requests = batches[0]
    _assert_sheets_requests_match_discovery(requests)
    assert sum("updateCells" in request for request in requests) == len(TAB_SCHEMAS)
    assert any("addChart" in request for request in requests)
    protections = [
        request["addProtectedRange"]["protectedRange"]
        for request in requests
        if "addProtectedRange" in request
    ]
    assert len(protections) == 9
    assert all(set(protection["range"]) == {"sheetId"} for protection in protections)
    editable_guards = [
        protection for protection in protections if "unprotectedRanges" in protection
    ]
    assert len(editable_guards) == 3
    assert all(protection["unprotectedRanges"] for protection in editable_guards)
    filter_views = [request for request in requests if "addFilterView" in request]
    assert {request["addFilterView"]["filter"]["title"] for request in filter_views} == {
        "Technical",
        "VA & Freelance",
        "Inbox",
        "Strong Fits",
        "Deadline Soon",
        "Active Pipeline",
        "Follow-Up Due",
        "Closed Archive",
    }
    filter_json = json.dumps(filter_views)
    assert "$M2" in filter_json  # Deadline in the compact v3.2 queue.
    assert "$P2" in filter_json  # Next Action At in the compact v3.2 queue.
    groups = [request for request in requests if "addDimensionGroup" in request]
    assert len(groups) == 1
    assert groups[0]["addDimensionGroup"]["range"] == {
        "sheetId": list(TAB_SCHEMAS).index("Opportunities") + 1,
        "dimension": "COLUMNS",
        "startIndex": len(DAILY_OPPORTUNITY_COLUMNS),
        "endIndex": len(OPPORTUNITY_COLUMNS),
    }
    group_updates = [request for request in requests if "updateDimensionGroup" in request]
    assert len(group_updates) == 1
    assert all(
        request["updateDimensionGroup"]["dimensionGroup"]["depth"] == 1 for request in group_updates
    )
    charts = [request for request in requests if "addChart" in request]
    assert len(charts) == 2
    opportunity_id = list(TAB_SCHEMAS).index("Opportunities") + 1
    sheet_updates = [
        request["updateSheetProperties"]
        for request in requests
        if "updateSheetProperties" in request
        and request["updateSheetProperties"]["properties"]["sheetId"] == opportunity_id
    ]
    assert any(
        update["properties"].get("gridProperties", {}).get("frozenColumnCount") == 3
        for update in sheet_updates
    )


def test_v32_opportunity_schema_is_progressively_disclosed() -> None:
    assert len(DAILY_OPPORTUNITY_COLUMNS) == 17
    assert OPPORTUNITY_COLUMNS[:17] == DAILY_OPPORTUNITY_COLUMNS
    assert OPPORTUNITY_COLUMNS == (
        DAILY_OPPORTUNITY_COLUMNS
        + REVIEW_DETAIL_COLUMNS
        + APPLICATION_DETAIL_COLUMNS
        + SYSTEM_DETAIL_COLUMNS
    )
    for removed in (
        "AI Score",
        "Preferred Skills",
        "Draft Type",
        "Company Domain",
        "Location Decision",
        "User Fields Version",
        "Approved By",
        "Image Digest",
    ):
        assert removed not in OPPORTUNITY_COLUMNS
    assert "Required Skill Match Score" in OPPORTUNITY_COLUMNS
    assert "Qualification" in DAILY_OPPORTUNITY_COLUMNS
    assert "Application Cost" in DAILY_OPPORTUNITY_COLUMNS
    for detail in (
        "Seniority",
        "Required Experience Years",
        "Location Eligibility",
        "Compensation",
        "Qualification Reasons",
    ):
        assert detail in REVIEW_DETAIL_COLUMNS
    for moved in ("Final Score", "Source", "Date Found"):
        assert moved not in DAILY_OPPORTUNITY_COLUMNS
        assert moved in REVIEW_DETAIL_COLUMNS


def test_v32_dashboard_is_compact_and_complete() -> None:
    rows = dashboard_values(opportunities_sheet_id=123)
    flattened = json.dumps(rows)

    assert rows[0] == ["JOB HUNT CONTROL CENTER"]
    assert "NOT STARTED" in rows[4][1]
    assert "STALE" in rows[4][1]
    for title in (
        "Technical",
        "VA & Freelance",
        "Inbox",
        "Active Pipeline",
        "Strong Fits",
        "Deadline Soon",
        "Follow-Up Due",
        "Closed Archive",
    ):
        assert title in flattened
    assert rows[59][:2] == ["Pipeline Stage", "Count"]
    assert rows[69] == ["System schema", SCHEMA_VERSION]
    assert "Schema" not in rows[0]

    charts = _dashboard_chart_requests(7)
    pipeline_position = charts[0]["addChart"]["chart"]["position"]["overlayPosition"]
    weekly_position = charts[1]["addChart"]["chart"]["position"]["overlayPosition"]
    assert pipeline_position["anchorCell"] == {"sheetId": 7, "rowIndex": 24, "columnIndex": 0}
    assert weekly_position["anchorCell"] == {"sheetId": 7, "rowIndex": 41, "columnIndex": 0}
    pipeline_domain = charts[0]["addChart"]["chart"]["spec"]["basicChart"]["domains"][0]
    source = pipeline_domain["domain"]["sourceRange"]["sources"][0]
    assert source["startRowIndex"] == 59
    assert source["endRowIndex"] == 68


def test_v30_to_v32_migration_reorders_without_losing_values() -> None:
    rows = [list(OPPORTUNITY_COLUMNS_V3_0)]
    old_row = [""] * len(OPPORTUNITY_COLUMNS_V3_0)
    preserved = {
        "Record ID": "record-31",
        "Pipeline Stage": "Preparing",
        "Priority": "High",
        "Company": "Example Co",
        "Role": "Automation Engineer",
        "Final Score": 88,
        "Source": "wwr",
        "Date Found": "2026-09-03T07:17:00+08:00",
        "Notes": "Keep this note",
        "Next Action": "Tailor portfolio",
    }
    for name, value in preserved.items():
        old_row[OPPORTUNITY_COLUMNS_V3_0.index(name)] = value
    rows.append(old_row)
    tabs = initial_tab_values()
    tabs["Opportunities"] = rows
    source = WorkbookSnapshot(schema_version="3.0.0", spreadsheet_id="test", tabs=tabs)

    target, report = build_v3_migration(source)

    assert report.source_version == "3.0.0"
    assert report.target_version == SCHEMA_VERSION
    migrated = target.tabs["Opportunities"][1]
    for name, value in preserved.items():
        actual = migrated[OPPORTUNITY_COLUMNS.index(name)]
        if name == "Date Found":
            assert actual == datetime.fromisoformat(value)
        else:
            assert actual == value
    assert migrated[OPPORTUNITY_COLUMNS.index("User Fields Hash")] == user_fields_hash(migrated)


def test_v31_to_v32_migration_reclassifies_and_archives_senior_rows() -> None:
    old_row = [""] * len(OPPORTUNITY_COLUMNS_V3_1)
    old_row[OPPORTUNITY_COLUMNS_V3_1.index("Record ID")] = "skip-31"
    old_row[OPPORTUNITY_COLUMNS_V3_1.index("Role")] = "Senior Software Engineer"
    old_row[OPPORTUNITY_COLUMNS_V3_1.index("Fit Band")] = "Low Priority"
    old_row[OPPORTUNITY_COLUMNS_V3_1.index("Processing Status")] = "Scored"
    old_row[OPPORTUNITY_COLUMNS_V3_1.index("Notes")] = "Preserve archive note"
    old_row[OPPORTUNITY_COLUMNS_V3_1.index("Description Snippet")] = (
        "Malformed source text says at least 130 years of experience."
    )
    tabs = initial_tab_values()
    tabs["Opportunities"] = [list(OPPORTUNITY_COLUMNS_V3_1), old_row]
    dedupe_row = [""] * len(DEDUPE_COLUMNS)
    dedupe_row[DEDUPE_COLUMNS.index("Record ID")] = "skip-31"
    dedupe_row[DEDUPE_COLUMNS.index("Primary Tab")] = "Opportunities"
    dedupe_row[DEDUPE_COLUMNS.index("Primary Row")] = 2
    tabs["Dedupe Index"].append(dedupe_row)
    tabs.pop("Excluded")
    tabs.pop("Manual Intake")
    source = WorkbookSnapshot(schema_version="3.1.0", spreadsheet_id="test", tabs=tabs)

    target, report = build_v3_migration(source)

    assert report.source_version == "3.1.0"
    assert len(target.tabs["Opportunities"]) == 1
    assert target.tabs["Excluded"][1][OPPORTUNITY_COLUMNS.index("Record ID")] == "skip-31"
    assert target.tabs["Excluded"][1][OPPORTUNITY_COLUMNS.index("Notes")] == (
        "Preserve archive note"
    )
    assert target.tabs["Excluded"][1][OPPORTUNITY_COLUMNS.index("Qualification")] == ("Ineligible")
    assert target.tabs["Excluded"][1][OPPORTUNITY_COLUMNS.index("Required Experience Years")] == ""
    assert (
        "supported 80-year range"
        in target.tabs["Excluded"][1][OPPORTUNITY_COLUMNS.index("Qualification Reasons")]
    )
    assert target.tabs["Manual Intake"] == [TAB_SCHEMAS["Manual Intake"].columns]
    migrated_dedupe = target.tabs["Dedupe Index"][1]
    assert migrated_dedupe[DEDUPE_COLUMNS.index("Primary Tab")] == "Excluded"
    assert migrated_dedupe[DEDUPE_COLUMNS.index("Primary Row")] == 2


def test_current_schema_migration_reconciles_paywalled_sources(config_dir) -> None:
    def opportunity_row(**values: object) -> list[object]:
        return [values.get(column, "") for column in OPPORTUNITY_COLUMNS]

    paywalled = opportunity_row(
        **{
            "Record ID": "paywalled-1",
            "Source Record ID": "remoteok-123",
            "Source": "remoteok",
            "Company": "Example Co",
            "Role": "Junior Developer",
            "Application Cost": "Not stated",
            "Eligibility": "Eligible",
            "Location Eligibility": "Eligible",
            "Work Arrangement": "remote",
            "Source URL": "https://remoteok.com/remote-jobs/123",
            "Apply URL": "https://remoteok.com/remote-jobs/123",
            "Retention Class": "excerpt",
            "Pipeline Stage": "Inbox",
            "Notes": "preserve this note",
        }
    )
    manual = opportunity_row(
        **{
            "Record ID": "manual-1",
            "Source Record ID": "manual-sheet-2",
            "Source": "remoteok",
            "Company": "Direct Employer",
            "Role": "Junior Developer",
            "Application Cost": "Free to apply",
            "Eligibility": "Eligible",
            "Location Eligibility": "Eligible",
            "Work Arrangement": "remote",
            "Source URL": "https://example.com/jobs/123",
            "Apply URL": "https://example.com/jobs/123",
            "Retention Class": "excerpt",
            "Pipeline Stage": "Shortlisted",
        }
    )
    tabs = initial_tab_values()
    tabs["Opportunities"] = [
        list(OPPORTUNITY_COLUMNS),
        paywalled,
        [""] * len(OPPORTUNITY_COLUMNS),
        manual,
    ]
    dedupe_row = [""] * len(DEDUPE_COLUMNS)
    dedupe_row[DEDUPE_COLUMNS.index("Record ID")] = "paywalled-1"
    dedupe_row[DEDUPE_COLUMNS.index("Primary Tab")] = "Opportunities"
    dedupe_row[DEDUPE_COLUMNS.index("Primary Row")] = 2
    tabs["Dedupe Index"].append(dedupe_row)
    source = WorkbookSnapshot(schema_version=SCHEMA_VERSION, spreadsheet_id="test", tabs=tabs)

    target, report = build_v3_migration(
        source,
        source_manifests=list(load_source_manifests(config_dir).values()),
    )

    assert report.opportunity_rows == 1
    assert "Moved 1 paywalled-source record" in report.warnings[0]
    assert target.tabs["Opportunities"][1][OPPORTUNITY_COLUMNS.index("Record ID")] == ("manual-1")
    excluded = target.tabs["Excluded"][1]
    assert excluded[OPPORTUNITY_COLUMNS.index("Record ID")] == "paywalled-1"
    assert excluded[OPPORTUNITY_COLUMNS.index("Application Cost")] == "Payment required"
    assert excluded[OPPORTUNITY_COLUMNS.index("Qualification")] == "Ineligible"
    assert excluded[OPPORTUNITY_COLUMNS.index("Notes")] == "preserve this note"
    assert target.tabs["Dedupe Index"][1][DEDUPE_COLUMNS.index("Primary Tab")] == "Excluded"
    assert target.tabs["Dedupe Index"][1][DEDUPE_COLUMNS.index("Primary Row")] == 2


def test_v32_presentation_cleanup_replaces_only_owned_objects() -> None:
    opportunity_id = 7
    dashboard_id = 8
    sheets = {
        "Opportunities": {
            "properties": {"title": "Opportunities", "sheetId": opportunity_id},
            "filterViews": [{"filterViewId": 310001}, {"filterViewId": 310008}],
            "bandedRanges": [
                {
                    "bandedRangeId": 4001,
                    "range": {
                        "sheetId": opportunity_id,
                        "startRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": len(OPPORTUNITY_COLUMNS),
                    },
                }
            ],
            "conditionalFormats": [
                {
                    "ranges": [
                        {
                            "sheetId": opportunity_id,
                            "startRowIndex": 1,
                            "endRowIndex": 1000,
                            "startColumnIndex": 0,
                            "endColumnIndex": 1,
                        }
                    ],
                    "booleanRule": {
                        "condition": {
                            "type": "TEXT_EQ",
                            "values": [{"userEnteredValue": "Inbox"}],
                        }
                    },
                },
                {
                    "ranges": [
                        {
                            "sheetId": opportunity_id,
                            "startRowIndex": 1,
                            "endRowIndex": 1000,
                            "startColumnIndex": 10,
                            "endColumnIndex": 11,
                        }
                    ],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": "=$K2<TODAY()"}],
                        }
                    },
                },
                {
                    "ranges": [{"sheetId": opportunity_id, "startRowIndex": 5}],
                    "booleanRule": {"condition": {"type": "BLANK"}},
                },
            ],
            "columnGroups": [
                {
                    "range": {
                        "sheetId": opportunity_id,
                        "dimension": "COLUMNS",
                        "startIndex": OPPORTUNITY_COLUMNS_V3_0.index("Blockers"),
                        "endIndex": len(OPPORTUNITY_COLUMNS),
                    },
                    "depth": 1,
                    "collapsed": True,
                }
            ],
        },
        "Dashboard": {
            "properties": {"title": "Dashboard", "sheetId": dashboard_id},
            "charts": [
                {"chartId": 5001, "spec": {"title": "Opportunity Pipeline"}},
                {
                    "chartId": 5002,
                    "spec": {"title": "Weekly Submissions and Responses"},
                },
                {"chartId": 5999, "spec": {"title": "My custom chart"}},
            ],
            "conditionalFormats": [
                {
                    "ranges": [
                        {
                            "sheetId": dashboard_id,
                            "startRowIndex": 4,
                            "endRowIndex": 5,
                            "startColumnIndex": 1,
                            "endColumnIndex": 2,
                        }
                    ],
                    "booleanRule": {
                        "condition": {
                            "type": "TEXT_EQ",
                            "values": [{"userEnteredValue": "OK"}],
                        }
                    },
                }
            ],
        },
    }

    requests, formatting_sheets = _presentation_cleanup_requests(sheets)

    _assert_sheets_requests_match_discovery(requests)
    kinds = [next(iter(request)) for request in requests]
    assert kinds.count("deleteConditionalFormatRule") == 3
    assert kinds.count("deleteFilterView") == 2
    assert kinds.count("deleteBanding") == 1
    assert kinds.count("deleteEmbeddedObject") == 2
    assert kinds.count("deleteDimensionGroup") == 1
    assert {
        request["deleteEmbeddedObject"]["objectId"]
        for request in requests
        if "deleteEmbeddedObject" in request
    } == {5001, 5002}
    assert formatting_sheets["Opportunities"]["columnGroups"] == []


def test_user_field_hash_treats_equivalent_sheet_dates_equally() -> None:
    iso_row = [""] * len(OPPORTUNITY_COLUMNS)
    python_row = [""] * len(OPPORTUNITY_COLUMNS)
    serial_row = [""] * len(OPPORTUNITY_COLUMNS)
    index = OPPORTUNITY_COLUMNS.index("Deadline")
    iso_row[index] = "2026-09-08"
    python_row[index] = date(2026, 9, 8)
    serial_row[index] = 46273.0

    assert user_fields_hash(iso_row) == user_fields_hash(python_row)
    assert user_fields_hash(python_row) == user_fields_hash(serial_row)


def test_google_bootstrap_does_nothing_when_schema_is_already_initialized(monkeypatch) -> None:
    metadata = {
        "sheets": [
            {"properties": {"title": title, "sheetId": index}}
            for index, title in enumerate(TAB_SCHEMAS, start=1)
        ]
    }
    initialized = WorkbookSnapshot(
        schema_version=SCHEMA_VERSION,
        spreadsheet_id="test-sheet",
        tabs=initial_tab_values(),
    )
    workbook = GoogleSheetsWorkbook("test-sheet", object(), service=object())
    monkeypatch.setattr(workbook, "_metadata", lambda: metadata)
    monkeypatch.setattr(workbook, "snapshot", lambda: initialized)
    monkeypatch.setattr(
        workbook,
        "_batch",
        lambda _requests: pytest.fail("initialized workbook should not be rewritten"),
    )

    workbook.ensure_schema()


def test_google_migration_verifies_queue_before_switching_dashboard(monkeypatch) -> None:
    tabs = initial_tab_values()
    tabs.pop("Opportunities")
    tabs["Technical Roles"] = [list(LEGACY_OPPORTUNITY_COLUMNS_V2)]
    tabs["VA & Freelance"] = [list(LEGACY_OPPORTUNITY_COLUMNS_V2)]
    source = WorkbookSnapshot(schema_version="2.0.0", spreadsheet_id="test-sheet", tabs=tabs)
    target, _ = build_v3_migration(source)
    titles = [*TAB_SCHEMAS, "Technical Roles", "VA & Freelance"]
    already_protected = {
        "Technical Roles",
        "VA & Freelance",
        "Source Config",
        "Lists & Enums",
        "Run Log",
        "Dedupe Index",
        "System Events",
    }

    def sheet_metadata(title: str, index: int) -> dict[str, object]:
        sheet: dict[str, object] = {
            "properties": {
                "title": title,
                "sheetId": index,
                "gridProperties": {
                    "columnCount": (len(OPPORTUNITY_COLUMNS) if title == "Opportunities" else 100),
                    "rowCount": 1000,
                },
            }
        }
        if title in already_protected:
            sheet["protectedRanges"] = [
                {
                    "protectedRangeId": 1000 + index,
                    "range": {"sheetId": index},
                    "unprotectedRanges": [
                        {
                            "sheetId": index,
                            "startRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": 1,
                        }
                    ],
                }
            ]
        return sheet

    metadata = {
        "sheets": [sheet_metadata(title, index) for index, title in enumerate(titles, start=1)]
    }
    batches: list[list[dict[str, object]]] = []
    workbook = GoogleSheetsWorkbook("test-sheet", object(), service=object())
    monkeypatch.setattr(workbook, "snapshot_existing", lambda: source)
    monkeypatch.setattr(
        workbook,
        "_read_sheet_values",
        lambda _sheets, **_kwargs: {
            "Opportunities": target.tabs["Opportunities"],
            "Excluded": target.tabs["Excluded"],
        },
    )
    monkeypatch.setattr(workbook, "_metadata", lambda: metadata)
    monkeypatch.setattr(workbook, "_batch", lambda requests: batches.append(requests))

    report = workbook.migrate_v3(write=True)

    assert report.target_version == SCHEMA_VERSION
    assert len(batches) == 3
    for batch in batches:
        _assert_sheets_requests_match_discovery(batch)
    assert len(batches[0]) == 1
    data_batch = batches[1]
    assert len(data_batch) == 4
    first_request = data_batch[2]["updateCells"]
    opportunities_id = titles.index("Opportunities") + 1
    assert first_request["range"]["sheetId"] == opportunities_id
    second_batch = batches[2]
    assert any(
        request.get("updateSheetProperties", {})
        .get("properties", {})
        .get("title", "")
        .endswith("(v2 archive)")
        for request in second_batch
    )
    assert any(
        request.get("updateCells", {}).get("fields") == "userEnteredValue"
        and set(request["updateCells"]["range"]) == {"sheetId"}
        for request in second_batch
    )
    updated_protections = [
        request["updateProtectedRange"]
        for request in second_batch
        if "updateProtectedRange" in request
    ]
    assert len(updated_protections) == len(already_protected)
    assert all(
        update["protectedRange"]["unprotectedRanges"] == [] for update in updated_protections
    )
    added_protections = [request for request in second_batch if "addProtectedRange" in request]
    assert len(added_protections) == 3
    assert opportunities_id in {
        request["addProtectedRange"]["protectedRange"]["range"]["sheetId"]
        for request in added_protections
    }


def test_google_v30_migration_reorders_and_reconciles_presentation(monkeypatch) -> None:
    tabs = initial_tab_values()
    old_row = [""] * len(OPPORTUNITY_COLUMNS_V3_0)
    old_row[OPPORTUNITY_COLUMNS_V3_0.index("Record ID")] = "record-31"
    old_row[OPPORTUNITY_COLUMNS_V3_0.index("Notes")] = "preserve me"
    tabs["Opportunities"] = [list(OPPORTUNITY_COLUMNS_V3_0), old_row]
    source = WorkbookSnapshot(schema_version="3.0.0", spreadsheet_id="test-sheet", tabs=tabs)
    target, _ = build_v3_migration(source)
    titles = list(TAB_SCHEMAS)
    opportunities_id = titles.index("Opportunities") + 1

    def sheet_metadata(title: str, index: int) -> dict[str, object]:
        sheet: dict[str, object] = {
            "properties": {
                "title": title,
                "sheetId": index,
                "gridProperties": {
                    "columnCount": len(OPPORTUNITY_COLUMNS) if title == "Opportunities" else 20,
                    "rowCount": 1000,
                },
            }
        }
        if title == "Opportunities":
            sheet["columnGroups"] = [
                {
                    "range": {
                        "sheetId": index,
                        "dimension": "COLUMNS",
                        "startIndex": OPPORTUNITY_COLUMNS_V3_0.index("Blockers"),
                        "endIndex": len(OPPORTUNITY_COLUMNS),
                    },
                    "depth": 1,
                    "collapsed": True,
                }
            ]
        return sheet

    metadata = {
        "sheets": [sheet_metadata(title, index) for index, title in enumerate(titles, start=1)]
    }
    batches: list[list[dict[str, object]]] = []
    workbook = GoogleSheetsWorkbook("test-sheet", object(), service=object())
    monkeypatch.setattr(workbook, "snapshot_existing", lambda: source)
    monkeypatch.setattr(
        workbook,
        "_read_sheet_values",
        lambda _sheets, **_kwargs: {
            "Opportunities": target.tabs["Opportunities"],
            "Excluded": target.tabs["Excluded"],
        },
    )
    monkeypatch.setattr(workbook, "_metadata", lambda: metadata)
    monkeypatch.setattr(workbook, "_batch", lambda requests: batches.append(requests))

    report = workbook.migrate_v3(write=True)

    assert report.source_version == "3.0.0"
    assert len(batches) == 3
    for batch in batches:
        _assert_sheets_requests_match_discovery(batch)
    assert batches[1][2]["updateCells"]["range"]["sheetId"] == opportunities_id
    final = batches[2]
    assert not any(
        request.get("updateSheetProperties", {})
        .get("properties", {})
        .get("title", "")
        .endswith("(v2 archive)")
        for request in final
    )
    assert any("deleteDimensionGroup" in request for request in final)
    added_group = next(
        request["addDimensionGroup"] for request in final if "addDimensionGroup" in request
    )
    assert added_group["range"]["startIndex"] == len(DAILY_OPPORTUNITY_COLUMNS)
    assert sum("addFilterView" in request for request in final) == 8
    assert any(
        request.get("updateSheetProperties", {})
        .get("properties", {})
        .get("gridProperties", {})
        .get("frozenColumnCount")
        == 3
        for request in final
    )


def test_google_v31_migration_expands_widened_support_tabs_before_writing(
    monkeypatch,
) -> None:
    tabs = initial_tab_values()
    tabs["Opportunities"] = [list(OPPORTUNITY_COLUMNS_V3_1)]
    source = WorkbookSnapshot(schema_version="3.1.0", spreadsheet_id="test-sheet", tabs=tabs)
    target, _ = build_v3_migration(source)
    titles = list(TAB_SCHEMAS)

    old_widths = {
        "Source Config": len(SOURCE_CONFIG_COLUMNS) - 1,
        "Run Log": len(RUN_LOG_COLUMNS) - 4,
    }
    metadata = {
        "sheets": [
            {
                "properties": {
                    "title": title,
                    "sheetId": index,
                    "gridProperties": {
                        "columnCount": old_widths.get(title, 100),
                        "rowCount": 1000,
                    },
                }
            }
            for index, title in enumerate(titles, start=1)
        ]
    }
    batches: list[list[dict[str, object]]] = []
    workbook = GoogleSheetsWorkbook("test-sheet", object(), service=object())
    monkeypatch.setattr(workbook, "snapshot_existing", lambda: source)
    monkeypatch.setattr(workbook, "_metadata", lambda: metadata)
    monkeypatch.setattr(
        workbook,
        "_read_sheet_values",
        lambda _sheets, **_kwargs: {
            "Opportunities": target.tabs["Opportunities"],
            "Excluded": target.tabs["Excluded"],
        },
    )
    monkeypatch.setattr(workbook, "_batch", lambda requests: batches.append(requests))

    workbook.migrate_v3(write=True)

    capacities = {
        sheet["properties"]["sheetId"]: sheet["properties"]["gridProperties"]["columnCount"]
        for sheet in metadata["sheets"]
    }
    expanded: dict[int, int] = {}
    for batch in batches:
        for request in batch:
            if "updateSheetProperties" in request:
                properties = request["updateSheetProperties"]["properties"]
                width = properties.get("gridProperties", {}).get("columnCount")
                if width is not None:
                    capacities[properties["sheetId"]] = width
                    expanded[properties["sheetId"]] = width
            update = request.get("updateCells")
            if not update or "endColumnIndex" not in update["range"]:
                continue
            sheet_id = update["range"]["sheetId"]
            start = update["range"].get("startColumnIndex", 0)
            end = update["range"]["endColumnIndex"]
            widest_row = max(
                (len(row.get("values", [])) for row in update.get("rows", [])),
                default=0,
            )
            assert start + widest_row <= end
            assert end <= capacities[sheet_id]

    source_config_id = titles.index("Source Config") + 1
    run_log_id = titles.index("Run Log") + 1
    assert expanded[source_config_id] == len(SOURCE_CONFIG_COLUMNS)
    assert expanded[run_log_id] == len(RUN_LOG_COLUMNS)


def test_google_transport_error_is_sanitized_but_actionable() -> None:
    assert _sanitized_google_error("metadata failed", TimeoutError("private detail")) == (
        "metadata failed: TimeoutError"
    )

    class Response:
        status = 400

    class ApiError(Exception):
        resp = Response()

        @staticmethod
        def _get_reason() -> str:
            return "Invalid request index 3"

    assert _sanitized_google_error("batch failed", ApiError()) == (
        "batch failed: HTTP 400: Invalid request index 3"
    )


def test_google_metadata_requests_only_valid_sheet_fields() -> None:
    observed: dict[str, object] = {}

    class Request:
        @staticmethod
        def execute(**kwargs: object) -> dict[str, list[object]]:
            observed["execute"] = kwargs
            return {"sheets": []}

    class Spreadsheets:
        def get(self, **kwargs: object) -> Request:
            observed.update(kwargs)
            return Request()

    class Service:
        @staticmethod
        def spreadsheets() -> Spreadsheets:
            return Spreadsheets()

    workbook = GoogleSheetsWorkbook("test-sheet", object(), service=Service())
    assert workbook._metadata() == {"sheets": []}
    fields = str(observed["fields"])
    assert "dimensionGroups" not in fields
    assert "columnGroups(range,depth,collapsed)" in fields
    assert "filterViews.filterViewId" in fields
    assert "protectedRanges(protectedRangeId,range" in fields
    assert "unprotectedRanges" in fields
    assert "conditionalFormats" in fields
    assert observed["execute"] == {"num_retries": GOOGLE_READ_RETRIES}


def test_opportunity_grouping_is_single_idempotent_and_conflict_safe() -> None:
    base_sheet: dict[str, Any] = {"properties": {"title": "Opportunities", "sheetId": 7}}
    start = len(DAILY_OPPORTUNITY_COLUMNS)
    end = len(OPPORTUNITY_COLUMNS)

    fresh = _collapsed_column_group_requests(base_sheet, start, end)
    assert [next(iter(request)) for request in fresh] == [
        "addDimensionGroup",
        "updateDimensionGroup",
    ]
    assert fresh[0]["addDimensionGroup"]["range"] == {
        "sheetId": 7,
        "dimension": "COLUMNS",
        "startIndex": start,
        "endIndex": end,
    }
    _assert_sheets_requests_match_discovery(fresh)

    exact_expanded = {
        **base_sheet,
        "columnGroups": [
            {
                "range": {
                    "sheetId": 7,
                    "dimension": "COLUMNS",
                    "startIndex": start,
                    "endIndex": end,
                },
                "depth": 2,
                "collapsed": False,
            }
        ],
    }
    update_only = _collapsed_column_group_requests(exact_expanded, start, end)
    assert len(update_only) == 1
    assert update_only[0]["updateDimensionGroup"]["dimensionGroup"]["depth"] == 2
    _assert_sheets_requests_match_discovery(update_only)

    exact_collapsed = exact_expanded | {
        "columnGroups": [{**exact_expanded["columnGroups"][0], "collapsed": True}]
    }
    assert _collapsed_column_group_requests(exact_collapsed, start, end) == []

    touching_custom_group = {
        **base_sheet,
        "columnGroups": [
            {
                "range": {
                    "sheetId": 7,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": start,
                },
                "depth": 1,
                "collapsed": False,
            }
        ],
    }
    with pytest.raises(WorkbookConflict, match="existing column group"):
        _collapsed_column_group_requests(touching_custom_group, start, end)


def test_google_snapshot_reads_metadata_bounded_ranges_with_safe_retries(monkeypatch) -> None:
    observed: dict[str, object] = {}
    metadata = {
        "sheets": [
            {
                "properties": {
                    "title": "Technical Roles",
                    "sheetId": 7,
                    "gridProperties": {"rowCount": 1000, "columnCount": 69},
                }
            },
            {
                "properties": {
                    "title": "Dashboard",
                    "sheetId": 8,
                    "gridProperties": {"rowCount": 40, "columnCount": 30},
                }
            },
        ]
    }

    class Request:
        @staticmethod
        def execute(**kwargs: object) -> dict[str, object]:
            observed["execute"] = kwargs
            return {
                "valueRanges": [
                    {"values": [list(LEGACY_OPPORTUNITY_COLUMNS_V2)]},
                    {"values": [["JOB HUNT ASSISTANT"]]},
                ]
            }

    class Values:
        def batchGet(self, **kwargs: object) -> Request:
            observed["batch_get"] = kwargs
            return Request()

    class Spreadsheets:
        @staticmethod
        def values() -> Values:
            return Values()

    class Service:
        @staticmethod
        def spreadsheets() -> Spreadsheets:
            return Spreadsheets()

    workbook = GoogleSheetsWorkbook("test-sheet", object(), service=Service())
    monkeypatch.setattr(workbook, "_metadata", lambda: metadata)

    snapshot = workbook.snapshot_existing()

    assert snapshot.schema_version == "2.0.0"
    request = observed["batch_get"]
    assert isinstance(request, dict)
    assert request["ranges"] == [
        "'Technical Roles'!A1:BQ1000",
        "'Dashboard'!A1:AD40",
    ]
    assert observed["execute"] == {"num_retries": GOOGLE_READ_RETRIES}


def test_google_client_uses_bounded_transport_timeout(monkeypatch) -> None:
    observed: dict[str, object] = {}
    service = object()

    class Transport:
        pass

    transport = Transport()
    authorized = object()

    def fake_http(*, timeout: int) -> Transport:
        observed["timeout"] = timeout
        return transport

    def fake_authorized_http(credentials: object, *, http: object) -> object:
        observed["credentials"] = credentials
        observed["transport"] = http
        return authorized

    def fake_build(*args: object, **kwargs: object) -> object:
        observed["build_args"] = args
        observed["build_kwargs"] = kwargs
        return service

    monkeypatch.setattr(httplib2, "Http", fake_http)
    monkeypatch.setattr(google_auth_httplib2, "AuthorizedHttp", fake_authorized_http)
    monkeypatch.setattr(googleapiclient.discovery, "build", fake_build)
    credentials = object()

    workbook = GoogleSheetsWorkbook("test-sheet", credentials)

    assert workbook._service is service
    assert observed["timeout"] == GOOGLE_HTTP_TIMEOUT_SECONDS
    assert observed["credentials"] is credentials
    assert observed["transport"] is transport
    assert observed["build_args"] == ("sheets", "v4")
    assert observed["build_kwargs"] == {
        "http": authorized,
        "cache_discovery": False,
    }

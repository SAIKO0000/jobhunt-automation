from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobhunt.backup import LocalSnapshotStore
from jobhunt.lease import FileRunLease, LeaseUnavailable
from jobhunt.pipeline import Pipeline
from jobhunt.workbook.gateway import LocalJsonWorkbook
from jobhunt.workbook.schema import MANUAL_INTAKE_COLUMNS, OPPORTUNITY_COLUMNS


def test_file_lease_blocks_overlap_and_recovers_expired_lock(tmp_path) -> None:
    path = tmp_path / "run-lease.json"
    lease = FileRunLease(path)
    lease.acquire("first", timedelta(minutes=5))
    with pytest.raises(LeaseUnavailable):
        lease.acquire("second", timedelta(minutes=5))
    lease.release("first")

    path.write_text(
        json.dumps(
            {
                "run_id": "abandoned",
                "expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    lease.acquire("recovered", timedelta(minutes=5))
    lease.release("recovered")


def test_fixture_pipeline_dry_run_has_no_workbook_data_writes(tmp_path, config_dir) -> None:
    workbook = LocalJsonWorkbook(tmp_path / "workbook.json")
    pipeline = Pipeline(
        config_dir=config_dir,
        workbook=workbook,
        snapshot_store=LocalSnapshotStore(tmp_path / "snapshots"),
        lease=FileRunLease(tmp_path / "lease.json"),
    )
    output = pipeline.run(write=False, fixture_dir=Path("tests/fixtures"))
    assert output.run.status == "success"
    assert output.run.records_fetched == 9
    assert len(output.opportunities) == 9
    assert all(item.source_record.expires_at for item in output.opportunities)
    assert output.snapshot_identifier is None
    assert not (tmp_path / "workbook.json").exists()
    assert not (tmp_path / "lease.json").exists()


def test_fixture_pipeline_write_is_idempotent_and_snapshotted(tmp_path, config_dir) -> None:
    workbook = LocalJsonWorkbook(tmp_path / "workbook.json")
    snapshots = LocalSnapshotStore(tmp_path / "snapshots")
    pipeline = Pipeline(
        config_dir=config_dir,
        workbook=workbook,
        snapshot_store=snapshots,
        lease=FileRunLease(tmp_path / "lease.json"),
    )
    first = pipeline.run(write=True, fixture_dir=Path("tests/fixtures"))
    assert first.snapshot_identifier
    first_snapshot = workbook.snapshot()
    first_count = len(first_snapshot.tabs["Opportunities"])

    second = pipeline.run(write=True, fixture_dir=Path("tests/fixtures"))
    second_snapshot = workbook.snapshot()
    second_count = len(second_snapshot.tabs["Opportunities"])
    assert second.snapshot_identifier
    assert second_count == first_count
    assert len(second_snapshot.tabs["Run Log"]) == 3


def test_manual_intake_routes_free_role_to_queue_and_upwork_to_excluded(
    tmp_path, config_dir
) -> None:
    workbook = LocalJsonWorkbook(tmp_path / "workbook.json")
    workbook.ensure_schema()
    snapshot = workbook.snapshot()

    def intake_row(**values: object) -> list[object]:
        return [values.get(column, "") for column in MANUAL_INTAKE_COLUMNS]

    description = "Build Python, TypeScript, React, Next.js workflow dashboards for internal tools."
    snapshot.tabs["Manual Intake"].extend(
        [
            intake_row(
                Platform="LinkedIn",
                **{
                    "Listing URL": "https://www.linkedin.com/jobs/view/123",
                    "Company": "Example One",
                    "Role": "Junior Full Stack Developer",
                    "Location": "Remote - Philippines",
                    "Work Arrangement": "remote",
                    "Application Cost": "Free to apply",
                    "Description / Requirements": description,
                },
            ),
            intake_row(
                Platform="Upwork",
                **{
                    "Listing URL": "https://www.upwork.com/jobs/example",
                    "Company": "Example Two",
                    "Role": "Junior Automation Developer",
                    "Location": "Remote - Worldwide",
                    "Work Arrangement": "remote",
                    "Description / Requirements": description,
                },
            ),
        ]
    )
    (tmp_path / "workbook.json").write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    pipeline = Pipeline(
        config_dir=config_dir,
        workbook=workbook,
        snapshot_store=LocalSnapshotStore(tmp_path / "snapshots"),
        lease=FileRunLease(tmp_path / "lease.json"),
    )

    output = pipeline.run(write=True)
    current = workbook.snapshot()

    assert output.run.records_fetched == 2
    assert output.run.records_actionable == 1
    assert output.run.records_excluded == 1
    assert len(current.tabs["Opportunities"]) == 2
    assert len(current.tabs["Excluded"]) == 2
    status_index = MANUAL_INTAKE_COLUMNS.index("Import Status")
    assert [row[status_index] for row in current.tabs["Manual Intake"][1:]] == [
        "Imported",
        "Imported",
    ]
    assert (
        current.tabs["Opportunities"][1][OPPORTUNITY_COLUMNS.index("Application Cost")]
        == "Free to apply"
    )
    assert (
        current.tabs["Excluded"][1][OPPORTUNITY_COLUMNS.index("Application Cost")]
        == "Payment required"
    )

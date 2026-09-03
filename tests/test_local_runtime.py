from __future__ import annotations

import sys
from pathlib import Path

import pytest

from jobhunt.backup import LocalSnapshotStore
from jobhunt.cli import main
from jobhunt.credentials import GEMINI_KEY_NAME, MemoryCredentialStore, WindowsCredentialStore
from jobhunt.google_auth import SHEETS_SCOPE, authorize_google, load_google_credentials
from jobhunt.lease import FileRunLease
from jobhunt.models import WorkbookSnapshot
from jobhunt.pending import PendingRunStore
from jobhunt.pipeline import Pipeline
from jobhunt.workbook.gateway import LocalJsonWorkbook, WorkbookConflict
from jobhunt.workbook.schema import (
    LEGACY_ARCHIVE_TABS,
    LEGACY_OPPORTUNITY_COLUMNS_V2,
    OPPORTUNITY_COLUMNS,
    SCHEMA_VERSION,
    initial_tab_values,
)


def test_non_windows_credential_backend_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="Windows Credential Manager"):
        WindowsCredentialStore()


def test_google_oauth_requests_exact_scope_and_stores_no_token_file(tmp_path, monkeypatch) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    observed: dict[str, object] = {}
    secret_file = tmp_path / "desktop-client.json"
    secret_file.write_text("{}", encoding="utf-8")

    class Credentials:
        def __init__(self) -> None:
            self.refresh_token = "refresh"  # noqa: S105 - synthetic fixture
            self.client_id = "client"
            self.client_secret = "secret"  # noqa: S105 - synthetic fixture
            self.token_uri = "https://oauth2.googleapis.com/token"  # noqa: S105
            self.scopes = [SHEETS_SCOPE]

    class Flow:
        def run_local_server(self, **kwargs):
            observed.update(kwargs)
            return Credentials()

    def factory(path, scopes):
        observed["path"] = path
        observed["scopes"] = scopes
        return Flow()

    monkeypatch.setattr(InstalledAppFlow, "from_client_secrets_file", factory)
    store = MemoryCredentialStore()
    before = {path.name for path in tmp_path.iterdir()}
    authorize_google(secret_file, store)
    after = {path.name for path in tmp_path.iterdir()}
    assert observed["scopes"] == [SHEETS_SCOPE]
    assert observed["host"] == "127.0.0.1"
    assert observed["port"] == 0
    assert before == after
    credentials = load_google_credentials(store)
    assert credentials.scopes == [SHEETS_SCOPE]


def test_credential_command_never_prints_secret(monkeypatch, capsys) -> None:
    class Store(MemoryCredentialStore):
        backend_name = "secure-test-backend"

    store = Store()
    monkeypatch.setattr("jobhunt.cli.WindowsCredentialStore", lambda: store)
    monkeypatch.setattr("jobhunt.cli.getpass.getpass", lambda _: "super-secret-key")
    assert main(["credentials", "set-gemini"]) == 0
    output = capsys.readouterr().out
    assert "super-secret-key" not in output
    assert store.get(GEMINI_KEY_NAME) == "super-secret-key"


class _FailingWorkbook(LocalJsonWorkbook):
    def commit(self, initial, opportunities, run, manifests):
        raise TimeoutError("ambiguous Sheets timeout")


def test_failed_commit_retains_pending_run_and_snapshot(tmp_path, config_dir) -> None:
    workbook = _FailingWorkbook(tmp_path / "workbook.json")
    pending = PendingRunStore(tmp_path / "pending")
    pipeline = Pipeline(
        config_dir=config_dir,
        workbook=workbook,
        snapshot_store=LocalSnapshotStore(tmp_path / "snapshots"),
        lease=FileRunLease(tmp_path / "lease.json"),
        pending_store=pending,
    )
    with pytest.raises(TimeoutError):
        pipeline.run(write=True, fixture_dir=Path("tests/fixtures"))
    files = list((tmp_path / "pending").glob("*.json"))
    assert len(files) == 1
    assert list((tmp_path / "snapshots").glob("*.json"))
    retained = pending.load(files[0].stem)
    assert retained.opportunities
    assert retained.run.status == "success"


def test_schema_migration_maps_human_fields_by_header(tmp_path) -> None:
    path = tmp_path / "workbook.json"
    tabs = initial_tab_values()
    tabs.pop("Opportunities")
    old_header = list(LEGACY_OPPORTUNITY_COLUMNS_V2)
    row = [""] * len(old_header)
    row[old_header.index("Record ID")] = "record-id"
    row[old_header.index("Company")] = "Example Co"
    row[old_header.index("Title")] = "Automation Engineer"
    row[old_header.index("Opportunity Type")] = "Technical Role"
    row[old_header.index("Review Status")] = "New"
    row[old_header.index("Reviewer Notes")] = "keep this human note"
    tabs["Technical Roles"] = [old_header, row]
    tabs["VA & Freelance"] = [old_header]
    path.write_text(
        WorkbookSnapshot(
            schema_version="1.0.0", spreadsheet_id="local-workbook", tabs=tabs
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )
    workbook = LocalJsonWorkbook(path)
    with pytest.raises(WorkbookConflict, match="migrate-workbook"):
        workbook.ensure_schema()
    preview = workbook.migrate_v3(write=False)
    assert preview.opportunity_rows == 1
    assert workbook.snapshot().schema_version == "1.0.0"
    workbook.migrate_v3(write=True)
    migrated = workbook.snapshot()
    assert migrated.schema_version == SCHEMA_VERSION
    rows = migrated.tabs["Opportunities"]
    assert rows[0] == OPPORTUNITY_COLUMNS
    assert rows[1][OPPORTUNITY_COLUMNS.index("Notes")] == "keep this human note"
    assert rows[1][OPPORTUNITY_COLUMNS.index("Role")] == "Automation Engineer"
    assert rows[1][OPPORTUNITY_COLUMNS.index("Track")] == "Technical"
    assert set(LEGACY_ARCHIVE_TABS.values()).issubset(migrated.tabs)


def test_schema_migration_rejects_populated_custom_columns(tmp_path) -> None:
    path = tmp_path / "workbook.json"
    tabs = initial_tab_values()
    tabs.pop("Opportunities")
    header = [*LEGACY_OPPORTUNITY_COLUMNS_V2, "My Custom Field"]
    row = [""] * len(header)
    row[header.index("Record ID")] = "record-id"
    row[-1] = "must not be lost"
    tabs["Technical Roles"] = [header, row]
    path.write_text(
        WorkbookSnapshot(
            schema_version="2.0.0", spreadsheet_id="local-workbook", tabs=tabs
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )

    with pytest.raises(WorkbookConflict, match="custom columns"):
        LocalJsonWorkbook(path).migrate_v3(write=False)


def test_schema_migration_rejects_conflicting_duplicate_records(tmp_path) -> None:
    path = tmp_path / "workbook.json"
    tabs = initial_tab_values()
    tabs.pop("Opportunities")
    header = list(LEGACY_OPPORTUNITY_COLUMNS_V2)
    technical = [""] * len(header)
    freelance = [""] * len(header)
    for row in (technical, freelance):
        row[header.index("Record ID")] = "same-id"
    technical[header.index("Reviewer Notes")] = "technical note"
    freelance[header.index("Reviewer Notes")] = "different note"
    tabs["Technical Roles"] = [header, technical]
    tabs["VA & Freelance"] = [header, freelance]
    path.write_text(
        WorkbookSnapshot(
            schema_version="2.0.0", spreadsheet_id="local-workbook", tabs=tabs
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )

    with pytest.raises(WorkbookConflict, match="Conflicting values"):
        LocalJsonWorkbook(path).migrate_v3(write=False)


def test_scheduler_scripts_encode_required_safety_settings() -> None:
    register = Path("scripts/register-jobhunt-task.ps1").read_text(encoding="utf-8")
    runner = Path("scripts/run-jobhunt.ps1").read_text(encoding="utf-8")
    for expected in (
        '"07:17"',
        "StartWhenAvailable",
        "IgnoreNew",
        "ExecutionTimeLimit",
        "RestartCount 1",
        "RestartInterval",
        "RunOnlyIfNetworkAvailable",
        "Interactive",
    ):
        assert expected in register
    assert '"19:17"' not in register
    assert "--summary-only" in runner
    assert "--spreadsheet-id" in runner


def test_no_active_paid_or_hosted_runtime_paths() -> None:
    roots = [Path("src"), Path("config"), Path("scripts"), Path(".github/workflows")]
    forbidden = (
        "open" + "ai",
        "cloud" + " run",
        "google-cloud-" + "storage",
        "g" + "cs",
        "n" + "8n",
    )
    scanned_files = [
        path
        for root in roots
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and not any(part.endswith(".egg-info") for part in path.parts)
    ]
    matches = {
        value: [
            path.as_posix()
            for path in scanned_files
            if value in path.read_text(encoding="utf-8", errors="ignore").casefold()
        ]
        for value in forbidden
    }
    matches = {value: paths for value, paths in matches.items() if paths}
    assert not matches, f"Found retired runtime references: {matches}"


def test_no_committed_secret_shapes() -> None:
    import re

    roots = [Path("src"), Path("config"), Path("scripts"), Path("docs"), Path("tests")]
    scanned = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for root in roots
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    patterns = (
        r"AIza[0-9A-Za-z_-]{25,}",
        r"sk-[0-9A-Za-z_-]{20,}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    )
    assert not any(re.search(pattern, scanned) for pattern in patterns)

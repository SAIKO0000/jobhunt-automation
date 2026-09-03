from __future__ import annotations

import json
import shutil

from jobhunt.cli import main
from jobhunt.models import WorkbookSnapshot
from jobhunt.workbook.schema import LEGACY_OPPORTUNITY_COLUMNS_V2, initial_tab_values


def test_cli_config_ingest_score_and_lead(capsys) -> None:
    assert main(["--config-dir", "config", "validate-config"]) == 0
    assert "Configuration valid" in capsys.readouterr().out

    assert (
        main(
            [
                "--config-dir",
                "config",
                "ingest",
                "--source",
                "remoteok",
                "--fixture",
                "tests/fixtures/remoteok.json",
            ]
        )
        == 0
    )
    assert "remoteok-1" in capsys.readouterr().out

    assert (
        main(
            [
                "--config-dir",
                "config",
                "score",
                "--record",
                "tests/fixtures/manual_record.json",
            ]
        )
        == 0
    )
    assert "manual-score-1" in capsys.readouterr().out

    assert main(["lead", "--input", "tests/fixtures/lead.json"]) == 0
    assert "sent manually" in capsys.readouterr().out


def test_cli_dry_run_and_ai_dry_run_guard(capsys, tmp_path) -> None:
    assert (
        main(
            [
                "--config-dir",
                "config",
                "run",
                "--dry-run",
                "--fixture-dir",
                "tests/fixtures",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["run"]["records_fetched"] == 9

    assert main(["run", "--dry-run", "--enable-ai"]) == 2
    assert "dry runs never call a provider" in capsys.readouterr().err

    disabled_config = tmp_path / "disabled-config"
    shutil.copytree("config", disabled_config)
    sources_path = disabled_config / "sources.json"
    sources = json.loads(sources_path.read_text(encoding="utf-8"))
    for source in sources:
        source["enabled"] = False
        source["owner_approved"] = False
    sources_path.write_text(json.dumps(sources), encoding="utf-8")

    assert (
        main(
            [
                "--config-dir",
                str(disabled_config),
                "run",
                "--dry-run",
                "--live",
            ]
        )
        == 2
    )
    assert "enabled and owner-approved" in capsys.readouterr().err


def test_cli_local_workbook_backup_and_restore(tmp_path, monkeypatch, capsys) -> None:
    source_runtime = tmp_path / "source-runtime"
    monkeypatch.setenv("JOBHUNT_RUNTIME_DIR", str(source_runtime))
    assert main(["bootstrap-workbook", "--backend", "local"]) == 0
    capsys.readouterr()
    assert main(["validate-workbook", "--backend", "local"]) == 0
    capsys.readouterr()

    backup = tmp_path / "exported.json"
    assert (
        main(
            [
                "export-backup",
                "--backend",
                "local",
                "--output",
                str(backup),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert backup.exists()

    destination_runtime = tmp_path / "destination-runtime"
    monkeypatch.setenv("JOBHUNT_RUNTIME_DIR", str(destination_runtime))
    assert (
        main(
            [
                "restore-backup",
                "--backend",
                "local",
                "--input",
                str(backup),
                "--blank-only",
            ]
        )
        == 0
    )
    assert "restored" in capsys.readouterr().out.casefold()
    assert (destination_runtime / "workbook.json").exists()


def test_cli_migration_previews_before_writing_and_snapshots(tmp_path, monkeypatch, capsys) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("JOBHUNT_RUNTIME_DIR", str(runtime))
    tabs = initial_tab_values()
    tabs.pop("Opportunities")
    tabs["Technical Roles"] = [list(LEGACY_OPPORTUNITY_COLUMNS_V2)]
    tabs["VA & Freelance"] = [list(LEGACY_OPPORTUNITY_COLUMNS_V2)]
    path = runtime / "workbook.json"
    path.write_text(
        WorkbookSnapshot(
            schema_version="2.0.0", spreadsheet_id="local-workbook", tabs=tabs
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )

    assert main(["migrate-workbook", "--backend", "local"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "preview"
    unchanged = WorkbookSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    assert unchanged.schema_version == "2.0.0"
    assert main(["migrate-workbook", "--backend", "local", "--write"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "write"
    assert list((runtime / "snapshots").glob("*.json"))

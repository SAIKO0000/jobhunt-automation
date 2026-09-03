from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jobhunt.adapters import adapter_registry
from jobhunt.ai import AnalysisOrchestrator, AnalysisProvider, GeminiAnalyzer, OllamaAnalyzer
from jobhunt.backup import LocalSnapshotStore
from jobhunt.budget import FileAnalysisQuotaLedger
from jobhunt.config import (
    load_analysis_config,
    load_candidate_profile,
    load_location_policy,
    load_profile_claims,
    load_search_preferences,
    load_source_manifests,
    validate_all,
)
from jobhunt.credentials import GEMINI_KEY_NAME, GOOGLE_CREDENTIAL_NAMES, WindowsCredentialStore
from jobhunt.dedupe import build_opportunities
from jobhunt.eligibility import combine_eligibility, evaluate_eligibility
from jobhunt.evaluation import evaluate_provider, load_evaluation_fixtures
from jobhunt.google_auth import authorize_google, load_google_credentials
from jobhunt.leads import assess_lead
from jobhunt.lease import FileRunLease, LeaseContext
from jobhunt.models import Lead, RunResult, SourceKind, WorkbookSnapshot
from jobhunt.pending import PendingRunStore
from jobhunt.pipeline import Pipeline
from jobhunt.qualification import assess_candidate_qualification
from jobhunt.scoring import score_opportunity
from jobhunt.workbook import GoogleSheetsWorkbook, LocalJsonWorkbook, WorkbookGateway
from jobhunt.workbook.gateway import run_is_logged


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (ValueError, PermissionError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobhunt")
    parser.add_argument(
        "--config-dir", default=os.getenv("JOBHUNT_CONFIG_DIR", "config"), type=Path
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-config", help="Validate deny-by-default config")
    validate.set_defaults(handler=_validate_config)

    run = subparsers.add_parser("run", help="Run the opportunity pipeline")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Do not update the workbook")
    mode.add_argument("--write", action="store_true", help="Commit workbook updates")
    run.add_argument("--fixture-dir", type=Path)
    run.add_argument("--live", action="store_true", help="Use enabled, owner-approved sources")
    run.add_argument(
        "--enable-ai", action="store_true", help="Permit configured local/free analysis"
    )
    run.add_argument(
        "--summary-only", action="store_true", help="Print only sanitized run metadata"
    )
    _add_workbook_args(run)
    run.set_defaults(handler=_run)

    ingest = subparsers.add_parser("ingest", help="Parse one source fixture without writes")
    ingest.add_argument("--source", choices=[item.value for item in SourceKind], required=True)
    ingest.add_argument("--fixture", type=Path, required=True)
    ingest.set_defaults(handler=_ingest)

    score = subparsers.add_parser("score", help="Score a manually supplied SourceRecord JSON")
    score.add_argument("--record", type=Path, required=True)
    score.set_defaults(handler=_score)

    for name, handler, help_text in (
        ("bootstrap-workbook", _bootstrap_workbook, "Create workbook tabs/schema"),
        ("validate-workbook", _validate_workbook, "Validate workbook schema"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        _add_workbook_args(command)
        command.set_defaults(handler=handler)

    migrate = subparsers.add_parser(
        "migrate-workbook", help="Preview or apply the explicit workbook schema migration"
    )
    _add_workbook_args(migrate)
    migrate.add_argument(
        "--write", action="store_true", help="Apply the migration; preview is the default"
    )
    migrate.set_defaults(handler=_migrate_workbook)

    export = subparsers.add_parser("export-backup", help="Export a normalized snapshot")
    _add_workbook_args(export)
    export.add_argument("--output", type=Path, required=True)
    export.set_defaults(handler=_export_backup)

    restore = subparsers.add_parser("restore-backup", help="Restore into a blank workbook")
    _add_workbook_args(restore)
    restore.add_argument("--input", type=Path, required=True)
    restore.add_argument("--blank-only", action=argparse.BooleanOptionalAction, default=True)
    restore.set_defaults(handler=_restore_backup)

    replay = subparsers.add_parser("replay-pending", help="Safely replay a retained Sheets write")
    replay.add_argument("--run-id", required=True)
    _add_workbook_args(replay)
    replay.set_defaults(handler=_replay_pending)

    evaluate = subparsers.add_parser(
        "evaluate-ollama", help="Evaluate an already-installed local model; never downloads"
    )
    evaluate.add_argument("--fixtures", type=Path, default=Path("tests/fixtures/ollama_eval.json"))
    evaluate.set_defaults(handler=_evaluate_ollama)

    lead = subparsers.add_parser("lead", help="Assess one user-seeded lead; never sends")
    lead.add_argument("--input", type=Path, required=True)
    lead.add_argument("--write", action="store_true")
    _add_workbook_args(lead)
    lead.set_defaults(handler=_lead)

    auth = subparsers.add_parser("auth-google", help="Authorize Sheets-only desktop OAuth")
    auth.add_argument("--client-secrets", type=Path, required=True)
    auth.set_defaults(handler=_auth_google)

    credentials = subparsers.add_parser("credentials", help="Manage local secure credentials")
    credentials_sub = credentials.add_subparsers(dest="credential_command", required=True)
    set_gemini = credentials_sub.add_parser("set-gemini")
    set_gemini.set_defaults(handler=_credentials_set_gemini)
    status = credentials_sub.add_parser("status")
    status.add_argument("--require-google", action="store_true")
    status.add_argument("--require-gemini", action="store_true")
    status.set_defaults(handler=_credentials_status)
    delete_google = credentials_sub.add_parser("delete-google")
    delete_google.set_defaults(handler=_credentials_delete_google)
    delete_gemini = credentials_sub.add_parser("delete-gemini")
    delete_gemini.set_defaults(handler=_credentials_delete_gemini)
    return parser


def _add_workbook_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend", choices=("local", "google"), default="local")
    parser.add_argument("--spreadsheet-id")


def _runtime_dir() -> Path:
    return Path(os.getenv("JOBHUNT_RUNTIME_DIR", "runtime"))


def _validate_config(args: argparse.Namespace) -> int:
    diagnostics = validate_all(args.config_dir)
    print("Configuration valid")
    for item in diagnostics:
        print(f"{item.level}: {item.message}")
    return 0


def _run(args: argparse.Namespace) -> int:
    if args.enable_ai and not args.write:
        raise PermissionError("AI analysis requires --write; dry runs never call a provider")
    runtime_dir = _runtime_dir()
    workbook = _workbook(args, runtime_dir)
    analyzer = _analysis_provider(args, runtime_dir)
    pipeline = Pipeline(
        config_dir=args.config_dir,
        workbook=workbook,
        snapshot_store=LocalSnapshotStore(runtime_dir / "snapshots"),
        lease=FileRunLease(runtime_dir / "run-lease.json"),
        ai_analyzer=analyzer,
        pending_store=PendingRunStore(runtime_dir / "pending"),
    )
    try:
        output = pipeline.run(
            write=bool(args.write),
            fixture_dir=args.fixture_dir,
            allow_live_sources=bool(args.live),
            trigger=os.getenv("JOBHUNT_TRIGGER", "manual"),
            image_digest="local",
        )
    finally:
        if analyzer:
            analyzer.close()
    payload: dict[str, Any] = {
        "run": output.run.model_dump(mode="json"),
        "snapshot": output.snapshot_identifier,
        "dry_run": not bool(args.write),
    }
    if not args.summary_only:
        payload["opportunities"] = [item.model_dump(mode="json") for item in output.opportunities]
    print(json.dumps(payload, indent=2))
    return 0 if output.run.status == "success" else 1


def _ingest(args: argparse.Namespace) -> int:
    source = SourceKind(args.source)
    manifest = load_source_manifests(args.config_dir)[source]
    text = args.fixture.read_text(encoding="utf-8")
    payload: Any = text if args.fixture.suffix.casefold() == ".xml" else json.loads(text)
    print(adapter_registry()[source].parse_payload(payload, manifest).model_dump_json(indent=2))
    return 0


def _score(args: argparse.Namespace) -> int:
    payload = json.loads(args.record.read_text(encoding="utf-8"))
    manifest = load_source_manifests(args.config_dir)[SourceKind.MANUAL]
    batch = adapter_registry()[SourceKind.MANUAL].parse_payload(payload, manifest)
    if not batch.records:
        raise ValueError("No valid manual record found")
    opportunity = build_opportunities(batch.records)[0]
    eligibility = evaluate_eligibility(
        opportunity.source_record, load_location_policy(args.config_dir)
    )
    qualification = assess_candidate_qualification(
        opportunity.source_record, load_candidate_profile(args.config_dir)
    )
    opportunity = opportunity.model_copy(
        update={
            "location_band": eligibility.location_band,
            "location_decision": eligibility.location_decision,
            "eligibility_decision": combine_eligibility(
                eligibility.eligibility_decision, qualification.decision
            ),
            "hard_fail_reasons": [
                *eligibility.hard_fail_reasons,
                *qualification.reasons,
            ],
            "qualification_decision": qualification.decision,
            "seniority_level": qualification.seniority_level,
            "mandatory_experience_years": qualification.mandatory_experience_years,
            "applicant_cost_decision": qualification.applicant_cost_decision,
            "compensation_decision": qualification.compensation_decision,
            "qualification_reason_codes": qualification.reason_codes,
            "qualification_reasons": qualification.reasons,
        }
    )
    opportunity = score_opportunity(
        opportunity,
        load_search_preferences(args.config_dir),
        load_profile_claims(args.config_dir, verified_only=True),
    )
    print(opportunity.model_dump_json(indent=2))
    return 0


def _bootstrap_workbook(args: argparse.Namespace) -> int:
    workbook = _workbook(args, _runtime_dir())
    LocalSnapshotStore(_runtime_dir() / "snapshots").save(
        workbook.snapshot_existing(), reason="pre-schema-bootstrap"
    )
    workbook.ensure_schema()
    print("Workbook schema ready")
    return 0


def _validate_workbook(args: argparse.Namespace) -> int:
    issues = _workbook(args, _runtime_dir()).validate()
    for issue in issues:
        print(issue if issue.startswith("warning: ") else f"error: {issue}")
    errors = [issue for issue in issues if not issue.startswith("warning: ")]
    if not issues:
        print("Workbook valid")
    return int(bool(errors))


def _migrate_workbook(args: argparse.Namespace) -> int:
    runtime_dir = _runtime_dir()
    workbook = _workbook(args, runtime_dir)
    source = None
    if args.write:
        source = workbook.snapshot_existing()
        LocalSnapshotStore(runtime_dir / "snapshots").save(source, reason="pre-schema-v3-migration")
    report = workbook.migrate_v3(
        write=bool(args.write),
        source=source,
        candidate_profile=load_candidate_profile(args.config_dir),
        source_manifests=list(load_source_manifests(args.config_dir).values()),
    )
    payload = report.as_dict()
    payload["mode"] = "write" if args.write else "preview"
    print(json.dumps(payload, indent=2))
    return 0


def _export_backup(args: argparse.Namespace) -> int:
    snapshot = _workbook(args, _runtime_dir()).snapshot()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    print(str(args.output))
    return 0


def _restore_backup(args: argparse.Namespace) -> int:
    workbook = _workbook(args, _runtime_dir())
    snapshot = WorkbookSnapshot.model_validate_json(args.input.read_text(encoding="utf-8"))
    workbook.restore(snapshot, blank_only=bool(args.blank_only))
    print("Snapshot restored")
    return 0


def _replay_pending(args: argparse.Namespace) -> int:
    runtime_dir = _runtime_dir()
    store = PendingRunStore(runtime_dir / "pending")
    pending = store.load(args.run_id)
    workbook = _workbook(args, runtime_dir)
    workbook.ensure_schema()
    current = workbook.snapshot()
    if run_is_logged(current, str(pending.run.run_id)):
        store.delete(args.run_id)
        print("Run was already committed; removed stale pending file")
        return 0
    LocalSnapshotStore(runtime_dir / "snapshots").save(
        current, reason=f"pre-replay-{pending.run.run_id}"
    )
    workbook.commit(pending.initial_snapshot, pending.opportunities, pending.run, pending.manifests)
    store.delete(args.run_id)
    print("Pending run committed")
    return 0


def _lead(args: argparse.Namespace) -> int:
    lead = assess_lead(Lead.model_validate_json(args.input.read_text(encoding="utf-8")))
    if args.write:
        runtime_dir = _runtime_dir()
        workbook = _workbook(args, runtime_dir)
        run = RunResult(trigger="manual-lead", status="success", records_fetched=1)
        with LeaseContext(
            FileRunLease(runtime_dir / "run-lease.json"), str(run.run_id), timedelta(minutes=20)
        ):
            workbook.ensure_schema()
            initial = workbook.snapshot()
            LocalSnapshotStore(runtime_dir / "snapshots").save(
                initial, reason=f"pre-lead-{lead.lead_id}"
            )
            run.finished_at = datetime.now(UTC)
            workbook.commit_leads(initial, [lead], run)
    print(lead.model_dump_json(indent=2))
    return 0


def _evaluate_ollama(args: argparse.Namespace) -> int:
    config = load_analysis_config(args.config_dir)
    provider = OllamaAnalyzer(config.ollama, evaluation_mode=True)
    try:
        report = evaluate_provider(provider, load_evaluation_fixtures(args.fixtures))
    finally:
        provider.close()
    print(report.model_dump_json(indent=2))
    return 0 if report.passed else 1


def _auth_google(args: argparse.Namespace) -> int:
    authorize_google(args.client_secrets, WindowsCredentialStore())
    print("Google Sheets authorization stored in Windows Credential Manager")
    return 0


def _credentials_set_gemini(_: argparse.Namespace) -> int:
    value = getpass.getpass("Gemini API key: ").strip()
    WindowsCredentialStore().set(GEMINI_KEY_NAME, value)
    print("Gemini key stored; its value was not displayed")
    return 0


def _credentials_status(args: argparse.Namespace) -> int:
    store = WindowsCredentialStore()
    google_present = all(store.get(name) for name in GOOGLE_CREDENTIAL_NAMES)
    gemini_present = bool(store.get(GEMINI_KEY_NAME))
    print(f"Backend: {store.backend_name}")
    print(f"Google credentials: {'present' if google_present else 'missing or incomplete'}")
    print(f"Google scope: {'Sheets-only' if google_present else 'not validated'}")
    print(f"Gemini key: {'present' if gemini_present else 'missing'}")
    return int(
        (bool(args.require_google) and not google_present)
        or (bool(args.require_gemini) and not gemini_present)
    )


def _credentials_delete_google(_: argparse.Namespace) -> int:
    store = WindowsCredentialStore()
    for name in GOOGLE_CREDENTIAL_NAMES:
        store.delete(name)
    print("Local Google credentials deleted; revoke server-side access separately if desired")
    return 0


def _credentials_delete_gemini(_: argparse.Namespace) -> int:
    WindowsCredentialStore().delete(GEMINI_KEY_NAME)
    print("Local Gemini key deleted")
    return 0


def _workbook(args: argparse.Namespace, runtime_dir: Path) -> WorkbookGateway:
    if args.backend == "google":
        spreadsheet_id = (args.spreadsheet_id or os.getenv("JOBHUNT_SPREADSHEET_ID", "")).strip()
        if not spreadsheet_id:
            raise ValueError(
                "--spreadsheet-id is required for Google (environment fallback is manual-only)"
            )
        store = WindowsCredentialStore()
        return GoogleSheetsWorkbook(spreadsheet_id, load_google_credentials(store))
    return LocalJsonWorkbook(runtime_dir / "workbook.json")


def _analysis_provider(args: argparse.Namespace, runtime_dir: Path) -> AnalysisProvider | None:
    if not args.enable_ai:
        return None
    config = load_analysis_config(args.config_dir)
    if not config.gemini.enabled:
        raise PermissionError("Gemini is disabled; disabling Gemini disables the complete AI chain")
    store = WindowsCredentialStore()
    key = store.get(GEMINI_KEY_NAME)
    if not key:
        raise PermissionError("No Gemini key is stored")
    primary = GeminiAnalyzer(
        key,
        config.gemini,
        FileAnalysisQuotaLedger(runtime_dir / "analysis-quota.json"),
    )
    fallback: AnalysisProvider | None = (
        OllamaAnalyzer(config.ollama) if config.ollama.enabled else None
    )
    return AnalysisOrchestrator(primary, fallback)


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import hashlib
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from jobhunt.canonical import canonicalize_url, duplicate_fingerprint
from jobhunt.eligibility import combine_eligibility
from jobhunt.models import (
    ApplicantCostDecision,
    CandidateProfile,
    EligibilityDecision,
    Lead,
    ListingAvailabilityStatus,
    ListingAvailabilityUpdate,
    Opportunity,
    ProcessingStatus,
    RetentionClass,
    RunResult,
    SourceKind,
    SourceManifest,
    SourceRecord,
    WorkArrangement,
    WorkbookSnapshot,
)
from jobhunt.qualification import assess_candidate_qualification
from jobhunt.security import sanitize_sheet_value
from jobhunt.workbook.schema import (
    CLOSED_REASONS,
    COLD_LEAD_COLUMNS,
    DAILY_OPPORTUNITY_COLUMNS,
    DAILY_OPPORTUNITY_COLUMNS_V3_1,
    DEDUPE_COLUMNS,
    DRAFT_REVIEW_STATUSES,
    EXCLUDED_COLUMNS,
    LEGACY_ARCHIVE_TABS,
    LEGACY_OPPORTUNITY_COLUMNS_V2,
    LEGACY_OPPORTUNITY_TABS,
    MANUAL_INTAKE_COLUMNS,
    OPPORTUNITY_COLUMNS,
    OPPORTUNITY_COLUMNS_V3_0,
    OPPORTUNITY_COLUMNS_V3_1,
    PIPELINE_STAGES,
    PRIORITIES,
    RUN_LOG_COLUMNS,
    SCHEMA_VERSION,
    SOURCE_CONFIG_COLUMNS,
    SYSTEM_EVENT_COLUMNS,
    TAB_SCHEMAS,
    USER_OWNED_LEAD_COLUMNS,
    USER_OWNED_MANUAL_INTAKE_COLUMNS,
    USER_OWNED_OPPORTUNITY_COLUMNS,
    column_letter,
    initial_tab_values,
)

# Keep the complete read failure path below the Task Scheduler run window and,
# more importantly, prevent a stalled socket from waiting indefinitely.
GOOGLE_HTTP_TIMEOUT_SECONDS = 20
GOOGLE_READ_RETRIES = 1


class WorkbookConflict(RuntimeError):
    """Raised for a workbook state that cannot be changed safely."""


class GoogleSheetsError(RuntimeError):
    """Sanitized Google Sheets transport or API failure."""


@dataclass(frozen=True)
class CellPatch:
    tab: str
    row_index: int
    values: dict[int, Any]


@dataclass
class CommitPlan:
    patches: list[CellPatch] = field(default_factory=list)
    records_written: int = 0
    conflicts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MigrationReport:
    source_version: str
    target_version: str
    opportunity_rows: int
    archived_tabs: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_version": self.source_version,
            "target_version": self.target_version,
            "opportunity_rows": self.opportunity_rows,
            "archived_tabs": list(self.archived_tabs),
            "warnings": list(self.warnings),
        }


class WorkbookGateway(ABC):
    @abstractmethod
    def ensure_schema(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def snapshot(self) -> WorkbookSnapshot:
        raise NotImplementedError

    def snapshot_existing(self) -> WorkbookSnapshot:
        """Capture the workbook before a possible schema bootstrap or migration."""
        return self.snapshot()

    @abstractmethod
    def migrate_v3(
        self,
        *,
        write: bool = False,
        source: WorkbookSnapshot | None = None,
        candidate_profile: CandidateProfile | None = None,
        source_manifests: list[SourceManifest] | None = None,
    ) -> MigrationReport:
        raise NotImplementedError

    @abstractmethod
    def commit(
        self,
        initial: WorkbookSnapshot,
        opportunities: list[Opportunity],
        run: RunResult,
        manifests: list[SourceManifest],
    ) -> CommitPlan:
        raise NotImplementedError

    @abstractmethod
    def restore(self, snapshot: WorkbookSnapshot, *, blank_only: bool = True) -> None:
        raise NotImplementedError

    @abstractmethod
    def commit_leads(
        self, initial: WorkbookSnapshot, leads: list[Lead], run: RunResult
    ) -> CommitPlan:
        raise NotImplementedError

    def validate(self) -> list[str]:
        snapshot = self.snapshot()
        errors: list[str] = []
        for title, schema in TAB_SCHEMAS.items():
            rows = snapshot.tabs.get(title)
            if rows is None:
                errors.append(f"Missing tab: {title}")
                continue
            if schema.columns and (not rows or rows[0] != schema.columns):
                errors.append(f"Header mismatch in tab: {title}")
        errors.extend(validate_workflow_rows(snapshot))
        return errors


class LocalJsonWorkbook(WorkbookGateway):
    def __init__(self, path: Path) -> None:
        self.path = path

    def ensure_schema(self) -> None:
        if self.path.exists():
            snapshot = self.snapshot()
            if _has_legacy_opportunity_tabs(snapshot) or snapshot.schema_version != SCHEMA_VERSION:
                raise WorkbookConflict(
                    f"Workbook schema {snapshot.schema_version} detected; run "
                    "migrate-workbook without --write first"
                )
            tabs = snapshot.tabs
            changed = snapshot.schema_version != SCHEMA_VERSION
            migrated_any = False
            for title, rows in initial_tab_values().items():
                if title not in tabs:
                    tabs[title] = rows
                    changed = True
                elif (
                    TAB_SCHEMAS[title].columns
                    and tabs[title]
                    and tabs[title][0] != TAB_SCHEMAS[title].columns
                ):
                    tabs[title] = _migrate_rows(tabs[title], TAB_SCHEMAS[title].columns)
                    changed = True
                    migrated_any = True
            if migrated_any:
                tabs["Dashboard"] = initial_tab_values()["Dashboard"]
            if changed:
                self._write(
                    snapshot.model_copy(update={"schema_version": SCHEMA_VERSION, "tabs": tabs})
                )
            return
        self._write(
            WorkbookSnapshot(
                schema_version=SCHEMA_VERSION,
                spreadsheet_id="local-workbook",
                tabs=initial_tab_values(),
            )
        )

    def migrate_v3(
        self,
        *,
        write: bool = False,
        source: WorkbookSnapshot | None = None,
        candidate_profile: CandidateProfile | None = None,
        source_manifests: list[SourceManifest] | None = None,
    ) -> MigrationReport:
        source = source or self.snapshot()
        target, report = build_v3_migration(
            source,
            candidate_profile=candidate_profile,
            source_manifests=source_manifests,
        )
        if write:
            self._write(target.model_copy(update={"spreadsheet_id": "local-workbook"}))
        return report

    def snapshot(self) -> WorkbookSnapshot:
        if not self.path.exists():
            raise WorkbookConflict("Local workbook does not exist; run bootstrap first")
        return WorkbookSnapshot.model_validate_json(self.path.read_text(encoding="utf-8"))

    def snapshot_existing(self) -> WorkbookSnapshot:
        if self.path.exists():
            return self.snapshot()
        return WorkbookSnapshot(
            schema_version=SCHEMA_VERSION,
            spreadsheet_id="local-workbook",
            tabs={},
        )

    def commit(
        self,
        initial: WorkbookSnapshot,
        opportunities: list[Opportunity],
        run: RunResult,
        manifests: list[SourceManifest],
    ) -> CommitPlan:
        current = self.snapshot()
        plan = build_commit_plan(initial, current, opportunities, run, manifests)
        tabs = {title: [list(row) for row in rows] for title, rows in current.tabs.items()}
        for patch in plan.patches:
            rows = tabs.setdefault(patch.tab, [])
            width = len(TAB_SCHEMAS[patch.tab].columns)
            while len(rows) <= patch.row_index:
                rows.append([""] * width)
            while len(rows[patch.row_index]) < width:
                rows[patch.row_index].append("")
            for column, value in patch.values.items():
                rows[patch.row_index][column] = value
        self._write(current.model_copy(update={"captured_at": datetime.now(UTC), "tabs": tabs}))
        return plan

    def restore(self, snapshot: WorkbookSnapshot, *, blank_only: bool = True) -> None:
        if blank_only and self.path.exists():
            current = self.snapshot()
            populated = any(
                len(rows) > 1 for title, rows in current.tabs.items() if title != "Dashboard"
            )
            if populated:
                raise WorkbookConflict("Refusing to restore over a populated workbook")
        self._write(snapshot.model_copy(update={"spreadsheet_id": "local-workbook"}))

    def commit_leads(
        self, initial: WorkbookSnapshot, leads: list[Lead], run: RunResult
    ) -> CommitPlan:
        current = self.snapshot()
        plan = build_lead_commit_plan(initial, current, leads, run)
        tabs = {title: [list(row) for row in rows] for title, rows in current.tabs.items()}
        for patch in plan.patches:
            rows = tabs.setdefault(patch.tab, [])
            width = len(TAB_SCHEMAS[patch.tab].columns)
            while len(rows) <= patch.row_index:
                rows.append([""] * width)
            while len(rows[patch.row_index]) < width:
                rows[patch.row_index].append("")
            for column, value in patch.values.items():
                rows[patch.row_index][column] = value
        self._write(current.model_copy(update={"captured_at": datetime.now(UTC), "tabs": tabs}))
        return plan

    def _write(self, snapshot: WorkbookSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


class GoogleSheetsWorkbook(WorkbookGateway):
    def __init__(
        self, spreadsheet_id: str, credentials: Any, *, service: Any | None = None
    ) -> None:
        if not spreadsheet_id:
            raise ValueError("A spreadsheet ID is required")
        self.spreadsheet_id = spreadsheet_id
        if service is None:
            try:
                import httplib2
                from google_auth_httplib2 import AuthorizedHttp
                from googleapiclient.discovery import build
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError("Install the sheets extra to use Google Sheets") from exc
            transport = httplib2.Http(timeout=GOOGLE_HTTP_TIMEOUT_SECONDS)
            authorized_transport = AuthorizedHttp(credentials, http=transport)
            service = build("sheets", "v4", http=authorized_transport, cache_discovery=False)
        self._service = service

    def ensure_schema(self) -> None:
        metadata = self._metadata()
        existing = {sheet["properties"]["title"] for sheet in metadata.get("sheets", [])}
        if any(title in existing for title in LEGACY_OPPORTUNITY_TABS):
            raise WorkbookConflict(
                "Workbook schema v2 detected; run migrate-workbook without --write first"
            )
        missing_titles = set(TAB_SCHEMAS) - existing
        additions = [
            {
                "addSheet": {
                    "properties": {
                        "title": schema.title,
                        "hidden": schema.hidden,
                        "gridProperties": {
                            "frozenRowCount": 1 if schema.columns else 0,
                            "hideGridlines": True,
                            "rowCount": 1000,
                            "columnCount": max(10, len(schema.columns)),
                        },
                    }
                }
            }
            for schema in TAB_SCHEMAS.values()
            if schema.title in missing_titles
        ]
        if additions:
            self._batch(additions)
        metadata = self._metadata()
        sheets = {sheet["properties"]["title"]: sheet for sheet in metadata.get("sheets", [])}
        sheet_ids = {title: sheet["properties"]["sheetId"] for title, sheet in sheets.items()}
        current_snapshot = self.snapshot()
        if (
            "Opportunities" in current_snapshot.tabs
            and current_snapshot.tabs["Opportunities"]
            and current_snapshot.schema_version != SCHEMA_VERSION
        ):
            raise WorkbookConflict(
                f"Workbook schema {current_snapshot.schema_version} detected; run "
                "migrate-workbook without --write first"
            )
        current = current_snapshot.tabs
        initial_values = initial_tab_values(
            opportunities_sheet_id=sheet_ids.get("Opportunities", 0)
        )
        requests: list[dict[str, Any]] = []
        migrated_any = False
        setup_titles = set(missing_titles)
        for title, rows in initial_values.items():
            existing_rows = current.get(title, [])
            if not existing_rows:
                # A prior atomic formatting batch may have failed after the
                # separate add-sheet batch succeeded. Treat an empty known tab
                # as unfinished so bootstrap can safely resume.
                setup_titles.add(title)
                requests.append(
                    _update_cells_request(sheet_ids[title], 0, 0, rows, trusted_formulas=True)
                )
            elif TAB_SCHEMAS[title].columns and existing_rows[0] != TAB_SCHEMAS[title].columns:
                migrated = _migrate_rows(existing_rows, TAB_SCHEMAS[title].columns)
                requests.append(
                    _update_cells_request(sheet_ids[title], 0, 0, migrated, trusted_formulas=False)
                )
                migrated_any = True
        if migrated_any:
            requests.append(
                _update_cells_request(
                    sheet_ids["Dashboard"],
                    0,
                    0,
                    initial_values["Dashboard"],
                    trusted_formulas=True,
                )
            )
        requests.extend(_formatting_requests(sheet_ids, setup_titles, sheets))
        if requests:
            self._batch(requests)

    def snapshot(self) -> WorkbookSnapshot:
        metadata = self._metadata()
        sheets = [
            sheet
            for sheet in metadata.get("sheets", [])
            if sheet["properties"]["title"] in TAB_SCHEMAS
        ]
        tabs = self._read_sheet_values(sheets)
        for title in TAB_SCHEMAS:
            tabs.setdefault(title, [])
        return WorkbookSnapshot(
            schema_version=_detect_schema_version(tabs),
            spreadsheet_id=self.spreadsheet_id,
            tabs=tabs,
        )

    def snapshot_existing(self) -> WorkbookSnapshot:
        metadata = self._metadata()
        recognized = (
            set(TAB_SCHEMAS) | set(LEGACY_OPPORTUNITY_TABS) | set(LEGACY_ARCHIVE_TABS.values())
        )
        sheets = [
            sheet
            for sheet in metadata.get("sheets", [])
            if sheet["properties"]["title"] in recognized
        ]
        tabs = self._read_sheet_values(sheets)
        return WorkbookSnapshot(
            schema_version=_detect_schema_version(tabs),
            spreadsheet_id=self.spreadsheet_id,
            tabs=tabs,
        )

    def migrate_v3(
        self,
        *,
        write: bool = False,
        source: WorkbookSnapshot | None = None,
        candidate_profile: CandidateProfile | None = None,
        source_manifests: list[SourceManifest] | None = None,
    ) -> MigrationReport:
        source = source or self.snapshot_existing()
        target, report = build_v3_migration(
            source,
            candidate_profile=candidate_profile,
            source_manifests=source_manifests,
        )
        if not write:
            return report

        metadata = self._metadata()
        sheets = {sheet["properties"]["title"]: sheet for sheet in metadata.get("sheets", [])}
        migrating_v2 = _has_legacy_opportunity_tabs(source)
        if migrating_v2:
            missing_legacy = [title for title in LEGACY_OPPORTUNITY_TABS if title not in sheets]
            if len(missing_legacy) == len(LEGACY_OPPORTUNITY_TABS):
                raise WorkbookConflict("Legacy opportunity tabs disappeared before migration")
        elif "Opportunities" not in sheets:
            raise WorkbookConflict("Opportunities disappeared before migration")
        if "Opportunities" not in sheets:
            self._batch(
                [
                    {
                        "addSheet": {
                            "properties": {
                                "title": "Opportunities",
                                "gridProperties": {
                                    "frozenRowCount": 1,
                                    "frozenColumnCount": 3,
                                    "hideGridlines": True,
                                    "rowCount": 1000,
                                    "columnCount": len(OPPORTUNITY_COLUMNS),
                                },
                            }
                        }
                    }
                ]
            )
            metadata = self._metadata()
            sheets = {sheet["properties"]["title"]: sheet for sheet in metadata.get("sheets", [])}

        missing_target_titles = set(TAB_SCHEMAS) - set(sheets)
        if missing_target_titles:
            self._batch(
                [
                    {
                        "addSheet": {
                            "properties": {
                                "title": TAB_SCHEMAS[title].title,
                                "hidden": TAB_SCHEMAS[title].hidden,
                                "gridProperties": {
                                    "frozenRowCount": (1 if TAB_SCHEMAS[title].columns else 0),
                                    "hideGridlines": True,
                                    "rowCount": 1000,
                                    "columnCount": max(10, len(TAB_SCHEMAS[title].columns)),
                                },
                            }
                        }
                    }
                    for title in TAB_SCHEMAS
                    if title in missing_target_titles
                ]
            )
            metadata = self._metadata()
            sheets = {sheet["properties"]["title"]: sheet for sheet in metadata.get("sheets", [])}

        sheet_ids = {title: sheet["properties"]["sheetId"] for title, sheet in sheets.items()}
        target.tabs["Dashboard"] = initial_tab_values(
            opportunities_sheet_id=sheet_ids["Opportunities"]
        )["Dashboard"]
        opportunity_rows = target.tabs["Opportunities"]
        excluded_rows = target.tabs["Excluded"]

        # Expand every target grid before writing any migrated rows. Schema
        # changes can widen support tabs as well as the primary tables (for
        # example Source Config grew from 15 to 16 columns in v3.2). Google
        # rejects an updateCells request when its rows exceed the sheet's
        # current columnCount, even when the request range itself is correct.
        grid_requests: list[dict[str, Any]] = []
        for title, schema in TAB_SCHEMAS.items():
            rows = target.tabs.get(title, [])
            grid = sheets[title]["properties"].get("gridProperties", {})
            current_columns = int(grid.get("columnCount", 0))
            current_rows = int(grid.get("rowCount", 0))
            content_width = max((len(row) for row in rows), default=0)
            required_columns = max(1, len(schema.columns), content_width)
            minimum_rows = 100 if title == "Dashboard" else 1000
            required_rows = max(minimum_rows, len(rows))

            properties: dict[str, Any] = {"sheetId": sheet_ids[title]}
            fields: list[str] = []
            # The normalized tables must not retain stale trailing columns.
            # Other sheets are only expanded here; their deliberate cleanup is
            # handled later after migrated values have been verified.
            exact_width = title in {"Opportunities", "Excluded"}
            if (exact_width and current_columns != required_columns) or (
                not exact_width and current_columns < required_columns
            ):
                properties.setdefault("gridProperties", {})["columnCount"] = required_columns
                fields.append("gridProperties.columnCount")
            if current_rows < required_rows:
                properties.setdefault("gridProperties", {})["rowCount"] = required_rows
                fields.append("gridProperties.rowCount")
            if fields:
                grid_requests.append(
                    {
                        "updateSheetProperties": {
                            "properties": properties,
                            "fields": ",".join(fields),
                        }
                    }
                )
        if grid_requests:
            self._batch(grid_requests)
        # Clear and replace both normalized tables in one atomic Sheets batch.
        # If validation fails, neither table is changed.
        self._batch(
            [
                {
                    "updateCells": {
                        "range": {"sheetId": sheet_ids["Opportunities"]},
                        "fields": "userEnteredValue",
                    }
                },
                {
                    "updateCells": {
                        "range": {"sheetId": sheet_ids["Excluded"]},
                        "fields": "userEnteredValue",
                    }
                },
                _update_cells_request(
                    sheet_ids["Opportunities"],
                    0,
                    0,
                    opportunity_rows,
                    trusted_formulas=False,
                ),
                _update_cells_request(
                    sheet_ids["Excluded"],
                    0,
                    0,
                    excluded_rows,
                    trusted_formulas=False,
                ),
            ]
        )
        verified_tables = self._read_sheet_values(
            [sheets["Opportunities"], sheets["Excluded"]],
            bounds={
                "Opportunities": (
                    max(1, len(opportunity_rows)),
                    len(OPPORTUNITY_COLUMNS),
                ),
                "Excluded": (max(1, len(excluded_rows)), len(EXCLUDED_COLUMNS)),
            },
        )
        verified = verified_tables.get("Opportunities", [])
        verified_excluded = verified_tables.get("Excluded", [])
        if (
            not verified
            or verified[0] != OPPORTUNITY_COLUMNS
            or len(verified) != len(opportunity_rows)
            or not verified_excluded
            or verified_excluded[0] != EXCLUDED_COLUMNS
            or len(verified_excluded) != len(excluded_rows)
        ):
            raise WorkbookConflict(
                "Normalized opportunity tables could not be verified; archives were left unchanged"
            )

        requests: list[dict[str, Any]] = []
        for title in LEGACY_OPPORTUNITY_TABS:
            if migrating_v2 and title in sheet_ids:
                requests.append(
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": sheet_ids[title],
                                "title": LEGACY_ARCHIVE_TABS[title],
                                "hidden": True,
                            },
                            "fields": "title,hidden",
                        }
                    }
                )
                requests.append(
                    _sheet_protection_request(
                        sheets[title],
                        description="Read-only schema v2 migration archive",
                        unprotected_ranges=[],
                    )
                )
        for title, schema in TAB_SCHEMAS.items():
            if schema.hidden and title in sheet_ids:
                requests.append(
                    {
                        "updateSheetProperties": {
                            "properties": {"sheetId": sheet_ids[title], "hidden": True},
                            "fields": "hidden",
                        }
                    }
                )
        for title in TAB_SCHEMAS:
            if (
                title in {"Opportunities", "Excluded"}
                or title not in sheet_ids
                or title not in target.tabs
            ):
                continue
            rows = target.tabs[title]
            if rows:
                if title == "Dashboard":
                    requests.append(
                        {
                            "updateCells": {
                                "range": {"sheetId": sheet_ids[title]},
                                "fields": "userEnteredValue",
                            }
                        }
                    )
                requests.append(
                    _update_cells_request(
                        sheet_ids[title],
                        0,
                        0,
                        rows,
                        trusted_formulas=title == "Dashboard",
                    )
                )
        run_log_grid_width = int(
            sheets.get("Run Log", {})
            .get("properties", {})
            .get("gridProperties", {})
            .get("columnCount", len(RUN_LOG_COLUMNS))
        )
        if run_log_grid_width > len(RUN_LOG_COLUMNS):
            requests.append(
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": sheet_ids["Run Log"],
                            "dimension": "COLUMNS",
                            "startIndex": len(RUN_LOG_COLUMNS),
                            "endIndex": len(RUN_LOG_COLUMNS) + 1,
                        }
                    }
                }
            )
        presentation_cleanup, formatting_sheets = _presentation_cleanup_requests(sheets)
        requests.extend(presentation_cleanup)
        formatting_requests = _formatting_requests(
            sheet_ids,
            {"Dashboard", "Opportunities", *missing_target_titles},
            formatting_sheets,
        )
        requests.extend(
            request for request in formatting_requests if "addProtectedRange" not in request
        )
        requests.append(
            _sheet_protection_request(
                sheets["Opportunities"],
                description="Machine-owned cells and row order",
                unprotected_ranges=_opportunity_unprotected_ranges(sheet_ids["Opportunities"]),
            )
        )
        if "Manual Intake" in sheets:
            requests.append(
                _sheet_protection_request(
                    sheets["Manual Intake"],
                    description="Manual intake input and machine import status",
                    unprotected_ranges=_manual_intake_unprotected_ranges(
                        sheet_ids["Manual Intake"]
                    ),
                )
            )
        for title in _SUPPORT_TITLES:
            if title in sheets:
                requests.append(
                    _sheet_protection_request(
                        sheets[title],
                        description="Machine-managed support data",
                        unprotected_ranges=[],
                    )
                )
        self._batch(requests)
        return report

    def commit(
        self,
        initial: WorkbookSnapshot,
        opportunities: list[Opportunity],
        run: RunResult,
        manifests: list[SourceManifest],
    ) -> CommitPlan:
        current = self.snapshot()
        plan = build_commit_plan(initial, current, opportunities, run, manifests)
        metadata = self._metadata()
        sheet_ids = {
            sheet["properties"]["title"]: sheet["properties"]["sheetId"]
            for sheet in metadata.get("sheets", [])
        }
        requests: list[dict[str, Any]] = []
        for patch in plan.patches:
            for start, values in _contiguous_values(patch.values):
                requests.append(
                    _update_cells_request(
                        sheet_ids[patch.tab],
                        patch.row_index,
                        start,
                        [values],
                        trusted_formulas=False,
                    )
                )
        if requests:
            try:
                self._batch(requests)
            except Exception as exc:
                try:
                    reconciled = run_is_logged(self.snapshot(), str(run.run_id))
                except Exception:
                    raise exc from None
                if not reconciled:
                    raise
        return plan

    def restore(self, snapshot: WorkbookSnapshot, *, blank_only: bool = True) -> None:
        self.ensure_schema()
        current = self.snapshot()
        if blank_only:
            populated = any(
                len(rows) > 1 for title, rows in current.tabs.items() if title != "Dashboard"
            )
            if populated:
                raise WorkbookConflict("Refusing to restore over a populated workbook")
        metadata = self._metadata()
        sheet_ids = {
            sheet["properties"]["title"]: sheet["properties"]["sheetId"]
            for sheet in metadata.get("sheets", [])
        }
        requests = [
            _update_cells_request(sheet_ids[title], 0, 0, rows, trusted_formulas=False)
            for title, rows in snapshot.tabs.items()
            if title in sheet_ids and rows
        ]
        self._batch(requests)

    def commit_leads(
        self, initial: WorkbookSnapshot, leads: list[Lead], run: RunResult
    ) -> CommitPlan:
        current = self.snapshot()
        plan = build_lead_commit_plan(initial, current, leads, run)
        metadata = self._metadata()
        sheet_ids = {
            sheet["properties"]["title"]: sheet["properties"]["sheetId"]
            for sheet in metadata.get("sheets", [])
        }
        requests: list[dict[str, Any]] = []
        for patch in plan.patches:
            for start, values in _contiguous_values(patch.values):
                requests.append(
                    _update_cells_request(
                        sheet_ids[patch.tab],
                        patch.row_index,
                        start,
                        [values],
                        trusted_formulas=False,
                    )
                )
        if requests:
            try:
                self._batch(requests)
            except Exception as exc:
                try:
                    reconciled = run_is_logged(self.snapshot(), str(run.run_id))
                except Exception:
                    raise exc from None
                if not reconciled:
                    raise
        return plan

    def _metadata(self) -> dict[str, Any]:
        try:
            metadata = cast(
                dict[str, Any],
                self._service.spreadsheets()
                .get(
                    spreadsheetId=self.spreadsheet_id,
                    fields=(
                        "properties(timeZone),"
                        "sheets(properties,charts(chartId,spec.title),filterViews.filterViewId,"
                        "protectedRanges(protectedRangeId,range,description,warningOnly,"
                        "unprotectedRanges),bandedRanges(bandedRangeId,range),"
                        "columnGroups(range,depth,collapsed),conditionalFormats)"
                    ),
                )
                .execute(num_retries=GOOGLE_READ_RETRIES),
            )
        except Exception as exc:
            message = _sanitized_google_error("Google Sheets metadata request failed", exc)
            raise GoogleSheetsError(message) from exc
        actual_time_zone = metadata.get("properties", {}).get("timeZone")
        if actual_time_zone != "Asia/Manila":
            raise WorkbookConflict(
                "Google Sheets workbook time zone must be Asia/Manila; "
                f"found {actual_time_zone or 'missing'}"
            )
        return metadata

    def _read_sheet_values(
        self,
        sheets: list[dict[str, Any]],
        *,
        bounds: dict[str, tuple[int, int]] | None = None,
    ) -> dict[str, list[list[Any]]]:
        """Read exact, metadata-derived rectangles instead of unbounded sheets."""
        if not sheets:
            return {}
        specs: list[tuple[str, str]] = []
        for sheet in sheets:
            properties = sheet["properties"]
            title = str(properties["title"])
            grid = properties.get("gridProperties", {})
            row_count, column_count = (bounds or {}).get(
                title,
                (
                    max(1, int(grid.get("rowCount", 1))),
                    max(1, int(grid.get("columnCount", 1))),
                ),
            )
            specs.append((title, _bounded_a1_range(title, row_count, column_count)))
        try:
            result = (
                self._service.spreadsheets()
                .values()
                .batchGet(
                    spreadsheetId=self.spreadsheet_id,
                    ranges=[a1_range for _, a1_range in specs],
                    valueRenderOption="FORMULA",
                )
                .execute(num_retries=GOOGLE_READ_RETRIES)
            )
        except Exception as exc:
            message = _sanitized_google_error("Google Sheets values request failed", exc)
            raise GoogleSheetsError(message) from exc
        tabs: dict[str, list[list[Any]]] = {}
        value_ranges = result.get("valueRanges", [])
        for (title, _), value_range in zip(specs, value_ranges, strict=False):
            tabs[title] = value_range.get("values", [])
        for title, _ in specs:
            tabs.setdefault(title, [])
        return tabs

    def _batch(self, requests: list[dict[str, Any]]) -> None:
        try:
            (
                self._service.spreadsheets()
                .batchUpdate(spreadsheetId=self.spreadsheet_id, body={"requests": requests})
                .execute()
            )
        except Exception as exc:
            message = _sanitized_google_error("Google Sheets batch request failed", exc)
            raise GoogleSheetsError(message) from exc


def _sanitized_google_error(prefix: str, exc: Exception) -> str:
    """Expose actionable API status/reason without dumping requests or credentials."""
    parts = [prefix]
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    if isinstance(status, int):
        parts.append(f"HTTP {status}")
    reason_getter = getattr(exc, "_get_reason", None)
    reason = reason_getter() if callable(reason_getter) else ""
    if reason:
        normalized = " ".join(str(reason).split())[:500]
        parts.append(normalized)
    if status is None and not reason:
        parts.append(type(exc).__name__)
    return ": ".join(parts)


def _bounded_a1_range(title: str, row_count: int, column_count: int) -> str:
    escaped_title = title.replace("'", "''")
    return f"'{escaped_title}'!A1:{_column_name(column_count)}{row_count}"


def _column_name(one_based_index: int) -> str:
    if one_based_index < 1:
        raise ValueError("Column count must be positive")
    letters: list[str] = []
    value = one_based_index
    while value:
        value, remainder = divmod(value - 1, 26)
        letters.append(chr(ord("A") + remainder))
    return "".join(reversed(letters))


def build_commit_plan(
    initial: WorkbookSnapshot,
    current: WorkbookSnapshot,
    opportunities: list[Opportunity],
    run: RunResult,
    manifests: list[SourceManifest],
) -> CommitPlan:
    plan = CommitPlan()
    tab_next_rows = {title: len(current.tabs.get(title, [])) for title in TAB_SCHEMAS}
    opportunity_tabs = ("Opportunities", "Excluded")
    initial_maps = {
        tab: _row_map(initial.tabs.get(tab, []), "Record ID") for tab in opportunity_tabs
    }
    current_maps = {
        tab: _row_map(current.tabs.get(tab, []), "Record ID") for tab in opportunity_tabs
    }
    for opportunity in opportunities:
        record_id = str(opportunity.record_id)
        destination_tab = (
            "Excluded"
            if opportunity.processing_status is ProcessingStatus.SKIPPED
            else "Opportunities"
        )
        existing = [tab for tab in opportunity_tabs if record_id in current_maps[tab]]
        if len(existing) > 1:
            message = f"Record {record_id} exists in both opportunity tables"
            plan.conflicts.append(message)
            event_row = tab_next_rows["System Events"]
            tab_next_rows["System Events"] += 1
            plan.patches.append(
                CellPatch(
                    "System Events",
                    event_row,
                    _row_values(
                        SYSTEM_EVENT_COLUMNS,
                        {
                            "Event ID": str(uuid4()),
                            "Run ID": str(run.run_id),
                            "Observed At": datetime.now(UTC).isoformat(),
                            "Severity": "error",
                            "Event Type": "duplicate_workbook_identity",
                            "Record ID": record_id,
                            "Message": message,
                        },
                    ),
                )
            )
            continue

        source_tab = existing[0] if existing else None
        is_new = source_tab is None
        moving = source_tab is not None and source_tab != destination_tab
        if source_tab is not None:
            row_index, current_row = current_maps[source_tab][record_id]
            initial_row = initial_maps[source_tab].get(record_id, (row_index, current_row))[1]
            if user_fields_hash(initial_row) != user_fields_hash(current_row):
                message = f"Human-owned cells changed during run for {record_id}"
                plan.conflicts.append(message)
                event_row = tab_next_rows["System Events"]
                tab_next_rows["System Events"] += 1
                plan.patches.append(
                    CellPatch(
                        "System Events",
                        event_row,
                        _row_values(
                            SYSTEM_EVENT_COLUMNS,
                            {
                                "Event ID": str(uuid4()),
                                "Run ID": str(run.run_id),
                                "Observed At": datetime.now(UTC).isoformat(),
                                "Severity": "warning",
                                "Event Type": "human_edit_conflict",
                                "Record ID": record_id,
                                "Message": message,
                            },
                        ),
                    )
                )
                continue
            stored_hash = _current_value(current_row, "User Fields Hash")
            observed_hash = user_fields_hash(current_row)
            if stored_hash and stored_hash != observed_hash:
                event_row = tab_next_rows["System Events"]
                tab_next_rows["System Events"] += 1
                plan.patches.append(
                    CellPatch(
                        "System Events",
                        event_row,
                        _row_values(
                            SYSTEM_EVENT_COLUMNS,
                            {
                                "Event ID": str(uuid4()),
                                "Run ID": str(run.run_id),
                                "Observed At": datetime.now(UTC).isoformat(),
                                "Severity": "info",
                                "Event Type": "human_workflow_change_observed",
                                "Record ID": record_id,
                                "Message": (
                                    "Observed workbook pipeline stage "
                                    f"{_current_value(current_row, 'Pipeline Stage') or 'blank'}"
                                ),
                            },
                        ),
                    )
                )
        else:
            current_row = [""] * len(OPPORTUNITY_COLUMNS)
        if is_new or moving:
            destination_row = tab_next_rows[destination_tab]
            tab_next_rows[destination_tab] += 1
        else:
            destination_row = row_index
        desired = opportunity_to_values(opportunity, current_row)
        machine_values = (
            desired
            if is_new or moving
            else {
                index: value
                for index, value in desired.items()
                if OPPORTUNITY_COLUMNS[index] not in USER_OWNED_OPPORTUNITY_COLUMNS
            }
        )
        plan.patches.append(CellPatch(destination_tab, destination_row, machine_values))
        if moving and source_tab is not None:
            plan.patches.append(
                CellPatch(
                    source_tab,
                    row_index,
                    {index: "" for index in range(len(OPPORTUNITY_COLUMNS))},
                )
            )
        plan.records_written += 1
        _plan_dedupe_patch(
            plan,
            current,
            tab_next_rows,
            destination_tab,
            destination_row,
            opportunity,
        )
        _plan_manual_intake_patch(plan, current, opportunity, destination_tab)

    _plan_listing_availability_updates(
        plan,
        current,
        initial_maps,
        current_maps,
        tab_next_rows,
        run.availability_updates,
        run,
    )

    _plan_source_config_patches(plan, current, tab_next_rows, manifests, run)
    run_row = tab_next_rows["Run Log"]
    run.records_written = plan.records_written
    if plan.conflicts:
        run.records_quarantined += len(plan.conflicts)
        run.status = "partial"
    plan.patches.append(CellPatch("Run Log", run_row, run_result_values(run)))
    return plan


def _plan_listing_availability_updates(
    plan: CommitPlan,
    current: WorkbookSnapshot,
    initial_maps: dict[str, dict[str, tuple[int, list[Any]]]],
    current_maps: dict[str, dict[str, tuple[int, list[Any]]]],
    tab_next_rows: dict[str, int],
    updates: list[ListingAvailabilityUpdate],
    run: RunResult,
) -> None:
    event_types = {
        ListingAvailabilityStatus.ACTIVE: "listing_available_observed",
        ListingAvailabilityStatus.INCONCLUSIVE: "listing_check_inconclusive",
        ListingAvailabilityStatus.UNAVAILABLE: "listing_unavailable_observed",
        ListingAvailabilityStatus.EXPIRED: "listing_expired_archived",
    }
    for update in updates:
        event_type = event_types[update.status]
        if update.archive and update.status is ListingAvailabilityStatus.UNAVAILABLE:
            event_type = "listing_inactive_archived"

        if not update.archive:
            _append_system_event(
                plan,
                tab_next_rows,
                run,
                record_id=update.record_id,
                event_type=event_type,
                severity=(
                    "warning" if update.status is ListingAvailabilityStatus.INCONCLUSIVE else "info"
                ),
                observed_at=update.checked_at,
                message=update.reason,
            )
            continue

        active_entry = current_maps["Opportunities"].get(update.record_id)
        if active_entry is None:
            _append_system_event(
                plan,
                tab_next_rows,
                run,
                record_id=update.record_id,
                event_type="listing_archive_deferred",
                severity="warning",
                observed_at=update.checked_at,
                message="Listing archive deferred because the active row was not found",
            )
            continue
        if update.record_id in current_maps["Excluded"]:
            message = f"Record {update.record_id} already exists in the Excluded table"
            plan.conflicts.append(message)
            _append_system_event(
                plan,
                tab_next_rows,
                run,
                record_id=update.record_id,
                event_type="listing_archive_deferred",
                severity="error",
                observed_at=update.checked_at,
                message=message,
            )
            continue

        row_index, current_row = active_entry
        initial_row = initial_maps["Opportunities"].get(update.record_id, (row_index, current_row))[
            1
        ]
        if user_fields_hash(initial_row) != user_fields_hash(current_row):
            message = f"Human-owned cells changed during listing check for {update.record_id}"
            plan.conflicts.append(message)
            _append_system_event(
                plan,
                tab_next_rows,
                run,
                record_id=update.record_id,
                event_type="listing_archive_deferred",
                severity="warning",
                observed_at=update.checked_at,
                message=message,
            )
            continue

        archived_row = [_value_at(current_row, index) for index in range(len(OPPORTUNITY_COLUMNS))]
        archived_row[OPPORTUNITY_COLUMNS.index("Processing Status")] = (
            ProcessingStatus.SKIPPED.value
        )
        blockers_index = OPPORTUNITY_COLUMNS.index("Blockers")
        existing_blockers = str(_value_at(archived_row, blockers_index) or "")
        availability_blocker = f"Listing unavailable: {update.reason}"
        archived_row[blockers_index] = " | ".join(
            value for value in (existing_blockers, availability_blocker) if value
        )
        archived_row[OPPORTUNITY_COLUMNS.index("Updated At")] = update.checked_at

        destination_row = tab_next_rows["Excluded"]
        tab_next_rows["Excluded"] += 1
        plan.patches.append(
            CellPatch(
                "Excluded",
                destination_row,
                {index: value for index, value in enumerate(archived_row)},
            )
        )
        plan.patches.append(
            CellPatch(
                "Opportunities",
                row_index,
                {index: "" for index in range(len(OPPORTUNITY_COLUMNS))},
            )
        )
        _plan_dedupe_location_patch(
            plan,
            current,
            update.record_id,
            destination_row,
        )
        _append_system_event(
            plan,
            tab_next_rows,
            run,
            record_id=update.record_id,
            event_type=event_type,
            severity="info",
            observed_at=update.checked_at,
            message=update.reason,
        )
        plan.records_written += 1
        run.records_excluded += 1


def _append_system_event(
    plan: CommitPlan,
    tab_next_rows: dict[str, int],
    run: RunResult,
    *,
    record_id: str,
    event_type: str,
    severity: str,
    observed_at: datetime,
    message: str,
) -> None:
    event_row = tab_next_rows["System Events"]
    tab_next_rows["System Events"] += 1
    plan.patches.append(
        CellPatch(
            "System Events",
            event_row,
            _row_values(
                SYSTEM_EVENT_COLUMNS,
                {
                    "Event ID": str(uuid4()),
                    "Run ID": str(run.run_id),
                    "Observed At": observed_at,
                    "Severity": severity,
                    "Event Type": event_type,
                    "Record ID": record_id,
                    "Message": message,
                },
            ),
        )
    )


def _plan_dedupe_location_patch(
    plan: CommitPlan,
    current: WorkbookSnapshot,
    record_id: str,
    destination_row: int,
) -> None:
    entry = _row_map(current.tabs.get("Dedupe Index", []), "Record ID").get(record_id)
    if entry is None:
        return
    row_index, _ = entry
    plan.patches.append(
        CellPatch(
            "Dedupe Index",
            row_index,
            {
                DEDUPE_COLUMNS.index("Primary Tab"): "Excluded",
                DEDUPE_COLUMNS.index("Primary Row"): destination_row + 1,
            },
        )
    )


def build_lead_commit_plan(
    initial: WorkbookSnapshot,
    current: WorkbookSnapshot,
    leads: list[Lead],
    run: RunResult,
) -> CommitPlan:
    plan = CommitPlan()
    tab = "Cold Outreach Leads"
    initial_map = _row_map(initial.tabs.get(tab, []), "Lead ID")
    current_map = _row_map(current.tabs.get(tab, []), "Lead ID")
    next_row = len(current.tabs.get(tab, []))
    event_row = len(current.tabs.get("System Events", []))
    for lead in leads:
        lead_id = str(lead.lead_id)
        is_new = lead_id not in current_map
        if is_new:
            row_index = next_row
            next_row += 1
            current_row: list[Any] = [""] * len(COLD_LEAD_COLUMNS)
        else:
            row_index, current_row = current_map[lead_id]
            initial_row = initial_map.get(lead_id, (row_index, current_row))[1]
            if _lead_user_values(initial_row) != _lead_user_values(current_row):
                message = f"Human-owned lead cells changed during run for {lead_id}"
                plan.conflicts.append(message)
                plan.patches.append(
                    CellPatch(
                        "System Events",
                        event_row,
                        _row_values(
                            SYSTEM_EVENT_COLUMNS,
                            {
                                "Event ID": str(uuid4()),
                                "Run ID": str(run.run_id),
                                "Observed At": datetime.now(UTC).isoformat(),
                                "Severity": "warning",
                                "Event Type": "human_lead_edit_conflict",
                                "Record ID": lead_id,
                                "Message": message,
                            },
                        ),
                    )
                )
                event_row += 1
                continue
        desired = lead_to_values(lead, current_row)
        values = (
            desired
            if is_new
            else {
                index: value
                for index, value in desired.items()
                if COLD_LEAD_COLUMNS[index] not in USER_OWNED_LEAD_COLUMNS
            }
        )
        plan.patches.append(CellPatch(tab, row_index, values))
        plan.records_written += 1
    run.records_written = plan.records_written
    if plan.conflicts:
        run.records_quarantined += len(plan.conflicts)
        run.status = "partial"
    plan.patches.append(
        CellPatch("Run Log", len(current.tabs.get("Run Log", [])), run_result_values(run))
    )
    return plan


def lead_to_values(lead: Lead, current_row: list[Any]) -> dict[int, Any]:
    values = {
        "Lead ID": str(lead.lead_id),
        "Business Name": lead.business_name,
        "Official Domain": str(lead.official_domain),
        "Country": lead.country,
        "Area": lead.area,
        "Industry": lead.industry,
        "Seeded By": lead.seeded_by,
        "Seeded At": lead.seeded_at.isoformat(),
        "Verification Source URL": str(lead.verification_source_url),
        "Verified At": lead.verified_at.isoformat() if lead.verified_at else "",
        "Registry ID": lead.registry_id,
        "Contact Channel": lead.contact_channel,
        "Public Contact": lead.public_contact,
        "Contact Person": lead.contact_person,
        "Role": lead.contact_role,
        "Contact Provenance": lead.contact_provenance,
        "Personal Data": lead.contains_personal_data,
        "Observed Problem": lead.observed_problem,
        "Evidence URL": str(lead.evidence_url or ""),
        "Service Hypothesis": lead.service_hypothesis,
        "AI Relevance Score": lead.ai_relevance_score
        if lead.ai_relevance_score is not None
        else "",
        "AI Rationale": lead.ai_rationale,
        "AI Confidence": lead.ai_confidence if lead.ai_confidence is not None else "",
        "AI Model": lead.ai_model,
        "Prompt Version": lead.prompt_version,
        "Analyzed At": lead.analyzed_at.isoformat() if lead.analyzed_at else "",
        "Lawful Basis Status": lead.lawful_basis_status,
        "LIA Reference": lead.lia_reference,
        "Privacy Notice Needed": lead.privacy_notice_needed,
        "Opt-Out": lead.opted_out,
        "Suppressed At": lead.suppressed_at.isoformat() if lead.suppressed_at else "",
        "Retention Review At": (
            lead.retention_review_at.isoformat() if lead.retention_review_at else ""
        ),
        "Draft": lead.draft,
        "Draft Fact IDs": " | ".join(lead.draft_fact_ids),
        "Review Status": _lead_current_value(current_row, "Review Status")
        or lead.review_status.value,
        "Approved By": _lead_current_value(current_row, "Approved By"),
        "Approved At": _lead_current_value(current_row, "Approved At"),
        "Manual Contact At": _lead_current_value(current_row, "Manual Contact At"),
        "Response": _lead_current_value(current_row, "Response"),
        "Follow-Up At": _lead_current_value(current_row, "Follow-Up At"),
        "Outcome": _lead_current_value(current_row, "Outcome"),
        "Notes": _lead_current_value(current_row, "Notes"),
        "Updated At": lead.updated_at.isoformat(),
    }
    return _row_values(COLD_LEAD_COLUMNS, values)


def _lead_user_values(row: list[Any]) -> dict[str, Any]:
    return {
        name: _value_at(row, COLD_LEAD_COLUMNS.index(name))
        for name in sorted(USER_OWNED_LEAD_COLUMNS)
    }


def _lead_current_value(row: list[Any], name: str) -> Any:
    return _value_at(row, COLD_LEAD_COLUMNS.index(name))


def _decision_label(value: str) -> str:
    return {
        "eligible": "Eligible",
        "manual_review": "Needs review",
        "ineligible": "Ineligible",
    }.get(value, value)


def _cost_label(value: str) -> str:
    return {
        "free_to_apply": "Free to apply",
        "not_stated": "Not stated",
        "ambiguous": "Check cost",
        "payment_required": "Payment required",
    }.get(value, value)


def opportunity_to_values(opportunity: Opportunity, current_row: list[Any]) -> dict[int, Any]:
    record = opportunity.source_record
    breakdown = opportunity.score_breakdown
    draft_review = _current_value(current_row, "Draft Review") or (
        "Needs review" if opportunity.draft else "Not needed"
    )
    draft_review_observed_at = _current_value(current_row, "Draft Review Observed At")
    if draft_review == "Approved" and not draft_review_observed_at:
        draft_review_observed_at = datetime.now(UTC)
    source_deadline = record.expires_at or ""
    values: dict[str, Any] = {
        "Pipeline Stage": _current_value(current_row, "Pipeline Stage") or "Inbox",
        "Priority": _current_value(current_row, "Priority"),
        "Company": record.company,
        "Role": record.title,
        "Track": (
            "VA/Freelance" if record.opportunity_type.value == "va_freelance" else "Technical"
        ),
        "Fit Band": opportunity.fit_band.value if opportunity.fit_band else "",
        "Qualification": _decision_label(opportunity.qualification_decision.value),
        "Final Score": opportunity.final_score if opportunity.final_score is not None else "",
        "Eligibility": _decision_label(opportunity.eligibility_decision.value),
        "Application Cost": _cost_label(opportunity.applicant_cost_decision.value),
        "Work Arrangement": record.work_arrangement.value,
        "Location": record.location_text,
        "Salary": record.salary_raw,
        "Deadline": _current_value(current_row, "Deadline") or source_deadline,
        "Apply URL": str(record.apply_url or ""),
        "Next Action": _current_value(current_row, "Next Action"),
        "Next Action At": _current_value(current_row, "Next Action At"),
        "Source": record.source_label or record.source.value,
        "Date Found": opportunity.first_seen_at,
        "Notes": _current_value(current_row, "Notes"),
        "Seniority": opportunity.seniority_level.value.replace("_", " ").title(),
        "Required Experience Years": (
            opportunity.mandatory_experience_years
            if opportunity.mandatory_experience_years is not None
            else ""
        ),
        "Location Eligibility": _decision_label(opportunity.location_decision.value),
        "Compensation": opportunity.compensation_decision.value.replace("_", " ").title(),
        "Qualification Reasons": " | ".join(opportunity.qualification_reasons),
        "Blockers": " | ".join(opportunity.hard_fail_reasons),
        "Missing Requirements": " | ".join(opportunity.missing_requirements),
        "Match Rationale": opportunity.ai_rationale,
        "Description Snippet": record.description_excerpt,
        "Requirements": " | ".join(record.requirements),
        "Employment Type": record.employment_type,
        "Engagement Type": record.engagement_type,
        "Country": record.country,
        "Published At": record.published_at or "",
        "Source URL": str(record.source_url),
        "Attribution": record.attribution,
        "Draft Review": draft_review,
        "AI Draft": opportunity.draft,
        "Materials Used": _current_value(current_row, "Materials Used"),
        "Submitted At": _current_value(current_row, "Submitted At"),
        "Response At": _current_value(current_row, "Response At"),
        "Interview/Call At": _current_value(current_row, "Interview/Call At"),
        "Contact Name": _current_value(current_row, "Contact Name"),
        "Contact Link": _current_value(current_row, "Contact Link"),
        "Closed Reason": _current_value(current_row, "Closed Reason"),
        "Closed At": _current_value(current_row, "Closed At"),
        "Draft Fact IDs": " | ".join(opportunity.draft_fact_ids),
        "Draft Verification": "Verified IDs" if opportunity.draft_verified else "",
        "Drafted At": opportunity.drafted_at or "",
        "Draft Review Observed At": draft_review_observed_at,
        "Record ID": str(opportunity.record_id),
        "Canonical Key": opportunity.canonical_key,
        "Duplicate Group ID": opportunity.duplicate_group_id or "",
        "Source Record ID": record.source_record_id,
        "Last Seen At": opportunity.last_seen_at,
        "Retrieved At": record.retrieved_at,
        "Source Deadline": source_deadline,
        "Currency": record.currency,
        "Salary Min": record.salary_min if record.salary_min is not None else "",
        "Salary Max": record.salary_max if record.salary_max is not None else "",
        "Retention Class": record.retention_class.value,
        "Location Band": opportunity.location_band,
        "Processing Status": opportunity.processing_status.value,
        "Rule Score": opportunity.rule_score if opportunity.rule_score is not None else "",
        "Required Skill Match Score": breakdown.required_skill_match if breakdown else "",
        "Matched Evidence IDs": " | ".join(opportunity.matched_evidence_ids),
        "AI Confidence": opportunity.ai_confidence if opportunity.ai_confidence is not None else "",
        "AI Provider": opportunity.ai_provider,
        "AI Model": opportunity.ai_model,
        "AI Fallback Reason": opportunity.ai_fallback_reason,
        "Prompt Version": opportunity.prompt_version,
        "Analyzed At": opportunity.analyzed_at or "",
        "AI Error": opportunity.ai_error,
        "Updated At": datetime.now(UTC),
    }
    provisional = _values_to_row(OPPORTUNITY_COLUMNS, values)
    values["User Fields Hash"] = user_fields_hash(provisional)
    return _row_values(OPPORTUNITY_COLUMNS, values)


def run_result_values(run: RunResult) -> dict[int, Any]:
    values = {
        "Run ID": str(run.run_id),
        "Trigger": run.trigger,
        "Started At": run.started_at,
        "Finished At": run.finished_at or "",
        "Sources Attempted": run.sources_attempted,
        "Sources Succeeded": run.sources_succeeded,
        "Records Fetched": run.records_fetched,
        "Records Deduplicated": run.records_deduplicated,
        "Records Actionable": run.records_actionable,
        "Records Needs Review": run.records_needing_review,
        "Records Excluded": run.records_excluded,
        "Records Written": run.records_written,
        "Records Quarantined": run.records_quarantined,
        "AI Input Tokens": run.ai_input_tokens,
        "AI Output Tokens": run.ai_output_tokens,
        "AI Provider Counts": json.dumps(run.ai_provider_counts, sort_keys=True),
        "AI Fallback Count": run.ai_fallback_count,
        "Retry Count": run.retry_count,
        "Status": run.status,
        "Errors": " | ".join(run.errors),
        "Checkpoints": json.dumps(run.checkpoints, sort_keys=True),
    }
    return _row_values(RUN_LOG_COLUMNS, values)


def user_fields_hash(row: list[Any]) -> str:
    values = {
        name: _canonical_user_field_value(_value_at(row, OPPORTUNITY_COLUMNS.index(name)))
        for name in sorted(USER_OWNED_OPPORTUNITY_COLUMNS)
    }
    raw = json.dumps(values, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _canonical_user_field_value(value: Any) -> Any:
    """Normalize Sheets date serials and Python/ISO dates to the same hash value."""
    if isinstance(value, bool) or value in (None, ""):
        return value
    if isinstance(value, (datetime, date)):
        return round(float(_user_entered_value(value, False)["numberValue"]), 10)
    if isinstance(value, (int, float)):
        return round(float(value), 10)
    if isinstance(value, str):
        candidate = value.strip()
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed_date = date.fromisoformat(candidate)
            except ValueError:
                return candidate
            return round(float(_user_entered_value(parsed_date, False)["numberValue"]), 10)
        return round(float(_user_entered_value(parsed, False)["numberValue"]), 10)
    return value


def _migrate_rows(rows: list[list[Any]], columns: list[str]) -> list[list[Any]]:
    """Map an older table by header name while retaining every known human field."""
    if not rows:
        return [list(columns)]
    old_header = [str(value) for value in rows[0]]
    old_indexes = {name: index for index, name in enumerate(old_header)}
    migrated: list[list[Any]] = [list(columns)]
    for row in rows[1:]:
        # Google values reads can retain interior/formatted rows represented
        # entirely by empty cells. They are grid capacity, not records, and
        # cannot be verified after an update because the API trims them again.
        if not any(value not in (None, "") for value in row):
            continue
        migrated.append(
            [
                _migrated_cell(name, _value_at(row, old_indexes[name]))
                if name in old_indexes
                else ""
                for name in columns
            ]
        )
    return migrated


_MIGRATION_DATE_COLUMNS = {
    "Deadline",
    "Next Action At",
    "Date Found",
    "Published At",
    "Submitted At",
    "Response At",
    "Interview/Call At",
    "Closed At",
    "Drafted At",
    "Draft Review Observed At",
    "Last Seen At",
    "Retrieved At",
    "Source Deadline",
    "Analyzed At",
    "Updated At",
    "Started At",
    "Finished At",
    "First Seen At",
    "Terms Verified At",
    "Observed At",
    "Seeded At",
    "Verified At",
    "Suppressed At",
    "Retention Review At",
    "Approved At",
    "Manual Contact At",
    "Follow-Up At",
}


def _migrated_cell(column: str, value: Any) -> Any:
    if column in {"Qualification", "Eligibility", "Location Eligibility"}:
        return _decision_label(str(value)) if value not in (None, "") else ""
    if column == "Application Cost":
        return _cost_label(str(value)) if value not in (None, "") else ""
    if column in {"Seniority", "Compensation"} and value not in (None, ""):
        return str(value).replace("_", " ").title()
    if column not in _MIGRATION_DATE_COLUMNS or not isinstance(value, str):
        return value
    candidate = value.strip()
    if not candidate:
        return ""
    try:
        return datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(candidate)
        except ValueError:
            return value


def _row_is_excluded(row: list[Any]) -> bool:
    """Route only definitive exclusions out of the daily queue during migration."""
    fit = str(_value_at(row, OPPORTUNITY_COLUMNS.index("Fit Band"))).casefold()
    processing = str(_value_at(row, OPPORTUNITY_COLUMNS.index("Processing Status"))).casefold()
    qualification = str(_value_at(row, OPPORTUNITY_COLUMNS.index("Qualification"))).casefold()
    cost = str(_value_at(row, OPPORTUNITY_COLUMNS.index("Application Cost"))).casefold()
    return (
        fit == "skip"
        or processing == "skipped"
        or qualification in {"ineligible", "excluded"}
        or cost in {"payment_required", "payment required"}
    )


def _reconcile_dedupe_positions(tabs: dict[str, list[list[Any]]]) -> None:
    dedupe_rows = tabs.get("Dedupe Index", [])
    if not dedupe_rows:
        return
    positions: dict[str, tuple[str, int]] = {}
    record_index = OPPORTUNITY_COLUMNS.index("Record ID")
    for title in ("Opportunities", "Excluded"):
        for row_number, row in enumerate(tabs.get(title, [])[1:], start=2):
            record_id = str(_value_at(row, record_index))
            if record_id:
                positions[record_id] = (title, row_number)
    dedupe_record_index = DEDUPE_COLUMNS.index("Record ID")
    tab_index = DEDUPE_COLUMNS.index("Primary Tab")
    row_index = DEDUPE_COLUMNS.index("Primary Row")
    for dedupe_row in dedupe_rows[1:]:
        record_id = str(_value_at(dedupe_row, dedupe_record_index))
        if record_id not in positions:
            continue
        while len(dedupe_row) < len(DEDUPE_COLUMNS):
            dedupe_row.append("")
        dedupe_row[tab_index], dedupe_row[row_index] = positions[record_id]


def _has_legacy_opportunity_tabs(snapshot: WorkbookSnapshot) -> bool:
    return any(title in snapshot.tabs for title in LEGACY_OPPORTUNITY_TABS)


def _apply_source_applicant_cost_policies(
    tabs: dict[str, list[list[Any]]],
    manifests: list[SourceManifest],
    candidate_profile: CandidateProfile,
) -> tuple[int, int]:
    """Move automated records from sources now known to require applicant payment."""
    blocked_sources = {
        manifest.adapter_id.value
        for manifest in manifests
        if manifest.default_applicant_cost is ApplicantCostDecision.PAYMENT_REQUIRED
    }
    if not blocked_sources:
        return 0, 0

    active = _migrate_rows(tabs.get("Opportunities", []), OPPORTUNITY_COLUMNS)
    excluded = _migrate_rows(tabs.get("Excluded", []), EXCLUDED_COLUMNS)
    source_index = OPPORTUNITY_COLUMNS.index("Source")
    source_record_index = OPPORTUNITY_COLUMNS.index("Source Record ID")
    record_index = OPPORTUNITY_COLUMNS.index("Record ID")
    cost_index = OPPORTUNITY_COLUMNS.index("Application Cost")
    qualification_index = OPPORTUNITY_COLUMNS.index("Qualification")
    hash_index = OPPORTUNITY_COLUMNS.index("User Fields Hash")
    excluded_by_id = {
        str(_value_at(row, record_index)): index
        for index, row in enumerate(excluded[1:], start=1)
        if _value_at(row, record_index) not in (None, "")
    }

    def is_blocked_automated_record(row: list[Any]) -> bool:
        source_record_id = str(_value_at(row, source_record_index))
        source = str(_value_at(row, source_index)).casefold().strip()
        return not source_record_id.startswith("manual-sheet-") and source in blocked_sources

    def enforce(row: list[Any]) -> bool:
        if (
            _value_at(row, cost_index) == "Payment required"
            and _value_at(row, qualification_index) == "Ineligible"
        ):
            return False
        row[cost_index] = "Payment required"
        _apply_migrated_qualification(row, candidate_profile)
        row[hash_index] = user_fields_hash(row)
        return True

    retained = [list(OPPORTUNITY_COLUMNS)]
    moved = 0
    updated = 0
    for row in active[1:]:
        if not is_blocked_automated_record(row):
            retained.append(row)
            continue
        enforce(row)
        record_id = str(_value_at(row, record_index))
        if record_id and record_id in excluded_by_id:
            excluded[excluded_by_id[record_id]] = row
        else:
            excluded.append(row)
            if record_id:
                excluded_by_id[record_id] = len(excluded) - 1
        moved += 1

    for row in excluded[1:]:
        if is_blocked_automated_record(row) and enforce(row):
            updated += 1

    tabs["Opportunities"] = retained
    tabs["Excluded"] = excluded
    if moved or updated:
        _reconcile_dedupe_positions(tabs)
    return moved, updated


def _detect_schema_version(tabs: dict[str, list[list[Any]]]) -> str:
    if any(title in tabs for title in LEGACY_OPPORTUNITY_TABS):
        return "2.0.0"
    rows = tabs.get("Opportunities", [])
    if not rows:
        return SCHEMA_VERSION
    header = [str(value) for value in rows[0]]
    if header == OPPORTUNITY_COLUMNS:
        return SCHEMA_VERSION
    if header == OPPORTUNITY_COLUMNS_V3_0:
        return "3.0.0"
    if header == OPPORTUNITY_COLUMNS_V3_1:
        return "3.1.0"
    return "unknown"


def build_v3_migration(
    source: WorkbookSnapshot,
    *,
    candidate_profile: CandidateProfile | None = None,
    source_manifests: list[SourceManifest] | None = None,
) -> tuple[WorkbookSnapshot, MigrationReport]:
    """Build a loss-averse migration to the current workbook schema."""
    candidate_profile = candidate_profile or CandidateProfile()
    source_manifests = source_manifests or []
    if (
        "Opportunities" in source.tabs
        and source.tabs["Opportunities"]
        and not _has_legacy_opportunity_tabs(source)
    ):
        opportunity_rows = source.tabs["Opportunities"]
        header = [str(value) for value in opportunity_rows[0]]
        if header == OPPORTUNITY_COLUMNS:
            current_tabs = {
                title: [list(row) for row in rows] for title, rows in source.tabs.items()
            }
            for title, initial_rows in initial_tab_values().items():
                current_tabs.setdefault(title, [list(row) for row in initial_rows])
            for title, schema in TAB_SCHEMAS.items():
                if not schema.columns:
                    continue
                current_tabs[title] = _migrate_rows(current_tabs.get(title, []), schema.columns)
            moved, updated = _apply_source_applicant_cost_policies(
                current_tabs, source_manifests, candidate_profile
            )
            policy_warnings: tuple[str, ...] = ()
            if moved:
                policy_warnings += (
                    f"Moved {moved} paywalled-source record(s) to the hidden Excluded tab.",
                )
            if updated:
                policy_warnings += (
                    f"Updated {updated} existing excluded record(s) with current "
                    "source-cost policy.",
                )
            return source.model_copy(
                update={"schema_version": SCHEMA_VERSION, "tabs": current_tabs}
            ), MigrationReport(
                source_version=SCHEMA_VERSION,
                target_version=SCHEMA_VERSION,
                opportunity_rows=max(0, len(current_tabs["Opportunities"]) - 1),
                archived_tabs=tuple(
                    title for title in LEGACY_ARCHIVE_TABS.values() if title in source.tabs
                ),
                warnings=policy_warnings
                or ("Workbook is already at the current schema; no data changes are required.",),
            )
        if header in (OPPORTUNITY_COLUMNS_V3_0, OPPORTUNITY_COLUMNS_V3_1):
            source_version = "3.0.0" if header == OPPORTUNITY_COLUMNS_V3_0 else "3.1.0"
            v31_tabs = {title: [list(row) for row in rows] for title, rows in source.tabs.items()}
            migrated = _migrate_rows(opportunity_rows, OPPORTUNITY_COLUMNS)
            for row in migrated[1:]:
                _apply_migrated_qualification(row, candidate_profile)
                row[OPPORTUNITY_COLUMNS.index("User Fields Hash")] = user_fields_hash(row)
            active_rows = [list(OPPORTUNITY_COLUMNS)]
            excluded_rows = _migrate_rows(v31_tabs.get("Excluded", []), EXCLUDED_COLUMNS)
            excluded_ids = {
                str(_value_at(row, EXCLUDED_COLUMNS.index("Record ID")))
                for row in excluded_rows[1:]
                if _value_at(row, EXCLUDED_COLUMNS.index("Record ID")) not in (None, "")
            }
            moved = 0
            for row in migrated[1:]:
                if _row_is_excluded(row):
                    record_id = str(_value_at(row, OPPORTUNITY_COLUMNS.index("Record ID")))
                    if record_id and record_id not in excluded_ids:
                        excluded_rows.append(row)
                        excluded_ids.add(record_id)
                    moved += 1
                else:
                    active_rows.append(row)
            v31_tabs["Opportunities"] = active_rows
            v31_tabs["Excluded"] = excluded_rows
            v31_tabs["Dashboard"] = initial_tab_values()["Dashboard"]
            for title, initial_rows in initial_tab_values().items():
                v31_tabs.setdefault(title, [list(row) for row in initial_rows])
            for title, schema in TAB_SCHEMAS.items():
                if title in {"Dashboard", "Opportunities"}:
                    continue
                source_rows = v31_tabs.get(title)
                if source_rows:
                    v31_tabs[title] = _migrate_rows(source_rows, schema.columns)
            _reconcile_dedupe_positions(v31_tabs)
            target = source.model_copy(
                update={
                    "schema_version": SCHEMA_VERSION,
                    "captured_at": datetime.now(UTC),
                    "tabs": v31_tabs,
                }
            )
            return target, MigrationReport(
                source_version=source_version,
                target_version=SCHEMA_VERSION,
                opportunity_rows=max(0, len(active_rows) - 1),
                archived_tabs=tuple(
                    title for title in LEGACY_ARCHIVE_TABS.values() if title in source.tabs
                ),
                warnings=(
                    (f"Moved {moved} previously skipped rows to the hidden Excluded tab.",)
                    if moved
                    else ()
                ),
            )
        raise WorkbookConflict("Opportunities exists with an unrecognized header")
    legacy_titles = [title for title in LEGACY_OPPORTUNITY_TABS if title in source.tabs]
    if not legacy_titles:
        raise WorkbookConflict("No schema v2 opportunity tabs were found")
    conflicting_archives = [
        archive for archive in LEGACY_ARCHIVE_TABS.values() if archive in source.tabs
    ]
    if conflicting_archives:
        raise WorkbookConflict(
            "Migration archive tab already exists: " + ", ".join(conflicting_archives)
        )

    migrated_rows: dict[str, list[Any]] = {}
    warnings: list[str] = []
    for title in legacy_titles:
        rows = source.tabs.get(title, [])
        if not rows:
            continue
        header = [str(value) for value in rows[0]]
        if len(header) != len(set(header)):
            raise WorkbookConflict(f"Duplicate headers in {title}")
        unknown_populated = [
            name
            for index, name in enumerate(header)
            if name not in LEGACY_OPPORTUNITY_COLUMNS_V2
            and name
            and any(_value_at(row, index) not in (None, "") for row in rows[1:])
        ]
        if unknown_populated:
            raise WorkbookConflict(
                f"Refusing to discard populated custom columns in {title}: "
                + ", ".join(unknown_populated)
            )
        indexes = {name: index for index, name in enumerate(header)}
        for row_number, row in enumerate(rows[1:], start=2):
            if not any(value not in (None, "") for value in row):
                continue
            record_id = str(_legacy_value(row, indexes, "Record ID"))
            if not record_id:
                raise WorkbookConflict(f"{title} row {row_number} has no Record ID")
            migrated = _legacy_opportunity_row(title, row, indexes)
            previous = migrated_rows.get(record_id)
            if previous is not None:
                compared_columns = set(OPPORTUNITY_COLUMNS) - {
                    "User Fields Hash",
                    "Updated At",
                }
                previous_values = _selected_values(previous, compared_columns)
                migrated_values = _selected_values(migrated, compared_columns)
                if previous_values != migrated_values:
                    raise WorkbookConflict(
                        f"Conflicting values for duplicate Record ID {record_id}"
                    )
                warnings.append(f"Collapsed duplicate legacy Record ID {record_id}")
            else:
                migrated_rows[record_id] = migrated

    for migrated_row in migrated_rows.values():
        _apply_migrated_qualification(migrated_row, candidate_profile)
        migrated_row[OPPORTUNITY_COLUMNS.index("User Fields Hash")] = user_fields_hash(migrated_row)

    target_tabs: dict[str, list[list[Any]]] = {
        title: [list(row) for row in rows] for title, rows in initial_tab_values().items()
    }
    target_tabs["Opportunities"] = [list(OPPORTUNITY_COLUMNS), *migrated_rows.values()]
    existing_opportunities = source.tabs.get("Opportunities", [])
    if existing_opportunities and not _migration_table_matches(
        existing_opportunities, target_tabs["Opportunities"]
    ):
        raise WorkbookConflict(
            "Existing Opportunities data does not match the recoverable migration state"
        )
    for title, schema in TAB_SCHEMAS.items():
        if title in {"Dashboard", "Opportunities"}:
            continue
        source_rows = source.tabs.get(title)
        if source_rows:
            target_tabs[title] = _migrate_rows(source_rows, schema.columns)
    for legacy_title in legacy_titles:
        target_tabs[LEGACY_ARCHIVE_TABS[legacy_title]] = [
            list(row) for row in source.tabs.get(legacy_title, [])
        ]

    _reconcile_dedupe_positions(target_tabs)

    target = source.model_copy(
        update={
            "schema_version": SCHEMA_VERSION,
            "captured_at": datetime.now(UTC),
            "tabs": target_tabs,
        }
    )
    report = MigrationReport(
        source_version=(
            source.schema_version if source.schema_version != SCHEMA_VERSION else "2.0.0"
        ),
        target_version=SCHEMA_VERSION,
        opportunity_rows=len(migrated_rows),
        archived_tabs=tuple(LEGACY_ARCHIVE_TABS[title] for title in legacy_titles),
        warnings=tuple(warnings),
    )
    return target, report


def _legacy_opportunity_row(legacy_tab: str, row: list[Any], indexes: dict[str, int]) -> list[Any]:
    def get(name: str) -> Any:
        return _legacy_value(row, indexes, name)

    legacy_status = str(get("Review Status"))
    outcome = str(get("Outcome"))
    notes = str(get("Reviewer Notes"))
    legacy_annotations: list[str] = []
    approved_by = str(get("Approved By"))
    approved_at = str(get("Approved At"))
    if approved_by or approved_at:
        legacy_annotations.append(
            "Legacy approval: " + " ".join(value for value in (approved_by, approved_at) if value)
        )
    closed_reason = _legacy_closed_reason(legacy_status, outcome)
    if outcome:
        legacy_annotations.append(f"Legacy outcome: {outcome}")
    if legacy_annotations:
        notes = "\n".join(value for value in (notes, *legacy_annotations) if value)
    ai_draft = get("AI Draft")
    source_deadline = get("Expires At")
    values: dict[str, Any] = {
        "Pipeline Stage": _legacy_pipeline_stage(legacy_status),
        "Priority": get("Priority"),
        "Company": get("Company"),
        "Role": get("Title"),
        "Track": _legacy_track(str(get("Opportunity Type")), legacy_tab),
        "Fit Band": get("Fit Band"),
        "Final Score": get("Final Score"),
        "Eligibility": get("Eligibility Decision"),
        "Work Arrangement": get("Work Arrangement"),
        "Location": get("Location Text"),
        "Salary": get("Salary Raw"),
        "Deadline": source_deadline,
        "Apply URL": get("Apply URL"),
        "Next Action": "Follow up" if get("Follow-Up At") else "",
        "Next Action At": get("Follow-Up At"),
        "Source": get("Source"),
        "Date Found": get("First Seen At"),
        "Notes": notes,
        "Blockers": get("Hard-Fail Reasons"),
        "Missing Requirements": get("Missing Requirements"),
        "Match Rationale": get("AI Rationale"),
        "Description Snippet": get("Description Snippet"),
        "Requirements": get("Requirements"),
        "Employment Type": get("Employment Type"),
        "Engagement Type": get("Engagement Type"),
        "Country": get("Country"),
        "Published At": get("Published At"),
        "Source URL": get("Source URL"),
        "Attribution": get("Attribution"),
        "Draft Review": "Needs review" if ai_draft else "Not needed",
        "AI Draft": ai_draft,
        "Materials Used": "",
        "Submitted At": get("Manual Action At"),
        "Response At": get("Response At"),
        "Interview/Call At": get("Interview At"),
        "Contact Name": "",
        "Contact Link": "",
        "Closed Reason": closed_reason,
        "Closed At": "",
        "Draft Fact IDs": get("Draft Fact IDs"),
        "Draft Verification": get("Draft Verification"),
        "Drafted At": get("Drafted At"),
        "Draft Review Observed At": "",
        "Record ID": get("Record ID"),
        "Canonical Key": get("Canonical Key"),
        "Duplicate Group ID": get("Duplicate Group ID"),
        "Source Record ID": get("Source Record ID"),
        "Last Seen At": get("Last Seen At"),
        "Retrieved At": get("Retrieved At"),
        "Source Deadline": source_deadline,
        "Currency": get("Currency"),
        "Salary Min": get("Salary Min"),
        "Salary Max": get("Salary Max"),
        "Retention Class": get("Retention Class"),
        "Location Band": get("Location Band"),
        "Processing Status": get("Processing Status"),
        "Rule Score": get("Rule Score"),
        "Required Skill Match Score": get("Required Skills"),
        "Matched Evidence IDs": get("Matched Evidence IDs"),
        "AI Confidence": get("AI Confidence"),
        "AI Provider": get("AI Provider"),
        "AI Model": get("AI Model"),
        "AI Fallback Reason": get("AI Fallback Reason"),
        "Prompt Version": get("Prompt Version"),
        "Analyzed At": get("Analyzed At"),
        "AI Error": get("AI Error"),
        "Updated At": get("Updated At"),
    }
    for name in _MIGRATION_DATE_COLUMNS & values.keys():
        values[name] = _migrated_cell(name, values[name])
    provisional = _values_to_row(OPPORTUNITY_COLUMNS, values)
    values["User Fields Hash"] = user_fields_hash(provisional)
    return _values_to_row(OPPORTUNITY_COLUMNS, values)


def _apply_migrated_qualification(row: list[Any], candidate_profile: CandidateProfile) -> None:
    """Reclassify retained rows using only facts already present in the workbook."""
    record_id = str(_current_value(row, "Record ID") or "migration-row")
    source_url = str(
        _current_value(row, "Source URL")
        or _current_value(row, "Apply URL")
        or f"https://migration.invalid/{record_id}"
    )
    try:
        arrangement = WorkArrangement(
            str(_current_value(row, "Work Arrangement") or "unknown").casefold()
        )
    except ValueError:
        arrangement = WorkArrangement.UNKNOWN
    try:
        retention = RetentionClass(
            str(_current_value(row, "Retention Class") or "excerpt").casefold()
        )
    except ValueError:
        retention = RetentionClass.EXCERPT
    description = str(_current_value(row, "Description Snippet"))[:4_000]
    requirements = [
        item.strip() for item in str(_current_value(row, "Requirements")).split("|") if item.strip()
    ]
    record = SourceRecord(
        source=SourceKind.MANUAL,
        source_record_id=record_id,
        source_url=source_url,
        apply_url=str(_current_value(row, "Apply URL") or source_url),
        company=str(_current_value(row, "Company") or "Unknown company"),
        title=str(_current_value(row, "Role") or "Untitled role"),
        location_text=str(_current_value(row, "Location")),
        country=str(_current_value(row, "Country")),
        work_arrangement=arrangement,
        employment_type=str(_current_value(row, "Employment Type")),
        engagement_type=str(_current_value(row, "Engagement Type")),
        salary_raw=str(_current_value(row, "Salary")),
        description_excerpt=description,
        requirements=requirements,
        attribution=str(_current_value(row, "Attribution") or "Migrated workbook row"),
        retention_class=retention,
        applicant_cost=_parse_applicant_cost(_current_value(row, "Application Cost")),
    )
    assessment = assess_candidate_qualification(record, candidate_profile)
    location = _parse_eligibility(_current_value(row, "Eligibility"))
    overall = combine_eligibility(location, assessment.decision)
    replacements: dict[str, Any] = {
        "Qualification": _decision_label(assessment.decision.value),
        "Eligibility": _decision_label(overall.value),
        "Application Cost": _cost_label(assessment.applicant_cost_decision.value),
        "Seniority": assessment.seniority_level.value.replace("_", " ").title(),
        "Required Experience Years": (
            assessment.mandatory_experience_years
            if assessment.mandatory_experience_years is not None
            else ""
        ),
        "Location Eligibility": _decision_label(location.value),
        "Compensation": assessment.compensation_decision.value.replace("_", " ").title(),
        "Qualification Reasons": " | ".join(assessment.reasons),
    }
    if assessment.decision.value == "ineligible":
        existing = str(_current_value(row, "Blockers"))
        replacements["Blockers"] = " | ".join(
            value for value in (existing, *assessment.reasons) if value
        )
        replacements["Processing Status"] = ProcessingStatus.SKIPPED.value
    for name, value in replacements.items():
        row[OPPORTUNITY_COLUMNS.index(name)] = value


def _parse_eligibility(value: Any) -> EligibilityDecision:
    normalized = str(value).casefold().replace(" ", "_")
    if normalized in {"eligible"}:
        return EligibilityDecision.ELIGIBLE
    if normalized in {"ineligible", "excluded"}:
        return EligibilityDecision.INELIGIBLE
    return EligibilityDecision.MANUAL_REVIEW


def _parse_applicant_cost(value: Any) -> ApplicantCostDecision:
    normalized = str(value).casefold().replace(" ", "_")
    aliases = {
        "free_to_apply": ApplicantCostDecision.FREE_TO_APPLY,
        "payment_required": ApplicantCostDecision.PAYMENT_REQUIRED,
        "check_cost": ApplicantCostDecision.AMBIGUOUS,
        "ambiguous": ApplicantCostDecision.AMBIGUOUS,
        "not_stated": ApplicantCostDecision.NOT_STATED,
    }
    return aliases.get(normalized, ApplicantCostDecision.NOT_STATED)


def _migration_table_matches(current: list[list[Any]], expected: list[list[Any]]) -> bool:
    if len(current) != len(expected) or not current or current[0] != expected[0]:
        return False
    for current_row, expected_row in zip(current[1:], expected[1:], strict=True):
        for index, column in enumerate(OPPORTUNITY_COLUMNS):
            current_value = _value_at(current_row, index)
            expected_value = _value_at(expected_row, index)
            if column in _MIGRATION_DATE_COLUMNS:
                current_value = _canonical_user_field_value(_migrated_cell(column, current_value))
                expected_value = _canonical_user_field_value(_migrated_cell(column, expected_value))
            if current_value != expected_value:
                return False
    return True


def _legacy_value(row: list[Any], indexes: dict[str, int], name: str) -> Any:
    index = indexes.get(name)
    return _value_at(row, index) if index is not None else ""


def _legacy_pipeline_stage(status: str) -> str:
    return {
        "New": "Inbox",
        "Needs Review": "Inbox",
        "Approved": "Shortlisted",
        "Applied/Sent": "Submitted",
        "Response": "Submitted",
        "Interview": "Interview/Call",
        "Closed/Rejected": "Closed",
    }.get(status, "Inbox")


def _legacy_track(value: str, legacy_tab: str) -> str:
    if value == "va_freelance" or legacy_tab == "VA & Freelance":
        return "VA/Freelance"
    return "Technical"


def _legacy_closed_reason(status: str, outcome: str) -> str:
    if status != "Closed/Rejected":
        return ""
    lookup = {value.casefold(): value for value in CLOSED_REASONS}
    return lookup.get(outcome.casefold(), "Other" if outcome else "Rejected")


def _selected_values(row: list[Any], columns: set[str]) -> dict[str, Any]:
    return {name: _current_value(row, name) for name in sorted(columns)}


def validate_workflow_rows(snapshot: WorkbookSnapshot) -> list[str]:
    issues: list[str] = []
    tab = "Opportunities"
    for row_index, row in enumerate(snapshot.tabs.get(tab, [])[1:], start=2):
        stage = _current_value(row, "Pipeline Stage")
        submitted_at = _current_value(row, "Submitted At")
        if stage in {"Submitted", "Interview/Call", "Offer", "Won"} and not submitted_at:
            issues.append(f"warning: {tab} row {row_index}: {stage} has no Submitted At date")
        if stage == "Interview/Call" and not _current_value(row, "Interview/Call At"):
            issues.append(f"warning: {tab} row {row_index}: Interview/Call has no interview date")
        if stage == "Closed":
            if not _current_value(row, "Closed Reason"):
                issues.append(f"warning: {tab} row {row_index}: Closed has no Closed Reason")
            if not _current_value(row, "Closed At"):
                issues.append(f"warning: {tab} row {row_index}: Closed has no Closed At date")
        if _current_value(row, "Draft Review") == "Approved" and (
            not _current_value(row, "AI Draft")
            or not _current_value(row, "Draft Fact IDs")
            or _current_value(row, "Draft Verification") != "Verified IDs"
        ):
            issues.append(
                f"{tab} row {row_index}: Approved draft requires a verified draft and fact IDs"
            )
    return issues


def run_is_logged(snapshot: WorkbookSnapshot, run_id: str) -> bool:
    return run_id in _row_map(snapshot.tabs.get("Run Log", []), "Run ID")


def _plan_dedupe_patch(
    plan: CommitPlan,
    current: WorkbookSnapshot,
    next_rows: dict[str, int],
    tab: str,
    row_index: int,
    opportunity: Opportunity,
) -> None:
    row_map = _row_map(current.tabs.get("Dedupe Index", []), "Record ID")
    record_id = str(opportunity.record_id)
    target_row = row_map.get(record_id, (next_rows["Dedupe Index"], []))[0]
    if record_id not in row_map:
        next_rows["Dedupe Index"] += 1
    values = {
        "Canonical Key": opportunity.canonical_key,
        "Record ID": record_id,
        "Fingerprint": duplicate_fingerprint(opportunity.source_record),
        "Duplicate Group ID": opportunity.duplicate_group_id or "",
        "Primary Tab": tab,
        "Primary Row": row_index + 1,
        "Source Links": str(opportunity.source_record.source_url),
        "First Seen At": opportunity.first_seen_at.isoformat(),
        "Last Seen At": opportunity.last_seen_at.isoformat(),
    }
    plan.patches.append(CellPatch("Dedupe Index", target_row, _row_values(DEDUPE_COLUMNS, values)))


def _plan_manual_intake_patch(
    plan: CommitPlan,
    current: WorkbookSnapshot,
    opportunity: Opportunity,
    destination_tab: str,
) -> None:
    if opportunity.source_record.source is not SourceKind.MANUAL:
        return
    rows = current.tabs.get("Manual Intake", [])
    if not rows or rows[0] != MANUAL_INTAKE_COLUMNS:
        return
    url_index = MANUAL_INTAKE_COLUMNS.index("Listing URL")
    status_index = MANUAL_INTAKE_COLUMNS.index("Import Status")
    message_index = MANUAL_INTAKE_COLUMNS.index("Import Message")
    record_index = MANUAL_INTAKE_COLUMNS.index("Imported Record ID")
    target_url = canonicalize_url(opportunity.source_record.source_url)
    for row_index, row in enumerate(rows[1:], start=1):
        raw_url = str(_value_at(row, url_index))
        if not raw_url:
            continue
        try:
            matches = canonicalize_url(raw_url) == target_url
        except ValueError:
            matches = False
        if matches:
            plan.patches.append(
                CellPatch(
                    "Manual Intake",
                    row_index,
                    {
                        status_index: "Imported",
                        message_index: f"Routed to {destination_tab}",
                        record_index: str(opportunity.record_id),
                    },
                )
            )
            return


def _plan_source_config_patches(
    plan: CommitPlan,
    current: WorkbookSnapshot,
    next_rows: dict[str, int],
    manifests: list[SourceManifest],
    run: RunResult,
) -> None:
    row_map = _row_map(current.tabs.get("Source Config", []), "Adapter ID")
    for manifest in manifests:
        adapter_id = manifest.adapter_id.value
        target_row = row_map.get(adapter_id, (next_rows["Source Config"], []))[0]
        if adapter_id not in row_map:
            next_rows["Source Config"] += 1
        values = {
            "Adapter ID": adapter_id,
            "Display Name": manifest.display_name,
            "Endpoint": "\n".join(manifest.fetch_endpoints),
            "Enabled": manifest.enabled,
            "Owner Approved": manifest.owner_approved,
            "Allowed Hosts": " | ".join(manifest.allowed_hosts),
            "Allowed Fields": " | ".join(manifest.allowed_fields),
            "Attribution": manifest.attribution,
            "Retention Class": manifest.retention_class.value,
            "Retention TTL Days": manifest.retention_ttl_days,
            "AI Processing Allowed": manifest.ai_processing_allowed,
            "Default Applicant Cost": manifest.default_applicant_cost.value,
            "Terms URL": str(manifest.terms_url or ""),
            "Terms Verified At": (
                manifest.terms_verified_at.isoformat() if manifest.terms_verified_at else ""
            ),
            "Rate Limit Per Minute": manifest.rate_limit_per_minute,
            "Checkpoint": run.checkpoints.get(adapter_id, manifest.checkpoint or ""),
        }
        plan.patches.append(
            CellPatch("Source Config", target_row, _row_values(SOURCE_CONFIG_COLUMNS, values))
        )


def _row_map(rows: list[list[Any]], id_column: str) -> dict[str, tuple[int, list[Any]]]:
    if not rows:
        return {}
    try:
        index = rows[0].index(id_column)
    except ValueError:
        return {}
    return {
        str(row[index]): (row_index, row)
        for row_index, row in enumerate(rows[1:], start=1)
        if len(row) > index and row[index] not in (None, "")
    }


def _current_value(row: list[Any], name: str) -> Any:
    return _value_at(row, OPPORTUNITY_COLUMNS.index(name))


def _value_at(row: list[Any], index: int) -> Any:
    return row[index] if len(row) > index else ""


def _row_values(columns: list[str], values: dict[str, Any]) -> dict[int, Any]:
    return {columns.index(name): sanitize_sheet_value(value) for name, value in values.items()}


def _values_to_row(columns: list[str], values: dict[str, Any]) -> list[Any]:
    return [values.get(column, "") for column in columns]


def _contiguous_values(values: dict[int, Any]) -> list[tuple[int, list[Any]]]:
    if not values:
        return []
    groups: list[tuple[int, list[Any]]] = []
    start = previous = min(values)
    current = [values[start]]
    for index in sorted(values)[1:]:
        if index == previous + 1:
            current.append(values[index])
        else:
            groups.append((start, current))
            start = index
            current = [values[index]]
        previous = index
    groups.append((start, current))
    return groups


def _update_cells_request(
    sheet_id: int,
    start_row: int,
    start_column: int,
    rows: list[list[Any]],
    *,
    trusted_formulas: bool,
) -> dict[str, Any]:
    return {
        "updateCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row,
                "endRowIndex": start_row + len(rows),
                "startColumnIndex": start_column,
                "endColumnIndex": start_column + max((len(row) for row in rows), default=0),
            },
            "rows": [
                {
                    "values": [
                        {"userEnteredValue": _user_entered_value(value, trusted_formulas)}
                        for value in row
                    ]
                }
                for row in rows
            ],
            "fields": "userEnteredValue",
        }
    }


def _user_entered_value(value: Any, trusted_formulas: bool) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, (int, float)):
        return {"numberValue": value}
    if isinstance(value, datetime):
        # Sheets serials are local wall-clock values in the workbook's Asia/Manila time zone.
        # Keep domain timestamps in UTC; convert only at this presentation boundary.
        manila = timezone(timedelta(hours=8))
        normalized = value.astimezone(manila).replace(tzinfo=None) if value.tzinfo else value
        sheets_epoch = datetime(1899, 12, 30)
        return {"numberValue": (normalized - sheets_epoch).total_seconds() / 86_400}
    if isinstance(value, date):
        sheets_epoch_date = date(1899, 12, 30)
        return {"numberValue": float((value - sheets_epoch_date).days)}
    text = str(value)
    if trusted_formulas and text.startswith("="):
        return {"formulaValue": text}
    return {"stringValue": str(sanitize_sheet_value(text))}


def _formatting_requests(
    sheet_ids: dict[str, int],
    created_titles: set[str],
    sheets: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    machine_header = _rgb("#F1F3F4")
    human_header = _rgb("#DCE8FF")
    dark_text = _rgb("#202124")
    for title, schema in TAB_SCHEMAS.items():
        if not schema.columns or title not in created_titles:
            continue
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_ids[title],
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": len(schema.columns),
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": machine_header,
                            "textFormat": {
                                "foregroundColor": dark_text,
                                "bold": True,
                            },
                            "horizontalAlignment": "LEFT",
                            "verticalAlignment": "MIDDLE",
                            "wrapStrategy": "WRAP",
                            "borders": {"bottom": {"style": "SOLID", "color": _rgb("#DADCE0")}},
                        }
                    },
                    "fields": "userEnteredFormat",
                }
            }
        )
        requests.append(
            {
                "addBanding": {
                    "bandedRange": {
                        "range": {
                            "sheetId": sheet_ids[title],
                            "startRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(schema.columns),
                        },
                        "rowProperties": {
                            "firstBandColor": {"red": 1, "green": 1, "blue": 1},
                            "secondBandColor": _rgb("#FAFBFC"),
                        },
                    }
                }
            }
        )
        user_columns = (
            USER_OWNED_OPPORTUNITY_COLUMNS
            if title == "Opportunities"
            else USER_OWNED_LEAD_COLUMNS
            if title == "Cold Outreach Leads"
            else USER_OWNED_MANUAL_INTAKE_COLUMNS
            if title == "Manual Intake"
            else set()
        )
        for start, end in _contiguous_indexes(
            sorted(schema.columns.index(name) for name in user_columns)
        ):
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_ids[title],
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": start,
                            "endColumnIndex": end,
                        },
                        "cell": {"userEnteredFormat": {"backgroundColor": human_header}},
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                }
            )
        requests.append(
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_ids[title],
                        "hidden": schema.hidden,
                        "gridProperties": {
                            "frozenRowCount": 1,
                            "frozenColumnCount": (
                                3
                                if title == "Opportunities"
                                else 2
                                if title == "Manual Intake"
                                else 0
                            ),
                            "hideGridlines": True,
                        },
                    },
                    "fields": (
                        "hidden,gridProperties.frozenRowCount,"
                        "gridProperties.frozenColumnCount,gridProperties.hideGridlines"
                    ),
                }
            }
        )
        if title == "Run Log":
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_ids[title],
                            "startRowIndex": 1,
                            "startColumnIndex": 2,
                            "endColumnIndex": 4,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {
                                    "type": "DATE_TIME",
                                    "pattern": "yyyy-mm-dd hh:mm",
                                }
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                }
            )
    requests.extend(_opportunity_guardrail_requests(sheet_ids, created_titles, sheets))
    requests.extend(_manual_intake_guardrail_requests(sheet_ids, created_titles))
    if "Manual Intake" in created_titles and "Manual Intake" in sheet_ids:
        requests.extend(_manual_intake_width_requests(sheet_ids["Manual Intake"]))
    requests.extend(_lead_guardrail_requests(sheet_ids, created_titles))
    requests.extend(_support_guardrail_requests(sheet_ids, created_titles))
    if "Dashboard" not in created_titles:
        return requests
    dashboard_id = sheet_ids["Dashboard"]
    requests.append(
        {
            "repeatCell": {
                "range": {"sheetId": dashboard_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 1, "green": 1, "blue": 1},
                        "textFormat": {
                            "foregroundColor": dark_text,
                            "bold": True,
                            "fontSize": 16,
                        },
                    }
                },
                "fields": "userEnteredFormat",
            }
        }
    )
    requests.extend(_dashboard_format_requests(dashboard_id))
    requests.extend(_dashboard_chart_requests(dashboard_id))
    return requests


def _presentation_cleanup_requests(
    sheets: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Remove generated presentation objects before recreating the v3.1 layout.

    The only dimension groups this migration owns are the exact v3.0 and v3.1
    advanced-detail ranges. Any other touching group is user structure and is
    rejected by the normal grouping guardrail.
    """
    requests: list[dict[str, Any]] = []
    formatting_sheets = dict(sheets)
    for title in ("Dashboard", "Opportunities"):
        sheet = sheets.get(title)
        if sheet is None:
            continue
        sheet_id = int(sheet["properties"]["sheetId"])
        if title == "Dashboard":
            requests.extend(
                [
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 23,
                                "endRowIndex": 24,
                                "startColumnIndex": 0,
                                "endColumnIndex": 6,
                            },
                            "cell": {},
                            "fields": "userEnteredFormat",
                        }
                    },
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 25,
                                "endRowIndex": 33,
                                "startColumnIndex": 3,
                                "endColumnIndex": 4,
                            },
                            "cell": {},
                            "fields": "userEnteredFormat.numberFormat",
                        }
                    },
                ]
            )
        elif title == "Opportunities":
            old_date_positions = {
                OPPORTUNITY_COLUMNS_V3_0.index("Deadline"),
                OPPORTUNITY_COLUMNS_V3_0.index("Next Action At"),
                OPPORTUNITY_COLUMNS_V3_0.index("Date Found"),
            }
            current_date_positions = {
                OPPORTUNITY_COLUMNS.index("Deadline"),
                OPPORTUNITY_COLUMNS.index("Next Action At"),
                OPPORTUNITY_COLUMNS.index("Date Found"),
            }
            for column_index in sorted(old_date_positions - current_date_positions):
                requests.append(
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 1,
                                "endRowIndex": 1000,
                                "startColumnIndex": column_index,
                                "endColumnIndex": column_index + 1,
                            },
                            "cell": {},
                            "fields": "userEnteredFormat.numberFormat",
                        }
                    }
                )
        generated_rule_indexes = [
            index
            for index, rule in enumerate(sheet.get("conditionalFormats", []))
            if _is_generated_conditional_format(title, sheet_id, rule)
        ]
        for index in reversed(generated_rule_indexes):
            requests.append(
                {
                    "deleteConditionalFormatRule": {
                        "sheetId": sheet_id,
                        "index": index,
                    }
                }
            )
        for banding in sheet.get("bandedRanges", []):
            banded_range = banding.get("range", {})
            if title == "Opportunities" and (
                int(banded_range.get("sheetId", -1)) == sheet_id
                and int(banded_range.get("startRowIndex", 0)) == 1
                and int(banded_range.get("startColumnIndex", 0)) == 0
                and int(banded_range.get("endColumnIndex", -1))
                in {
                    len(OPPORTUNITY_COLUMNS),
                    len(OPPORTUNITY_COLUMNS_V3_1),
                    len(OPPORTUNITY_COLUMNS_V3_0),
                }
            ):
                requests.append({"deleteBanding": {"bandedRangeId": int(banding["bandedRangeId"])}})
        for filter_view in sheet.get("filterViews", []):
            filter_id = int(filter_view["filterViewId"])
            if title == "Opportunities" and 310001 <= filter_id <= 310008:
                requests.append({"deleteFilterView": {"filterId": filter_id}})
        for chart in sheet.get("charts", []):
            if title == "Dashboard" and chart.get("spec", {}).get("title") in {
                "Opportunity Pipeline",
                "Weekly Submissions and Responses",
            }:
                requests.append({"deleteEmbeddedObject": {"objectId": int(chart["chartId"])}})

    opportunity = sheets.get("Opportunities")
    if opportunity is None:
        return requests, formatting_sheets
    sheet_id = int(opportunity["properties"]["sheetId"])
    owned_legacy_groups = {
        (
            OPPORTUNITY_COLUMNS_V3_0.index("Blockers"),
            len(OPPORTUNITY_COLUMNS_V3_0),
        ),
        (
            OPPORTUNITY_COLUMNS_V3_0.index("Blockers"),
            len(OPPORTUNITY_COLUMNS),
        ),
        (len(DAILY_OPPORTUNITY_COLUMNS_V3_1), len(OPPORTUNITY_COLUMNS_V3_1)),
        (len(DAILY_OPPORTUNITY_COLUMNS_V3_1), len(OPPORTUNITY_COLUMNS)),
    }
    retained_groups: list[dict[str, Any]] = []
    for group in opportunity.get("columnGroups", []):
        group_range = group.get("range", {})
        bounds = (
            int(group_range.get("startIndex", -1)),
            int(group_range.get("endIndex", -1)),
        )
        if bounds in owned_legacy_groups:
            requests.append(
                {
                    "deleteDimensionGroup": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": bounds[0],
                            "endIndex": bounds[1],
                        }
                    }
                }
            )
        else:
            retained_groups.append(group)
    formatting_sheets["Opportunities"] = {
        **opportunity,
        "columnGroups": retained_groups,
    }
    # Let _collapsed_column_group_requests preserve an existing exact v3.1
    # group or add it after the old v3.0 range is deleted.
    return requests, formatting_sheets


def _is_generated_conditional_format(title: str, sheet_id: int, rule: dict[str, Any]) -> bool:
    ranges = rule.get("ranges", [])
    if len(ranges) != 1:
        return False
    target = ranges[0]
    if int(target.get("sheetId", -1)) != sheet_id:
        return False
    condition = rule.get("booleanRule", {}).get("condition", {})
    condition_type = condition.get("type")
    values = {str(value.get("userEnteredValue", "")) for value in condition.get("values", [])}
    bounds = (
        int(target.get("startRowIndex", 0)),
        int(target.get("endRowIndex", -1)),
        int(target.get("startColumnIndex", 0)),
        int(target.get("endColumnIndex", -1)),
    )
    if title == "Dashboard":
        return (
            bounds == (4, 5, 1, 2)
            and condition_type == "TEXT_EQ"
            and bool(values)
            and values <= {"OK", "NOT STARTED", "STALE", "ERROR"}
        )
    if title != "Opportunities" or bounds[:2] != (1, 1000):
        return False
    current_indexes = {
        "stage": OPPORTUNITY_COLUMNS.index("Pipeline Stage"),
        "priority": OPPORTUNITY_COLUMNS.index("Priority"),
        "deadline": OPPORTUNITY_COLUMNS.index("Deadline"),
        "next_action": OPPORTUNITY_COLUMNS.index("Next Action At"),
        "processing": OPPORTUNITY_COLUMNS.index("Processing Status"),
        "qualification": OPPORTUNITY_COLUMNS.index("Qualification"),
        "eligibility": OPPORTUNITY_COLUMNS.index("Eligibility"),
        "applicant_cost": OPPORTUNITY_COLUMNS.index("Application Cost"),
    }
    old_indexes = {
        "deadline": OPPORTUNITY_COLUMNS_V3_0.index("Deadline"),
        "next_action": OPPORTUNITY_COLUMNS_V3_0.index("Next Action At"),
    }
    v31_indexes = {
        "deadline": OPPORTUNITY_COLUMNS_V3_1.index("Deadline"),
        "next_action": OPPORTUNITY_COLUMNS_V3_1.index("Next Action At"),
    }
    column_bounds = (bounds[2], bounds[3])
    if condition_type == "TEXT_EQ":
        return (
            (
                column_bounds == (current_indexes["stage"], current_indexes["stage"] + 1)
                and bool(values)
                and values <= set(PIPELINE_STAGES)
            )
            or (
                column_bounds == (current_indexes["priority"], current_indexes["priority"] + 1)
                and bool(values)
                and values <= set(PRIORITIES)
            )
            or (
                column_bounds == (current_indexes["processing"], current_indexes["processing"] + 1)
                and values == {"Error"}
            )
            or (
                column_bounds
                == (current_indexes["qualification"], current_indexes["qualification"] + 1)
                and bool(values)
                and values <= {"Eligible", "Needs review", "Ineligible"}
            )
            or (
                column_bounds
                == (current_indexes["eligibility"], current_indexes["eligibility"] + 1)
                and bool(values)
                and values <= {"Eligible", "Needs review", "Ineligible"}
            )
            or (
                column_bounds
                == (current_indexes["applicant_cost"], current_indexes["applicant_cost"] + 1)
                and bool(values)
                and values <= {"Free to apply", "Not stated", "Check cost", "Payment required"}
            )
        )
    if condition_type != "CUSTOM_FORMULA":
        return False
    single_column_indexes = {
        current_indexes["deadline"],
        current_indexes["next_action"],
        old_indexes["deadline"],
        old_indexes["next_action"],
        v31_indexes["deadline"],
        v31_indexes["next_action"],
    }
    return (bounds[3] == bounds[2] + 1 and bounds[2] in single_column_indexes) or column_bounds in {
        (0, len(DAILY_OPPORTUNITY_COLUMNS)),
        (0, OPPORTUNITY_COLUMNS_V3_0.index("Blockers")),
        (0, len(DAILY_OPPORTUNITY_COLUMNS_V3_1)),
    }


def _opportunity_guardrail_requests(
    sheet_ids: dict[str, int],
    created_titles: set[str],
    sheets: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    title = "Opportunities"
    if title not in created_titles:
        return []
    requests: list[dict[str, Any]] = []
    sheet_id = sheet_ids[title]
    stage_index = OPPORTUNITY_COLUMNS.index("Pipeline Stage")
    priority_index = OPPORTUNITY_COLUMNS.index("Priority")
    draft_review_index = OPPORTUNITY_COLUMNS.index("Draft Review")
    closed_reason_index = OPPORTUNITY_COLUMNS.index("Closed Reason")
    processing_index = OPPORTUNITY_COLUMNS.index("Processing Status")
    qualification_index = OPPORTUNITY_COLUMNS.index("Qualification")
    eligibility_index = OPPORTUNITY_COLUMNS.index("Eligibility")
    applicant_cost_index = OPPORTUNITY_COLUMNS.index("Application Cost")
    deadline_index = OPPORTUNITY_COLUMNS.index("Deadline")
    next_action_at_index = OPPORTUNITY_COLUMNS.index("Next Action At")
    stage_column = column_letter(stage_index)
    deadline_column = column_letter(deadline_index)
    next_action_at_column = column_letter(next_action_at_index)
    stage_colors = {
        "Inbox": _rgb("#E8EAED"),
        "Shortlisted": _rgb("#D2E3FC"),
        "Preparing": _rgb("#FEEFC3"),
        "Submitted": _rgb("#CBF0F8"),
        "Interview/Call": _rgb("#E8DAEF"),
        "Offer": _rgb("#CEEAD6"),
        "Won": _rgb("#B7E1CD"),
        "Closed": _rgb("#E8EAED"),
    }
    for column_index, values in (
        (stage_index, PIPELINE_STAGES),
        (priority_index, PRIORITIES),
        (draft_review_index, DRAFT_REVIEW_STATUSES),
        (closed_reason_index, CLOSED_REASONS),
    ):
        requests.append(_validation_request(sheet_id, column_index, values))
    for status, color in stage_colors.items():
        requests.append(_text_conditional_request(sheet_id, stage_index, status, color))
    for priority, color in {
        "High": _rgb("#D2E3FC"),
        "Medium": _rgb("#FEEFC3"),
        "Low": _rgb("#E8EAED"),
    }.items():
        requests.append(_text_conditional_request(sheet_id, priority_index, priority, color))
    for column_index, value, color in (
        (qualification_index, "Eligible", _rgb("#E6F4EA")),
        (qualification_index, "Needs review", _rgb("#FEEFC3")),
        (eligibility_index, "Eligible", _rgb("#E6F4EA")),
        (eligibility_index, "Needs review", _rgb("#FEEFC3")),
        (applicant_cost_index, "Free to apply", _rgb("#E6F4EA")),
        (applicant_cost_index, "Not stated", _rgb("#F1F3F4")),
        (applicant_cost_index, "Check cost", _rgb("#FEEFC3")),
        (applicant_cost_index, "Payment required", _rgb("#FAD2CF")),
    ):
        requests.append(_text_conditional_request(sheet_id, column_index, value, color))
    requests.extend(
        [
            _formula_conditional_request(
                sheet_id,
                next_action_at_index,
                f'=AND(${next_action_at_column}2<>"",${next_action_at_column}2<TODAY(),'
                f'${stage_column}2<>"Closed")',
                _rgb("#FAD2CF"),
            ),
            _formula_conditional_request(
                sheet_id,
                next_action_at_index,
                f"=AND(${next_action_at_column}2>=TODAY(),"
                f'${next_action_at_column}2<=TODAY()+3,${stage_column}2<>"Closed")',
                _rgb("#FEEFC3"),
            ),
            _formula_conditional_request(
                sheet_id,
                deadline_index,
                f'=AND(${deadline_column}2<>"",${deadline_column}2<TODAY(),'
                f'${stage_column}2<>"Closed")',
                _rgb("#FAD2CF"),
            ),
            _formula_conditional_request(
                sheet_id,
                deadline_index,
                f"=AND(${deadline_column}2>=TODAY(),${deadline_column}2<=TODAY()+3,"
                f'${stage_column}2<>"Closed")',
                _rgb("#FEEFC3"),
            ),
            _text_conditional_request(sheet_id, processing_index, "Error", _rgb("#F4C7F3")),
            _range_formula_conditional_request(
                sheet_id,
                0,
                len(DAILY_OPPORTUNITY_COLUMNS),
                f'=${stage_column}2="Closed"',
                _rgb("#F1F3F4"),
            ),
        ]
    )
    # Sheets coalesces adjacent dimension groups at the same depth. Three
    # back-to-back groups therefore become one larger group before the later
    # updateDimensionGroup requests run. Use the actual UI behavior directly:
    # one collapsed progressive-disclosure group for every non-daily column.
    requests.extend(
        _collapsed_column_group_requests(
            sheets[title], len(DAILY_OPPORTUNITY_COLUMNS), len(OPPORTUNITY_COLUMNS)
        )
    )
    requests.extend(_opportunity_width_requests(sheet_id))
    requests.extend(_opportunity_filter_view_requests(sheet_id))
    requests.append(
        {
            "addProtectedRange": {
                "protectedRange": {
                    "range": {"sheetId": sheet_id},
                    "description": "Machine-owned cells and row order",
                    "warningOnly": False,
                    "unprotectedRanges": _opportunity_unprotected_ranges(sheet_id),
                }
            }
        }
    )
    return requests


def _validation_request(sheet_id: int, column_index: int, values: list[str]) -> dict[str, Any]:
    return {
        "setDataValidation": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "endRowIndex": 1000,
                "startColumnIndex": column_index,
                "endColumnIndex": column_index + 1,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": value} for value in values],
                },
                "strict": True,
                "showCustomUi": True,
            },
        }
    }


def _text_conditional_request(
    sheet_id: int, column_index: int, value: str, color: dict[str, float]
) -> dict[str, Any]:
    return {
        "addConditionalFormatRule": {
            "index": 0,
            "rule": {
                "ranges": [
                    {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": 1000,
                        "startColumnIndex": column_index,
                        "endColumnIndex": column_index + 1,
                    }
                ],
                "booleanRule": {
                    "condition": {
                        "type": "TEXT_EQ",
                        "values": [{"userEnteredValue": value}],
                    },
                    "format": {"backgroundColor": color},
                },
            },
        }
    }


def _formula_conditional_request(
    sheet_id: int, column_index: int, formula: str, color: dict[str, float]
) -> dict[str, Any]:
    return {
        "addConditionalFormatRule": {
            "index": 0,
            "rule": {
                "ranges": [
                    {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": 1000,
                        "startColumnIndex": column_index,
                        "endColumnIndex": column_index + 1,
                    }
                ],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": formula}],
                    },
                    "format": {"backgroundColor": color},
                },
            },
        }
    }


def _range_formula_conditional_request(
    sheet_id: int,
    start_column: int,
    end_column: int,
    formula: str,
    color: dict[str, float],
) -> dict[str, Any]:
    request = _formula_conditional_request(sheet_id, start_column, formula, color)
    rule_range = request["addConditionalFormatRule"]["rule"]["ranges"][0]
    rule_range["endColumnIndex"] = end_column
    return request


def _collapsed_column_group_requests(
    sheet: dict[str, Any], start: int, end: int
) -> list[dict[str, Any]]:
    sheet_id = int(sheet["properties"]["sheetId"])
    dimension_range = {
        "sheetId": sheet_id,
        "dimension": "COLUMNS",
        "startIndex": start,
        "endIndex": end,
    }
    exact_group: dict[str, Any] | None = None
    conflicts: list[dict[str, Any]] = []
    for group in sheet.get("columnGroups", []):
        group_range = group.get("range", {})
        group_start = int(group_range.get("startIndex", 0))
        group_end = int(group_range.get("endIndex", 0))
        if group_start == start and group_end == end:
            exact_group = group
            continue
        # Adjacent same-depth groups can be coalesced by Sheets just like the
        # three original advanced-detail groups were. Treat both touching and
        # overlapping user groups as conflicts rather than reshaping them.
        if group_start <= end and group_end >= start:
            conflicts.append(group)
    if conflicts:
        raise WorkbookConflict(
            "Opportunities has an existing column group touching the intended "
            "advanced-details range; remove that custom group or restore the snapshot"
        )
    if exact_group is not None:
        if bool(exact_group.get("collapsed")):
            return []
        return [
            {
                "updateDimensionGroup": {
                    "dimensionGroup": {
                        "range": dimension_range,
                        "depth": int(exact_group["depth"]),
                        "collapsed": True,
                    },
                    "fields": "collapsed",
                }
            }
        ]
    return [
        {"addDimensionGroup": {"range": dimension_range}},
        {
            "updateDimensionGroup": {
                "dimensionGroup": {
                    "range": dimension_range,
                    "depth": 1,
                    "collapsed": True,
                },
                "fields": "collapsed",
            }
        },
    ]


def _opportunity_width_requests(sheet_id: int) -> list[dict[str, Any]]:
    widths = {
        "Pipeline Stage": 120,
        "Priority": 85,
        "Company": 180,
        "Role": 260,
        "Track": 115,
        "Fit Band": 105,
        "Final Score": 80,
        "Eligibility": 105,
        "Work Arrangement": 120,
        "Location": 190,
        "Salary": 135,
        "Deadline": 105,
        "Apply URL": 170,
        "Next Action": 210,
        "Next Action At": 120,
        "Source": 105,
        "Date Found": 115,
        "Notes": 260,
        "Blockers": 240,
        "Missing Requirements": 240,
        "Match Rationale": 320,
        "Description Snippet": 360,
        "Requirements": 320,
        "AI Draft": 380,
    }
    requests = [
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": index,
                    "endIndex": index + 1,
                },
                "properties": {"pixelSize": widths.get(name, 135)},
                "fields": "pixelSize",
            }
        }
        for index, name in enumerate(OPPORTUNITY_COLUMNS)
    ]
    requests.extend(
        [
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": 0,
                        "endIndex": 1,
                    },
                    "properties": {"pixelSize": 42},
                    "fields": "pixelSize",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": 1,
                        "endIndex": 1000,
                    },
                    "properties": {"pixelSize": 32},
                    "fields": "pixelSize",
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": 1000,
                        "startColumnIndex": 0,
                        "endColumnIndex": len(OPPORTUNITY_COLUMNS),
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "verticalAlignment": "MIDDLE",
                            "wrapStrategy": "CLIP",
                        }
                    },
                    "fields": "userEnteredFormat(verticalAlignment,wrapStrategy)",
                }
            },
        ]
    )
    date_only = ("Deadline", "Next Action At")
    date_times = (
        "Date Found",
        "Published At",
        "Submitted At",
        "Response At",
        "Interview/Call At",
        "Closed At",
        "Drafted At",
        "Draft Review Observed At",
        "Last Seen At",
        "Retrieved At",
        "Source Deadline",
        "Analyzed At",
        "Updated At",
    )
    for names, pattern in ((date_only, "yyyy-mm-dd"), (date_times, "yyyy-mm-dd hh:mm")):
        for name in names:
            index = OPPORTUNITY_COLUMNS.index(name)
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "endRowIndex": 1000,
                            "startColumnIndex": index,
                            "endColumnIndex": index + 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {"type": "DATE", "pattern": pattern}
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                }
            )
    return requests


def _filter_condition(column: str, value: str) -> dict[str, Any]:
    return {
        "columnIndex": OPPORTUNITY_COLUMNS.index(column),
        "filterCriteria": {
            "condition": {
                "type": "TEXT_EQ",
                "values": [{"userEnteredValue": value}],
            }
        },
    }


def _formula_filter_condition(column: str, formula: str) -> dict[str, Any]:
    return {
        "columnIndex": OPPORTUNITY_COLUMNS.index(column),
        "filterCriteria": {
            "condition": {
                "type": "CUSTOM_FORMULA",
                "values": [{"userEnteredValue": formula}],
            }
        },
    }


def _opportunity_filter_view_requests(sheet_id: int) -> list[dict[str, Any]]:
    stage = column_letter(OPPORTUNITY_COLUMNS.index("Pipeline Stage"))
    deadline = column_letter(OPPORTUNITY_COLUMNS.index("Deadline"))
    next_action_at = column_letter(OPPORTUNITY_COLUMNS.index("Next Action At"))
    definitions: list[tuple[int, str, list[dict[str, Any]], str, str]] = [
        (
            310001,
            "Technical",
            [_filter_condition("Track", "Technical")],
            "Final Score",
            "DESCENDING",
        ),
        (
            310002,
            "VA & Freelance",
            [_filter_condition("Track", "VA/Freelance")],
            "Final Score",
            "DESCENDING",
        ),
        (
            310003,
            "Inbox",
            [_filter_condition("Pipeline Stage", "Inbox")],
            "Date Found",
            "DESCENDING",
        ),
        (
            310004,
            "Strong Fits",
            [
                _filter_condition("Fit Band", "Strong Fit"),
                _formula_filter_condition("Pipeline Stage", f'=${stage}2<>"Closed"'),
            ],
            "Final Score",
            "DESCENDING",
        ),
        (
            310005,
            "Deadline Soon",
            [
                _formula_filter_condition(
                    "Deadline",
                    f'=AND(${deadline}2<>"",${deadline}2<=TODAY()+7,${stage}2<>"Closed")',
                )
            ],
            "Deadline",
            "ASCENDING",
        ),
        (
            310006,
            "Active Pipeline",
            [
                _formula_filter_condition(
                    "Pipeline Stage",
                    f'=OR(${stage}2="Submitted",${stage}2="Interview/Call",${stage}2="Offer")',
                )
            ],
            "Next Action At",
            "ASCENDING",
        ),
        (
            310007,
            "Follow-Up Due",
            [
                _formula_filter_condition(
                    "Next Action At",
                    f'=AND(${next_action_at}2<>"",${next_action_at}2<=TODAY(),${stage}2<>"Closed")',
                )
            ],
            "Next Action At",
            "ASCENDING",
        ),
        (
            310008,
            "Closed Archive",
            [_filter_condition("Pipeline Stage", "Closed")],
            "Closed At",
            "DESCENDING",
        ),
    ]
    return [
        {
            "addFilterView": {
                "filter": {
                    "filterViewId": view_id,
                    "title": title,
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": 1000,
                        "startColumnIndex": 0,
                        "endColumnIndex": len(OPPORTUNITY_COLUMNS),
                    },
                    "filterSpecs": filters,
                    "sortSpecs": [
                        {
                            "dimensionIndex": OPPORTUNITY_COLUMNS.index(sort_column),
                            "sortOrder": sort_order,
                        }
                    ],
                }
            }
        }
        for view_id, title, filters, sort_column, sort_order in definitions
    ]


def _lead_guardrail_requests(
    sheet_ids: dict[str, int], created_titles: set[str]
) -> list[dict[str, Any]]:
    title = "Cold Outreach Leads"
    if title not in created_titles:
        return []
    sheet_id = sheet_ids[title]
    status_index = COLD_LEAD_COLUMNS.index("Review Status")
    user_indexes = sorted(COLD_LEAD_COLUMNS.index(name) for name in USER_OWNED_LEAD_COLUMNS)
    return [
        {
            "setDataValidation": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": status_index,
                    "endColumnIndex": status_index + 1,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [
                            {"userEnteredValue": value}
                            for value in (
                                "New",
                                "Needs Review",
                                "Approved",
                                "Applied/Sent",
                                "Response",
                                "Closed/Rejected",
                            )
                        ],
                    },
                    "strict": True,
                    "showCustomUi": True,
                },
            }
        },
        {
            "addProtectedRange": {
                "protectedRange": {
                    "range": {"sheetId": sheet_id},
                    "description": "Machine-owned lead analysis cells and row order",
                    "warningOnly": False,
                    "unprotectedRanges": [
                        {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "startColumnIndex": start,
                            "endColumnIndex": end,
                        }
                        for start, end in _contiguous_indexes(user_indexes)
                    ],
                }
            }
        },
    ]


_SUPPORT_TITLES = {
    "Excluded",
    "Source Config",
    "Lists & Enums",
    "Run Log",
    "Dedupe Index",
    "System Events",
}


def _manual_intake_guardrail_requests(
    sheet_ids: dict[str, int], selected_titles: set[str]
) -> list[dict[str, Any]]:
    title = "Manual Intake"
    if title not in selected_titles or title not in sheet_ids:
        return []
    sheet_id = sheet_ids[title]
    requests = [
        _validation_request(
            sheet_id,
            MANUAL_INTAKE_COLUMNS.index("Platform"),
            [
                "LinkedIn",
                "JobStreet",
                "Indeed",
                "PhilJobNet",
                "OnlineJobs.ph",
                "Wellfound",
                "Upwork",
                "Other",
            ],
        ),
        _validation_request(
            sheet_id,
            MANUAL_INTAKE_COLUMNS.index("Work Arrangement"),
            ["remote", "hybrid", "onsite", "unknown"],
        ),
        _validation_request(
            sheet_id,
            MANUAL_INTAKE_COLUMNS.index("Application Cost"),
            ["Free to apply", "Not stated", "Check cost", "Payment required"],
        ),
    ]
    requests.append(
        {
            "addProtectedRange": {
                "protectedRange": {
                    "range": {"sheetId": sheet_id},
                    "description": "Manual intake input and machine import status",
                    "warningOnly": False,
                    "unprotectedRanges": _manual_intake_unprotected_ranges(sheet_id),
                }
            }
        }
    )
    return requests


def _manual_intake_unprotected_ranges(sheet_id: int) -> list[dict[str, int]]:
    user_indexes = sorted(
        MANUAL_INTAKE_COLUMNS.index(name) for name in USER_OWNED_MANUAL_INTAKE_COLUMNS
    )
    return [
        {
            "sheetId": sheet_id,
            "startRowIndex": 1,
            "startColumnIndex": start,
            "endColumnIndex": end,
        }
        for start, end in _contiguous_indexes(user_indexes)
    ]


def _manual_intake_width_requests(sheet_id: int) -> list[dict[str, Any]]:
    widths = {
        "Platform": 120,
        "Listing URL": 250,
        "Company": 180,
        "Role": 240,
        "Location": 180,
        "Work Arrangement": 135,
        "Application Cost": 140,
        "Description / Requirements": 360,
        "Date Added": 110,
        "Import Status": 105,
        "Import Message": 190,
        "Imported Record ID": 240,
    }
    requests = [
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": index,
                    "endIndex": index + 1,
                },
                "properties": {"pixelSize": widths[name]},
                "fields": "pixelSize",
            }
        }
        for index, name in enumerate(MANUAL_INTAKE_COLUMNS)
    ]
    requests.append(
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": 1000,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(MANUAL_INTAKE_COLUMNS),
                },
                "cell": {
                    "userEnteredFormat": {
                        "verticalAlignment": "MIDDLE",
                        "wrapStrategy": "CLIP",
                    }
                },
                "fields": "userEnteredFormat(verticalAlignment,wrapStrategy)",
            }
        }
    )
    date_index = MANUAL_INTAKE_COLUMNS.index("Date Added")
    requests.append(
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": 1000,
                    "startColumnIndex": date_index,
                    "endColumnIndex": date_index + 1,
                },
                "cell": {
                    "userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}}
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        }
    )
    return requests


def _opportunity_unprotected_ranges(sheet_id: int) -> list[dict[str, int]]:
    user_indexes = sorted(
        OPPORTUNITY_COLUMNS.index(name) for name in USER_OWNED_OPPORTUNITY_COLUMNS
    )
    return [
        {
            "sheetId": sheet_id,
            "startRowIndex": 1,
            "startColumnIndex": start,
            "endColumnIndex": end,
        }
        for start, end in _contiguous_indexes(user_indexes)
    ]


def _whole_sheet_protection(sheet: dict[str, Any]) -> dict[str, Any] | None:
    sheet_id = sheet["properties"]["sheetId"]
    bounded_fields = {
        "startRowIndex",
        "endRowIndex",
        "startColumnIndex",
        "endColumnIndex",
    }
    for protection in sheet.get("protectedRanges", []):
        protected_range = protection.get("range", {})
        if protected_range.get("sheetId") == sheet_id and not bounded_fields.intersection(
            protected_range
        ):
            return cast(dict[str, Any], protection)
    return None


def _sheet_protection_request(
    sheet: dict[str, Any],
    *,
    description: str,
    unprotected_ranges: list[dict[str, int]],
) -> dict[str, Any]:
    sheet_id = sheet["properties"]["sheetId"]
    protected_range: dict[str, Any] = {
        "range": {"sheetId": sheet_id},
        "description": description,
        "warningOnly": False,
        "unprotectedRanges": unprotected_ranges,
    }
    existing = _whole_sheet_protection(sheet)
    if existing is None:
        return {"addProtectedRange": {"protectedRange": protected_range}}
    protected_range["protectedRangeId"] = existing["protectedRangeId"]
    return {
        "updateProtectedRange": {
            "protectedRange": protected_range,
            "fields": "range,description,warningOnly,unprotectedRanges",
        }
    }


def _support_guardrail_requests(
    sheet_ids: dict[str, int], selected_titles: set[str]
) -> list[dict[str, Any]]:
    return [
        {
            "addProtectedRange": {
                "protectedRange": {
                    "range": {"sheetId": sheet_ids[title]},
                    "description": "Machine-managed support data",
                    "warningOnly": False,
                }
            }
        }
        for title in _SUPPORT_TITLES & selected_titles
        if title in sheet_ids
    ]


def _dashboard_format_requests(dashboard_id: int) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = [
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": dashboard_id,
                    "gridProperties": {"frozenRowCount": 1, "hideGridlines": True},
                },
                "fields": "gridProperties.frozenRowCount,gridProperties.hideGridlines",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": dashboard_id,
                    "startRowIndex": 1,
                    "endRowIndex": 2,
                    "startColumnIndex": 0,
                    "endColumnIndex": 6,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": _rgb("#E8F0FE"),
                        "textFormat": {"foregroundColor": _rgb("#174EA6"), "bold": True},
                    }
                },
                "fields": "userEnteredFormat",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": dashboard_id,
                    "startRowIndex": 7,
                    "endRowIndex": 8,
                    "startColumnIndex": 0,
                    "endColumnIndex": 4,
                },
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {
                            "foregroundColor": _rgb("#1A73E8"),
                            "bold": True,
                            "fontSize": 18,
                        },
                        "horizontalAlignment": "CENTER",
                    }
                },
                "fields": "userEnteredFormat",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": dashboard_id,
                    "startRowIndex": 3,
                    "endRowIndex": 4,
                    "startColumnIndex": 1,
                    "endColumnIndex": 2,
                },
                "cell": {
                    "userEnteredFormat": {
                        "numberFormat": {"type": "DATE_TIME", "pattern": "yyyy-mm-dd hh:mm"}
                    }
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        },
        {
            "repeatCell": {
                "range": {
                    "sheetId": dashboard_id,
                    "startRowIndex": 60,
                    "endRowIndex": 68,
                    "startColumnIndex": 3,
                    "endColumnIndex": 4,
                },
                "cell": {
                    "userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}}
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        },
    ]
    for row_index in (6, 9, 13, 59):
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": dashboard_id,
                        "startRowIndex": row_index,
                        "endRowIndex": row_index + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 6,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": _rgb("#F1F3F4"),
                            "textFormat": {"bold": True, "foregroundColor": _rgb("#202124")},
                        }
                    },
                    "fields": "userEnteredFormat",
                }
            }
        )
    for status, color in {
        "OK": _rgb("#CEEAD6"),
        "NOT STARTED": _rgb("#E8EAED"),
        "STALE": _rgb("#FEEFC3"),
        "ERROR": _rgb("#F4C7F3"),
    }.items():
        request = _text_conditional_request(dashboard_id, 1, status, color)
        rule_range = request["addConditionalFormatRule"]["rule"]["ranges"][0]
        rule_range["startRowIndex"] = 4
        rule_range["endRowIndex"] = 5
        requests.append(request)
    for index, width in enumerate((135, 180, 220, 190, 115, 95)):
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": dashboard_id,
                        "dimension": "COLUMNS",
                        "startIndex": index,
                        "endIndex": index + 1,
                    },
                    "properties": {"pixelSize": width},
                    "fields": "pixelSize",
                }
            }
        )
    return requests


def _dashboard_chart_requests(dashboard_id: int) -> list[dict[str, Any]]:
    pipeline = _dashboard_basic_chart(
        dashboard_id,
        title="Opportunity Pipeline",
        start_row=59,
        end_row=68,
        domain_column=0,
        series_column=1,
        anchor_row=24,
        anchor_column=0,
    )
    weekly = {
        "addChart": {
            "chart": {
                "spec": {
                    "title": "Weekly Submissions and Responses",
                    "fontName": "Arial",
                    "backgroundColor": _rgb("#FFFFFF"),
                    "basicChart": {
                        "chartType": "LINE",
                        "legendPosition": "BOTTOM_LEGEND",
                        "axis": [
                            {"position": "BOTTOM_AXIS", "title": "Week"},
                            {"position": "LEFT_AXIS", "title": "Count"},
                        ],
                        "domains": [
                            {
                                "domain": {
                                    "sourceRange": {
                                        "sources": [
                                            {
                                                "sheetId": dashboard_id,
                                                "startRowIndex": 59,
                                                "endRowIndex": 68,
                                                "startColumnIndex": 3,
                                                "endColumnIndex": 4,
                                            }
                                        ]
                                    }
                                }
                            }
                        ],
                        "series": [
                            {
                                "series": {
                                    "sourceRange": {
                                        "sources": [
                                            {
                                                "sheetId": dashboard_id,
                                                "startRowIndex": 59,
                                                "endRowIndex": 68,
                                                "startColumnIndex": column,
                                                "endColumnIndex": column + 1,
                                            }
                                        ]
                                    }
                                },
                                "targetAxis": "LEFT_AXIS",
                                "colorStyle": {"rgbColor": color},
                            }
                            for column, color in (
                                (4, _rgb("#1A73E8")),
                                (5, _rgb("#188038")),
                            )
                        ],
                        "headerCount": 1,
                    },
                },
                "position": {
                    "overlayPosition": {
                        "anchorCell": {
                            "sheetId": dashboard_id,
                            "rowIndex": 41,
                            "columnIndex": 0,
                        },
                        "widthPixels": 620,
                        "heightPixels": 320,
                    }
                },
            }
        }
    }
    return [pipeline, weekly]


def _dashboard_basic_chart(
    sheet_id: int,
    *,
    title: str,
    start_row: int,
    end_row: int,
    domain_column: int,
    series_column: int,
    anchor_row: int,
    anchor_column: int,
) -> dict[str, Any]:
    return {
        "addChart": {
            "chart": {
                "spec": {
                    "title": title,
                    "fontName": "Arial",
                    "backgroundColor": _rgb("#FFFFFF"),
                    "basicChart": {
                        "chartType": "BAR",
                        "legendPosition": "NO_LEGEND",
                        "axis": [
                            {"position": "BOTTOM_AXIS", "title": "Count"},
                            {"position": "LEFT_AXIS", "title": "Stage"},
                        ],
                        "domains": [
                            {
                                "domain": {
                                    "sourceRange": {
                                        "sources": [
                                            {
                                                "sheetId": sheet_id,
                                                "startRowIndex": start_row,
                                                "endRowIndex": end_row,
                                                "startColumnIndex": domain_column,
                                                "endColumnIndex": domain_column + 1,
                                            }
                                        ]
                                    }
                                }
                            }
                        ],
                        "series": [
                            {
                                "series": {
                                    "sourceRange": {
                                        "sources": [
                                            {
                                                "sheetId": sheet_id,
                                                "startRowIndex": start_row,
                                                "endRowIndex": end_row,
                                                "startColumnIndex": series_column,
                                                "endColumnIndex": series_column + 1,
                                            }
                                        ]
                                    }
                                },
                                "colorStyle": {"rgbColor": _rgb("#1A73E8")},
                            }
                        ],
                        "headerCount": 1,
                    },
                },
                "position": {
                    "overlayPosition": {
                        "anchorCell": {
                            "sheetId": sheet_id,
                            "rowIndex": anchor_row,
                            "columnIndex": anchor_column,
                        },
                        "widthPixels": 520,
                        "heightPixels": 320,
                    }
                },
            }
        }
    }


def _contiguous_indexes(indexes: list[int]) -> list[tuple[int, int]]:
    if not indexes:
        return []
    groups: list[tuple[int, int]] = []
    start = previous = indexes[0]
    for index in indexes[1:]:
        if index != previous + 1:
            groups.append((start, previous + 1))
            start = index
        previous = index
    groups.append((start, previous + 1))
    return groups


def _rgb(hex_color: str) -> dict[str, float]:
    value = hex_color.removeprefix("#")
    if len(value) != 6:
        raise ValueError(f"Invalid RGB color: {hex_color}")
    return {
        "red": int(value[0:2], 16) / 255,
        "green": int(value[2:4], 16) / 255,
        "blue": int(value[4:6], 16) / 255,
    }

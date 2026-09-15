from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from jobhunt.models import (
    ListingAvailabilityUpdate,
    Opportunity,
    RunResult,
    SourceManifest,
    WorkbookSnapshot,
)


class PendingRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initial_snapshot: WorkbookSnapshot
    opportunities: list[Opportunity]
    run: RunResult
    manifests: list[SourceManifest]
    availability_updates: list[ListingAvailabilityUpdate] = Field(default_factory=list)


class PendingRunStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def save(self, pending: PendingRun) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(str(pending.run.run_id))
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(pending.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, path)
        return path

    def load(self, run_id: str) -> PendingRun:
        path = self.path_for(run_id)
        if not path.is_file():
            raise FileNotFoundError(f"Pending run not found: {run_id}")
        try:
            return PendingRun.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Pending run is invalid: {run_id}") from exc

    def delete(self, run_id: str) -> None:
        try:
            self.path_for(run_id).unlink()
        except FileNotFoundError:
            pass

    def path_for(self, run_id: str) -> Path:
        if not run_id or any(
            character not in "0123456789abcdef-" for character in run_id.casefold()
        ):
            raise ValueError("Run ID must be a UUID-like identifier")
        return self.directory / f"{run_id}.json"

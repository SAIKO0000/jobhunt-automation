from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path

from jobhunt.models import WorkbookSnapshot


class SnapshotStore(ABC):
    @abstractmethod
    def save(self, snapshot: WorkbookSnapshot, *, reason: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def load(self, identifier: str) -> WorkbookSnapshot:
        raise NotImplementedError


class LocalSnapshotStore(SnapshotStore):
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def save(self, snapshot: WorkbookSnapshot, *, reason: str) -> str:
        self.directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        safe_reason = "".join(
            character for character in reason if character.isalnum() or character in "-_"
        )
        path = self.directory / f"{timestamp}-{safe_reason or 'snapshot'}.json"
        path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
        return str(path)

    def load(self, identifier: str) -> WorkbookSnapshot:
        path = Path(identifier).resolve()
        root = self.directory.resolve()
        if path != root and root not in path.parents:
            raise ValueError("Snapshot path is outside the configured snapshot directory")
        return WorkbookSnapshot.model_validate_json(path.read_text(encoding="utf-8"))

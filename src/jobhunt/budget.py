from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jobhunt.models import AnalysisQuotaState


class QuotaExceeded(RuntimeError):
    """Raised before an analysis request that would exceed a local row limit."""


class AnalysisQuotaLedger(ABC):
    @abstractmethod
    def load(self, now: datetime | None = None) -> AnalysisQuotaState:
        raise NotImplementedError

    @abstractmethod
    def save(self, state: AnalysisQuotaState) -> None:
        raise NotImplementedError

    def reserve(
        self,
        *,
        max_rows_per_day: int,
        now: datetime | None = None,
    ) -> AnalysisQuotaState:
        current = self.load(now)
        if current.daily_rows >= max_rows_per_day:
            raise QuotaExceeded("Daily AI row limit reached")
        current.daily_rows += 1
        self.save(current)
        return current

    def reconcile_usage(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        now: datetime | None = None,
    ) -> AnalysisQuotaState:
        current = self.load(now)
        current.input_tokens += input_tokens
        current.output_tokens += output_tokens
        self.save(current)
        return current


class FileAnalysisQuotaLedger(AnalysisQuotaLedger):
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, now: datetime | None = None) -> AnalysisQuotaState:
        current_time = now or datetime.now(UTC)
        month = current_time.strftime("%Y-%m")
        day = current_time.date().isoformat()
        try:
            raw: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
            state = AnalysisQuotaState.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValueError):
            state = AnalysisQuotaState(month=month, day=day)
        if state.month != month:
            return AnalysisQuotaState(month=month, day=day)
        if state.day != day:
            state.day = day
            state.daily_rows = 0
        return state

    def save(self, state: AnalysisQuotaState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


class MemoryAnalysisQuotaLedger(AnalysisQuotaLedger):
    def __init__(self) -> None:
        self.state: AnalysisQuotaState | None = None

    def load(self, now: datetime | None = None) -> AnalysisQuotaState:
        current_time = now or datetime.now(UTC)
        month = current_time.strftime("%Y-%m")
        day = current_time.date().isoformat()
        if self.state is None or self.state.month != month:
            self.state = AnalysisQuotaState(month=month, day=day)
        elif self.state.day != day:
            self.state.day = day
            self.state.daily_rows = 0
        return self.state.model_copy(deep=True)

    def save(self, state: AnalysisQuotaState) -> None:
        self.state = state.model_copy(deep=True)

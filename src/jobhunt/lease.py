from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class LeaseUnavailable(RuntimeError):
    """Raised when another non-expired run owns the execution lease."""


class RunLease(ABC):
    @abstractmethod
    def acquire(self, run_id: str, ttl: timedelta) -> None:
        raise NotImplementedError

    @abstractmethod
    def release(self, run_id: str) -> None:
        raise NotImplementedError


class FileRunLease(RunLease):
    def __init__(self, path: Path) -> None:
        self.path = path

    def acquire(self, run_id: str, ttl: timedelta) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = _lease_payload(run_id, ttl)
        for _ in range(2):
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                existing = _read_lease(self.path)
                if existing and _expires_at(existing) > datetime.now(UTC):
                    raise LeaseUnavailable(
                        f"Run lease is owned by {existing.get('run_id')}"
                    ) from None
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            return
        raise LeaseUnavailable("Unable to recover an expired run lease")

    def release(self, run_id: str) -> None:
        existing = _read_lease(self.path)
        if existing and existing.get("run_id") != run_id:
            raise LeaseUnavailable("Refusing to release a lease owned by another run")
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class LeaseContext:
    def __init__(self, lease: RunLease, run_id: str, ttl: timedelta) -> None:
        self.lease = lease
        self.run_id = run_id
        self.ttl = ttl

    def __enter__(self) -> LeaseContext:
        self.lease.acquire(self.run_id, self.ttl)
        return self

    def __exit__(self, *_: object) -> None:
        self.lease.release(self.run_id)


def _lease_payload(run_id: str, ttl: timedelta) -> dict[str, str]:
    now = datetime.now(UTC)
    return {
        "run_id": run_id,
        "acquired_at": now.isoformat(),
        "expires_at": (now + ttl).isoformat(),
    }


def _read_lease(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _expires_at(value: dict[str, Any]) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value["expires_at"]).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (KeyError, ValueError):
        return datetime.min.replace(tzinfo=UTC)

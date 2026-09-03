from __future__ import annotations

import sys
from typing import Protocol


class CredentialStore(Protocol):
    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


class MemoryCredentialStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


class WindowsCredentialStore:
    """Credential Locker access with an explicit, fail-closed Windows backend."""

    service_name = "jobhunt-automation"
    backend_name: str

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows Credential Manager is required on this platform")
        try:
            import keyring
            from keyring.backends.Windows import WinVaultKeyring
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Install the sheets or gemini extra to use secure credentials"
            ) from exc
        backend = WinVaultKeyring()  # type: ignore[no-untyped-call, unused-ignore]
        if backend.priority <= 0:
            raise RuntimeError("The Windows Credential Manager backend is unavailable")
        keyring.set_keyring(backend)
        active = keyring.get_keyring()
        if not isinstance(active, WinVaultKeyring):
            raise RuntimeError("Refusing insecure, plaintext, null, or unsupported keyring backend")
        self._keyring = keyring
        self.backend_name = f"{active.__class__.__module__}.{active.__class__.__name__}"

    def get(self, name: str) -> str | None:
        return self._keyring.get_password(self.service_name, name)

    def set(self, name: str, value: str) -> None:
        if not value:
            raise ValueError("Refusing to store an empty credential")
        self._keyring.set_password(self.service_name, name, value)

    def delete(self, name: str) -> None:
        try:
            self._keyring.delete_password(self.service_name, name)
        except self._keyring.errors.PasswordDeleteError:
            pass


GOOGLE_CREDENTIAL_NAMES = (
    "google.client_id",
    "google.client_secret",
    "google.refresh_token",
    "google.token_uri",
    "google.scopes",
)
GEMINI_KEY_NAME = "gemini.api_key"

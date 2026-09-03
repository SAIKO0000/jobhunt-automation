from __future__ import annotations

from pathlib import Path
from typing import Any

from jobhunt.credentials import CredentialStore

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


def authorize_google(client_secrets_path: Path, store: CredentialStore) -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("Install the sheets extra to authorize Google Sheets") from exc
    if not client_secrets_path.is_file():
        raise ValueError("Desktop OAuth client-secrets file does not exist")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), [SHEETS_SCOPE])
    credentials = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True)
    if not credentials.refresh_token:
        raise RuntimeError("Google did not return a refresh token; authorization was not stored")
    scopes = set(credentials.scopes or ())
    if scopes != {SHEETS_SCOPE}:
        raise RuntimeError("Google returned scopes outside the exact Sheets-only request")
    values = {
        "google.client_id": credentials.client_id,
        "google.client_secret": credentials.client_secret,
        "google.refresh_token": credentials.refresh_token,
        "google.token_uri": credentials.token_uri,
        "google.scopes": SHEETS_SCOPE,
    }
    if any(not value for value in values.values()):
        raise RuntimeError("Google returned incomplete desktop credentials")
    for name, value in values.items():
        store.set(name, str(value))


def load_google_credentials(store: CredentialStore) -> Any:
    try:
        from google.oauth2.credentials import Credentials
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("Install the sheets extra to use Google Sheets") from exc
    values = {
        name: store.get(f"google.{name}")
        for name in ("client_id", "client_secret", "refresh_token", "token_uri", "scopes")
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise PermissionError(f"Stored Google credentials are incomplete: {', '.join(missing)}")
    if values["scopes"] != SHEETS_SCOPE:
        raise PermissionError("Stored Google credentials contain an unapproved scope")
    return Credentials(  # type: ignore[no-untyped-call]
        token=None,
        refresh_token=values["refresh_token"],
        token_uri=values["token_uri"],
        client_id=values["client_id"],
        client_secret=values["client_secret"],
        scopes=[SHEETS_SCOPE],
    )

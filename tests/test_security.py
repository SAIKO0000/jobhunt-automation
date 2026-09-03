from __future__ import annotations

import httpx
import pytest

from jobhunt.security import (
    SafeHttpClient,
    UnsafeRemoteResponse,
    sanitize_sheet_value,
    validate_https_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://remoteok.com/api",
        "https://127.0.0.1/api",
        "https://evil.example/api",
        "https://user:pass@remoteok.com/api",
        "https://remoteok.com:8443/api",
    ],
)
def test_url_allowlist_rejects_unsafe_targets(url: str) -> None:
    with pytest.raises(UnsafeRemoteResponse):
        validate_https_url(url, {"remoteok.com"})


def test_redirect_cannot_escape_host_allowlist() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/payload"})

    with SafeHttpClient(
        allowed_hosts={"remoteok.com"},
        max_response_bytes=100,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(UnsafeRemoteResponse):
            client.get_bytes("https://remoteok.com/api")


def test_response_limit_is_enforced() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 101))
    with SafeHttpClient(
        allowed_hosts={"remoteok.com"}, max_response_bytes=100, transport=transport
    ) as client:
        with pytest.raises(UnsafeRemoteResponse):
            client.get_bytes("https://remoteok.com/api")


@pytest.mark.parametrize("value", ["=IMPORTXML(A1)", "+cmd", "-1+2", "@SUM(A1:A2)"])
def test_sheet_formula_inputs_are_neutralized(value: str) -> None:
    assert sanitize_sheet_value(value).startswith("'")

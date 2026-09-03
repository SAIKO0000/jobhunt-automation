from __future__ import annotations

import ipaddress
import json
import re
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter


class UnsafeRemoteResponse(RuntimeError):
    """Raised when a remote request violates the adapter's security policy."""


def validate_https_url(url: str, allowed_hosts: set[str]) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise UnsafeRemoteResponse("Only HTTPS source URLs are allowed")
    if parsed.username or parsed.password:
        raise UnsafeRemoteResponse("Credentials in source URLs are prohibited")
    if parsed.port not in (None, 443):
        raise UnsafeRemoteResponse("Non-standard source ports are prohibited")
    host = (parsed.hostname or "").casefold().rstrip(".")
    if not host:
        raise UnsafeRemoteResponse("Source URL has no host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UnsafeRemoteResponse("IP-literal source hosts are prohibited")
    normalized_allowed = {item.casefold().rstrip(".") for item in allowed_hosts}
    if not any(host == allowed or host.endswith(f".{allowed}") for allowed in normalized_allowed):
        raise UnsafeRemoteResponse(f"Host {host!r} is not approved for this adapter")
    return url


class SafeHttpClient:
    def __init__(
        self,
        *,
        allowed_hosts: set[str],
        max_response_bytes: int,
        timeout_seconds: float = 20,
        max_redirects: int = 3,
        requests_per_minute: int = 60,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.allowed_hosts = allowed_hosts
        self.max_response_bytes = max_response_bytes
        self.max_redirects = max_redirects
        self._minimum_interval = 60 / requests_per_minute
        self._last_request_at = 0.0
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": "jobhunt-automation/0.1 (+manual-review-only)"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SafeHttpClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @retry(
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        wait=wait_exponential_jitter(initial=0.5, max=5),
        stop=stop_after_attempt(2),
        reraise=True,
    )
    def get_bytes(self, url: str) -> bytes:
        current = validate_https_url(url, self.allowed_hosts)
        for redirect_count in range(self.max_redirects + 1):
            self._throttle()
            with self._client.stream("GET", current) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirect_count >= self.max_redirects:
                        raise UnsafeRemoteResponse("Source exceeded the redirect limit")
                    location = response.headers.get("location")
                    if not location:
                        raise UnsafeRemoteResponse("Source returned a redirect without Location")
                    current = validate_https_url(urljoin(current, location), self.allowed_hosts)
                    continue
                response.raise_for_status()
                declared = response.headers.get("content-length")
                if declared and int(declared) > self.max_response_bytes:
                    raise UnsafeRemoteResponse("Source response exceeds the configured size limit")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > self.max_response_bytes:
                        raise UnsafeRemoteResponse(
                            "Source response exceeds the configured size limit"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
        raise UnsafeRemoteResponse("Source redirect handling failed")

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._minimum_interval:
            time.sleep(self._minimum_interval - elapsed)
        self._last_request_at = time.monotonic()

    def get_text(self, url: str) -> str:
        return self.get_bytes(url).decode("utf-8", errors="replace")

    def get_json(self, url: str) -> Any:
        try:
            return json.loads(self.get_text(url))
        except json.JSONDecodeError as exc:
            raise UnsafeRemoteResponse("Source returned invalid JSON") from exc


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: str, *, limit: int = 4_000) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    return text[:limit]


FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def sanitize_sheet_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    cleaned = value.replace("\x00", "").strip()
    if cleaned.startswith(FORMULA_PREFIXES):
        return f"'{cleaned}"
    return cleaned

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

import httpx

from jobhunt.canonical import canonicalize_url

HIMALAYAS_MCP_URL = "https://mcp.himalayas.app/mcp"
MAX_MCP_RESPONSE_BYTES = 128 * 1024
_JOB_PATH = re.compile(r"^/companies/([a-z0-9][a-z0-9-]*)/jobs/([a-z0-9][a-z0-9-]*)/?$")
_RESPONSE_JOB_URL = re.compile(
    r"https://himalayas\.app/companies/[a-z0-9-]+/jobs/[a-z0-9-]+(?:\?[^\s)\]]+)?"
)


class ExactJobStatus(StrEnum):
    ACTIVE = "active"
    NOT_FOUND = "not_found"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class ExactJobCheck:
    status: ExactJobStatus
    reason: str


def job_slugs(url: str) -> tuple[str, str] | None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "himalayas.app":
        return None
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        return None
    match = _JOB_PATH.fullmatch(parsed.path)
    return (match.group(1), match.group(2)) if match else None


class HimalayasExactVerifier:
    """Check one source-owned listing through Himalayas' public read-only MCP tool."""

    def __init__(
        self,
        *,
        endpoint: str = HIMALAYAS_MCP_URL,
        transport: httpx.BaseTransport | None = None,
        requests_per_minute: int = 4,
    ) -> None:
        if endpoint != HIMALAYAS_MCP_URL:
            raise ValueError("Unexpected Himalayas availability endpoint")
        self.endpoint = endpoint
        self._minimum_interval = 60 / requests_per_minute
        self._last_request_at = 0.0
        self._client = httpx.Client(
            timeout=httpx.Timeout(15), follow_redirects=False, transport=transport
        )

    def __enter__(self) -> HimalayasExactVerifier:
        return self

    def __exit__(self, *_: object) -> None:
        self._client.close()

    def check(self, source_url: str) -> ExactJobCheck:
        slugs = job_slugs(source_url)
        if slugs is None:
            return ExactJobCheck(ExactJobStatus.INCONCLUSIVE, "Listing URL has no exact job slugs")

        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._minimum_interval:
            time.sleep(self._minimum_interval - elapsed)
        self._last_request_at = time.monotonic()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "get_job_details",
                "arguments": {"company_slug": slugs[0], "job_slug": slugs[1]},
            },
        }
        try:
            with self._client.stream(
                "POST",
                self.endpoint,
                json=payload,
                headers={"Accept": "application/json, text/event-stream"},
            ) as response:
                if response.status_code != 200:
                    return ExactJobCheck(
                        ExactJobStatus.INCONCLUSIVE,
                        f"Exact-job service returned HTTP {response.status_code}",
                    )
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                if content_type not in {"text/event-stream", "application/json"}:
                    return ExactJobCheck(
                        ExactJobStatus.INCONCLUSIVE, "Exact-job response type was unexpected"
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_MCP_RESPONSE_BYTES:
                        return ExactJobCheck(
                            ExactJobStatus.INCONCLUSIVE, "Exact-job response was too large"
                        )
        except (httpx.HTTPError, UnicodeError, ValueError):
            return ExactJobCheck(ExactJobStatus.INCONCLUSIVE, "Exact-job request failed")

        try:
            raw = body.decode("utf-8")
            if content_type == "text/event-stream":
                data_lines = [line[6:] for line in raw.splitlines() if line.startswith("data: ")]
                if len(data_lines) != 1:
                    raise ValueError("Unexpected event count")
                raw = data_lines[0]
            result = json.loads(raw)
            if result.get("jsonrpc") != "2.0" or result.get("id") != 1 or "error" in result:
                raise ValueError("Unexpected JSON-RPC response")
            response_result = result["result"]
            if response_result.get("isError"):
                raise ValueError("Tool reported an error")
            content = response_result["content"]
            if not isinstance(content, list) or len(content) != 1:
                raise ValueError("Unexpected tool content")
            item = content[0]
            if item.get("type") != "text" or not isinstance(item.get("text"), str):
                raise ValueError("Unexpected tool text")
            message = item["text"]
        except (UnicodeError, ValueError, KeyError, TypeError, AttributeError):
            return ExactJobCheck(ExactJobStatus.INCONCLUSIVE, "Exact-job response was invalid")

        expected_missing = (
            f"Job not found. Check the company slug ('{slugs[0]}') and job slug ('{slugs[1]}')."
        )
        if message == expected_missing:
            return ExactJobCheck(ExactJobStatus.NOT_FOUND, "Himalayas exact job was not found")
        canonical = canonicalize_url(source_url)
        returned_urls = {
            canonicalize_url(url.rstrip(".,;")) for url in _RESPONSE_JOB_URL.findall(message)
        }
        if message.startswith("# ") and canonical in returned_urls:
            return ExactJobCheck(ExactJobStatus.ACTIVE, "Himalayas exact job is listed")
        return ExactJobCheck(ExactJobStatus.INCONCLUSIVE, "Exact-job response was unrecognized")

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobhunt.models import SourceRecord

TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "referrer",
    "source",
}


def canonicalize_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold().rstrip(".")
    port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=False)
        if not key.casefold().startswith("utm_") and key.casefold() not in TRACKING_PARAMETERS
    ]
    return urlunsplit(("https", f"{host}{port}", path, urlencode(sorted(query)), ""))


def normalized_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def canonical_key(record: SourceRecord) -> str:
    return canonicalize_url(str(record.apply_url or record.source_url))


def duplicate_fingerprint(record: SourceRecord) -> str:
    date_part = _date_bucket(record.published_at or record.retrieved_at)
    raw = "|".join(
        (
            _company_identity(record),
            normalized_token(record.title),
            normalized_token(record.location_text),
            date_part,
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _company_identity(record: SourceRecord) -> str:
    return normalized_token(record.company)


def _date_bucket(value: datetime) -> str:
    return value.date().isoformat()

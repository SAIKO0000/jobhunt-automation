from __future__ import annotations

from collections import defaultdict
from uuid import NAMESPACE_URL, uuid5

from jobhunt.canonical import canonical_key, duplicate_fingerprint
from jobhunt.models import Opportunity, SourceRecord


def build_opportunities(records: list[SourceRecord]) -> list[Opportunity]:
    exact: dict[tuple[str, str], Opportunity] = {}
    canonical: dict[str, Opportunity] = {}
    fingerprints: defaultdict[str, list[Opportunity]] = defaultdict(list)

    for record in records:
        source_identity = (record.source.value, record.source_record_id)
        key = canonical_key(record)
        if source_identity in exact:
            existing = exact[source_identity]
            if record.retrieved_at > existing.last_seen_at:
                existing.last_seen_at = record.retrieved_at
                existing.source_record = record
            continue
        if key in canonical:
            existing = canonical[key]
            existing.last_seen_at = max(existing.last_seen_at, record.retrieved_at)
            exact[source_identity] = existing
            continue
        fingerprint = duplicate_fingerprint(record)
        group_id = str(uuid5(NAMESPACE_URL, f"duplicate:{fingerprint}"))
        opportunity = Opportunity(
            record_id=uuid5(NAMESPACE_URL, f"record:{key}"),
            canonical_key=key,
            duplicate_group_id=group_id,
            source_record=record,
            first_seen_at=record.retrieved_at,
            last_seen_at=record.retrieved_at,
        )
        exact[source_identity] = opportunity
        canonical[key] = opportunity
        fingerprints[fingerprint].append(opportunity)

    # A one-record group is not a duplicate group. Multi-source candidates remain linked,
    # never destructively merged.
    for group in fingerprints.values():
        if len(group) == 1:
            group[0].duplicate_group_id = None
    return list(canonical.values())

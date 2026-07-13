"""Pure deterministic rules for M16 entity blocking and duplicate detection."""

from __future__ import annotations

from scidatafusion.contracts.normalization import NormalizedRecord
from scidatafusion.contracts.scientific import FieldName
from scidatafusion.domain.registry import canonical_hash


def entity_key_fields(
    record: NormalizedRecord, required_keys: tuple[FieldName, ...]
) -> tuple[tuple[FieldName, str, str], ...] | None:
    """Return ordered eligible key field ids/hashes or no deterministic identity."""

    fields = {item.field_name: item for item in record.fields}
    selected: list[tuple[FieldName, str, str]] = []
    for key in required_keys:
        field = fields.get(key)
        if field is None or not field.eligible_for_m16 or field.normalized_value_sha256 is None:
            return None
        selected.append((key, field.normalized_field_id, field.normalized_value_sha256))
    return tuple(selected)


def entity_fingerprint(keys: tuple[tuple[FieldName, str, str], ...]) -> str:
    """Build a privacy-reduced exact stable-identifier blocking key."""

    return canonical_hash(
        [{"field": name, "value_hash": value_hash} for name, _, value_hash in keys]
    )


def exact_record_fingerprint(record: NormalizedRecord) -> str:
    """Fingerprint all M16-eligible fields for conservative exact duplicate detection."""

    values = tuple(
        sorted(
            (
                item.field_name,
                item.normalized_value_sha256,
            )
            for item in record.fields
            if item.eligible_for_m16 and item.normalized_value_sha256 is not None
        )
    )
    return canonical_hash(values)

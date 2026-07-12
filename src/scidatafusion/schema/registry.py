"""Content-addressed field templates supplied by domain and task packs."""

from __future__ import annotations

import hmac
import json
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from scidatafusion.contracts.base import ContentHash, NonEmptyStr, SemanticVersion, StrictContract
from scidatafusion.contracts.routing import PackReference
from scidatafusion.contracts.scientific import (
    DataType,
    DerivationRule,
    FieldName,
    FieldRequirement,
    NumericRange,
    QualityGate,
)
from scidatafusion.domain.registry import RegistryErrorCode, RegistryLoadError, canonical_hash

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_MAX_REGISTRY_BYTES = 2 * 1024 * 1024


class FieldTemplate(StrictContract):
    name: FieldName
    description: NonEmptyStr
    requirement: FieldRequirement
    data_type: DataType
    semantic_type: NonEmptyStr
    aliases: tuple[NonEmptyStr, ...] = ()
    unit_dimension: NonEmptyStr | None = None
    allowed_units: tuple[NonEmptyStr, ...] = ()
    target_unit: NonEmptyStr | None = None
    nullable: bool
    valid_range: NumericRange | None = None
    source_preference: tuple[NonEmptyStr, ...] = Field(min_length=1)
    derivation: DerivationRule | None = None
    quality_threshold: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_derivation(self) -> Self:
        if (self.requirement is FieldRequirement.DERIVED) != (self.derivation is not None):
            msg = "schema templates require derivation exactly for derived fields"
            raise ValueError(msg)
        if len(self.aliases) != len({alias.casefold() for alias in self.aliases}):
            msg = "schema template aliases must be unique"
            raise ValueError(msg)
        if len(self.allowed_units) != len(set(self.allowed_units)):
            msg = "schema template allowed_units must be unique"
            raise ValueError(msg)
        if self.target_unit is not None and self.target_unit not in self.allowed_units:
            msg = "schema template target_unit must be allowed"
            raise ValueError(msg)
        if self.requirement is FieldRequirement.REQUIRED and self.nullable:
            msg = "required schema template fields cannot be nullable"
            raise ValueError(msg)
        if self.valid_range is not None and self.data_type not in {
            DataType.INTEGER,
            DataType.NUMBER,
        }:
            msg = "only numeric schema template fields may define a range"
            raise ValueError(msg)
        if len(self.source_preference) != len(set(self.source_preference)):
            msg = "schema template source preferences must be unique"
            raise ValueError(msg)
        return self


class SchemaPack(StrictContract):
    pack_type: Literal["domain", "task"]
    name: NonEmptyStr
    version: SemanticVersion
    source_pack_hash: ContentHash
    intent_aliases: tuple[NonEmptyStr, ...] = ()
    entity_keys: tuple[FieldName, ...] = ()
    source_types: tuple[NonEmptyStr, ...] = ()
    fields: tuple[FieldTemplate, ...]
    quality_gates: tuple[QualityGate, ...] = ()

    @model_validator(mode="after")
    def validate_unique_fields(self) -> Self:
        names = tuple(field.name for field in self.fields)
        if len(names) != len(set(names)):
            msg = "schema pack field names must be unique"
            raise ValueError(msg)
        if len(self.entity_keys) != len(set(self.entity_keys)):
            msg = "schema pack entity keys must be unique"
            raise ValueError(msg)
        fields_by_name = {field.name: field for field in self.fields}
        if any(key not in fields_by_name for key in self.entity_keys):
            msg = "schema pack entity keys must reference local fields"
            raise ValueError(msg)
        if any(
            fields_by_name[key].requirement is not FieldRequirement.REQUIRED
            for key in self.entity_keys
        ):
            msg = "schema pack entity keys must be required fields"
            raise ValueError(msg)
        if len(self.source_types) != len(set(self.source_types)):
            msg = "schema pack source types must be unique"
            raise ValueError(msg)
        if len(self.intent_aliases) != len({alias.casefold() for alias in self.intent_aliases}):
            msg = "schema pack intent aliases must be unique"
            raise ValueError(msg)
        gate_ids = tuple(gate.gate_id for gate in self.quality_gates)
        if len(gate_ids) != len(set(gate_ids)):
            msg = "schema pack quality gate ids must be unique"
            raise ValueError(msg)
        if any(field not in fields_by_name for gate in self.quality_gates for field in gate.fields):
            msg = "schema pack quality gates must reference local fields"
            raise ValueError(msg)
        return self


class _SchemaRegistryDocument(StrictContract):
    registry_version: SemanticVersion
    content_hash: ContentHash
    packs: tuple[SchemaPack, ...]

    @model_validator(mode="after")
    def validate_unique_packs(self) -> Self:
        identities = tuple((pack.pack_type, pack.name, pack.version) for pack in self.packs)
        if len(identities) != len(set(identities)):
            msg = "schema pack identities must be unique"
            raise ValueError(msg)
        return self


class SchemaPackRegistry(StrictContract):
    registry_version: SemanticVersion
    content_hash: ContentHash
    packs: tuple[SchemaPack, ...]

    @model_validator(mode="after")
    def validate_unique_packs(self) -> Self:
        identities = tuple((pack.pack_type, pack.name, pack.version) for pack in self.packs)
        if len(identities) != len(set(identities)):
            msg = "schema pack identities must be unique"
            raise ValueError(msg)
        return self

    @classmethod
    def from_file(cls, path: Path | str) -> SchemaPackRegistry:
        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise RegistryLoadError(RegistryErrorCode.NOT_FOUND, resolved, "file does not exist")
        if resolved.stat().st_size > _MAX_REGISTRY_BYTES:
            raise RegistryLoadError(RegistryErrorCode.TOO_LARGE, resolved, "registry exceeds limit")
        try:
            encoded = resolved.read_bytes()
            raw = json.loads(encoded)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RegistryLoadError(RegistryErrorCode.INVALID_JSON, resolved, str(exc)) from exc
        try:
            document = _SchemaRegistryDocument.model_validate_json(encoded)
        except ValidationError as exc:
            raise RegistryLoadError(RegistryErrorCode.INVALID_SCHEMA, resolved, str(exc)) from exc
        actual_hash = canonical_hash(
            {"packs": raw["packs"], "registry_version": raw["registry_version"]}
        )
        if not hmac.compare_digest(document.content_hash, actual_hash):
            raise RegistryLoadError(
                RegistryErrorCode.HASH_MISMATCH,
                resolved,
                f"declared {document.content_hash}, calculated {actual_hash}",
            )
        return cls(
            registry_version=document.registry_version,
            content_hash=document.content_hash,
            packs=document.packs,
        )

    @classmethod
    def load_default(cls) -> SchemaPackRegistry:
        return cls.from_file(_PROJECT_ROOT / "schema_packs" / "registry.json")

    def get(self, reference: PackReference) -> SchemaPack | None:
        return next(
            (
                pack
                for pack in self.packs
                if pack.pack_type == reference.pack_type
                and pack.name == reference.name
                and pack.version == reference.version
                and hmac.compare_digest(pack.source_pack_hash, reference.content_hash)
            ),
            None,
        )

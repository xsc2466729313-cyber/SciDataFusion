"""Scientific data-contract models shared by discovery and integration modules."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from scidatafusion.contracts.base import (
    ContentHash,
    NonEmptyStr,
    RunId,
    SemanticVersion,
    StrictContract,
    TaskId,
)
from scidatafusion.contracts.events import EventEnvelope

FieldName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
ContractId = Annotated[str, StringConstraints(pattern=r"^ctr_[0-9a-f]{32}$")]
SchemaId = Annotated[str, StringConstraints(pattern=r"^sch_[0-9a-f]{32}$")]
ConflictId = Annotated[str, StringConstraints(pattern=r"^cnf_[0-9a-f]{16}$")]
ConstraintId = Annotated[str, StringConstraints(pattern=r"^cst_[0-9a-f]{16}$")]
ResearchConceptId = Annotated[str, StringConstraints(pattern=r"^rcp_[0-9a-f]{16}$")]


class ContractStatus(StrEnum):
    DRAFT = "draft"
    NEEDS_REVIEW = "needs_review"
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"


class FieldRequirement(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    DERIVED = "derived"


class DataType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATETIME = "datetime"


class FieldOriginKind(StrEnum):
    USER = "user"
    PROBLEM = "problem"
    DOMAIN_PACK = "domain_pack"
    TASK_PACK = "task_pack"


class FieldOrigin(StrictContract):
    kind: FieldOriginKind
    reference: NonEmptyStr
    rationale: NonEmptyStr


class NumericRange(StrictContract):
    minimum: float | None = Field(default=None, allow_inf_nan=False)
    maximum: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            msg = "minimum must not exceed maximum"
            raise ValueError(msg)
        return self


class DerivationRule(StrictContract):
    method: NonEmptyStr
    expression: NonEmptyStr
    input_fields: tuple[FieldName, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_inputs(self) -> Self:
        if len(self.input_fields) != len(set(self.input_fields)):
            msg = "derivation input fields must be unique"
            raise ValueError(msg)
        return self


class FieldContract(StrictContract):
    """One evidence-ready canonical field and all of its deterministic constraints."""

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
    origins: tuple[FieldOrigin, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_field_rules(self) -> Self:
        if len(self.aliases) != len(set(alias.casefold() for alias in self.aliases)):
            msg = "field aliases must be unique"
            raise ValueError(msg)
        if len(self.allowed_units) != len(set(self.allowed_units)):
            msg = "allowed_units must be unique"
            raise ValueError(msg)
        if self.target_unit is not None and self.target_unit not in self.allowed_units:
            msg = "target_unit must be included in allowed_units"
            raise ValueError(msg)
        if self.requirement is FieldRequirement.DERIVED and self.derivation is None:
            msg = "derived fields require a derivation rule"
            raise ValueError(msg)
        if self.requirement is not FieldRequirement.DERIVED and self.derivation is not None:
            msg = "only derived fields may define a derivation rule"
            raise ValueError(msg)
        if self.requirement is FieldRequirement.REQUIRED and self.nullable:
            msg = "required fields cannot be nullable"
            raise ValueError(msg)
        if self.valid_range is not None and self.data_type not in {
            DataType.INTEGER,
            DataType.NUMBER,
        }:
            msg = "only numeric fields may define a valid range"
            raise ValueError(msg)
        if len(self.source_preference) != len(set(self.source_preference)):
            msg = "source preferences must be unique"
            raise ValueError(msg)
        origin_keys = tuple(
            (origin.kind, origin.reference, origin.rationale) for origin in self.origins
        )
        if len(origin_keys) != len(set(origin_keys)):
            msg = "field origins must be unique"
            raise ValueError(msg)
        return self


class QualityGateKind(StrEnum):
    REQUIRED_FIELDS = "required_fields"
    ANY_OF_FIELDS = "any_of_fields"
    FIELD_PROVENANCE = "field_provenance"


class QualityGate(StrictContract):
    gate_id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{2,63}$")]
    kind: QualityGateKind
    fields: tuple[FieldName, ...] = Field(min_length=1)
    threshold: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    blocking: bool = True
    description: NonEmptyStr

    @model_validator(mode="after")
    def validate_fields(self) -> Self:
        if len(self.fields) != len(set(self.fields)):
            msg = "quality gate fields must be unique"
            raise ValueError(msg)
        return self


class LicensePolicy(StrictContract):
    allow_restricted_metadata: bool = True
    require_redistribution_permission: bool = True
    require_attribution: bool = True


class SelectionConstraintKind(StrEnum):
    CONDITION = "condition"
    TEMPORAL_SCOPE = "temporal_scope"
    SPATIAL_SCOPE = "spatial_scope"


class SelectionConstraint(StrictContract):
    constraint_id: ConstraintId
    kind: SelectionConstraintKind
    expression: NonEmptyStr
    qualifier: NonEmptyStr | None = None
    negated: bool = False
    evidence_refs: tuple[NonEmptyStr, ...] = Field(min_length=1)


class ResearchConceptKind(StrEnum):
    ENTITY = "entity"
    VARIABLE = "variable"


class ResearchConcept(StrictContract):
    """Evidence-grounded search subject retained from the compiled research problem."""

    concept_id: ResearchConceptId
    kind: ResearchConceptKind
    term: NonEmptyStr
    qualifier: NonEmptyStr | None = None
    evidence_refs: tuple[NonEmptyStr, ...] = Field(min_length=1)


class ContractAssumption(StrictContract):
    assumption_id: Annotated[str, StringConstraints(pattern=r"^asm_[0-9a-f]{16}$")]
    statement: NonEmptyStr
    rationale: NonEmptyStr
    source_status: Literal["proposed", "confirmed", "rejected"]
    evidence_refs: tuple[NonEmptyStr, ...] = Field(min_length=1)


class ScientificDataContract(StrictContract):
    """Frozen contract consumed unchanged by downstream workflow modules."""

    contract_id: ContractId
    task_id: TaskId
    run_id: RunId
    problem_id: NonEmptyStr
    routing_ref: ContentHash
    schema_registry_hash: ContentHash
    version: SemanticVersion
    status: ContractStatus
    producer_version: SemanticVersion
    created_at: datetime
    confirmed_at: datetime | None = None
    confirmed_by: NonEmptyStr | None = None
    domain_profile: tuple[NonEmptyStr, ...] = Field(min_length=1)
    task_archetypes: tuple[NonEmptyStr, ...] = Field(min_length=1)
    fields: tuple[FieldContract, ...] = Field(min_length=1)
    entity_keys: tuple[FieldName, ...] = ()
    acceptable_source_types: tuple[NonEmptyStr, ...] = Field(min_length=1)
    quality_gates: tuple[QualityGate, ...] = Field(min_length=1)
    research_concepts: tuple[ResearchConcept, ...] = ()
    selection_constraints: tuple[SelectionConstraint, ...] = ()
    assumptions: tuple[ContractAssumption, ...] = ()
    provenance_level: Literal["field"] = "field"
    output_formats: tuple[Literal["csv", "parquet", "json", "notebook"], ...] = (
        "csv",
        "parquet",
        "json",
    )
    license_policy: LicensePolicy = Field(default_factory=LicensePolicy)
    schema_hash: ContentHash
    contract_hash: ContentHash

    @field_validator("created_at", "confirmed_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "contract timestamps must include a timezone"
            raise ValueError(msg)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        names = tuple(field.name for field in self.fields)
        fields_by_name = {field.name: field for field in self.fields}
        if len(names) != len(set(names)):
            msg = "contract field names must be unique"
            raise ValueError(msg)
        if not self.entity_keys and self.status in {
            ContractStatus.DRAFT,
            ContractStatus.CONFIRMED,
        }:
            msg = "draft and confirmed contracts require at least one entity key"
            raise ValueError(msg)
        if len(self.entity_keys) != len(set(self.entity_keys)):
            msg = "entity keys must be unique"
            raise ValueError(msg)
        for values, label in (
            (self.domain_profile, "domain profile"),
            (self.task_archetypes, "task archetypes"),
            (self.acceptable_source_types, "acceptable source types"),
        ):
            if len(values) != len(set(values)):
                msg = f"{label} must be unique"
                raise ValueError(msg)
        if any(key not in names for key in self.entity_keys):
            msg = "every entity key must reference a declared field"
            raise ValueError(msg)
        if any(
            fields_by_name[key].requirement is not FieldRequirement.REQUIRED
            for key in self.entity_keys
        ):
            msg = "every entity key must be a required field"
            raise ValueError(msg)
        gate_ids = tuple(gate.gate_id for gate in self.quality_gates)
        if len(gate_ids) != len(set(gate_ids)):
            msg = "quality gate ids must be unique"
            raise ValueError(msg)
        constraint_ids = tuple(item.constraint_id for item in self.selection_constraints)
        if len(constraint_ids) != len(set(constraint_ids)):
            msg = "selection constraint ids must be unique"
            raise ValueError(msg)
        concept_ids = tuple(item.concept_id for item in self.research_concepts)
        if len(concept_ids) != len(set(concept_ids)):
            msg = "research concept ids must be unique"
            raise ValueError(msg)
        concept_keys = tuple(
            (item.kind, item.term.casefold(), (item.qualifier or "").casefold())
            for item in self.research_concepts
        )
        if len(concept_keys) != len(set(concept_keys)):
            msg = "research concepts must be semantically unique"
            raise ValueError(msg)
        assumption_ids = tuple(item.assumption_id for item in self.assumptions)
        if len(assumption_ids) != len(set(assumption_ids)):
            msg = "contract assumption ids must be unique"
            raise ValueError(msg)
        gate_fields = {name for gate in self.quality_gates for name in gate.fields}
        if not gate_fields.issubset(set(names)):
            msg = "quality gates may only reference declared fields"
            raise ValueError(msg)
        if any(
            gate.kind is QualityGateKind.REQUIRED_FIELDS
            and any(
                fields_by_name[name].requirement is not FieldRequirement.REQUIRED
                for name in gate.fields
            )
            for gate in self.quality_gates
        ):
            msg = "required-fields gates may only reference required fields"
            raise ValueError(msg)
        derived_dependencies = {
            field.name: field.derivation.input_fields
            for field in self.fields
            if field.derivation is not None
        }
        if any(
            dependency not in fields_by_name
            for dependencies in derived_dependencies.values()
            for dependency in dependencies
        ):
            msg = "derived fields may only reference declared input fields"
            raise ValueError(msg)
        self._reject_derivation_cycles(derived_dependencies)
        has_confirmation = self.confirmed_at is not None and self.confirmed_by is not None
        has_partial_confirmation = (self.confirmed_at is None) != (self.confirmed_by is None)
        if has_partial_confirmation or (
            self.status is ContractStatus.CONFIRMED and not has_confirmation
        ):
            msg = "confirmed metadata must be complete for confirmed contracts"
            raise ValueError(msg)
        if has_confirmation and self.status not in {
            ContractStatus.CONFIRMED,
            ContractStatus.SUPERSEDED,
        }:
            msg = "only confirmed or superseded contracts may retain confirmation metadata"
            raise ValueError(msg)
        if len(self.output_formats) != len(set(self.output_formats)):
            msg = "output formats must be unique"
            raise ValueError(msg)
        return self

    @staticmethod
    def _reject_derivation_cycles(graph: dict[str, tuple[FieldName, ...]]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                msg = "derived fields must not contain dependency cycles"
                raise ValueError(msg)
            if name in visited:
                return
            visiting.add(name)
            for dependency in graph.get(name, ()):
                if dependency in graph:
                    visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for field_name in graph:
            visit(field_name)


class CanonicalField(StrictContract):
    name: FieldName
    json_type: Literal["string", "integer", "number", "boolean"]
    format: Literal["date-time"] | None = None
    nullable: bool
    required: bool
    description: NonEmptyStr
    minimum: float | None = Field(default=None, allow_inf_nan=False)
    maximum: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            msg = "canonical field minimum must not exceed maximum"
            raise ValueError(msg)
        if self.json_type not in {"integer", "number"} and (
            self.minimum is not None or self.maximum is not None
        ):
            msg = "only numeric canonical fields may define a range"
            raise ValueError(msg)
        return self


class CanonicalSchema(StrictContract):
    schema_id: SchemaId
    contract_id: ContractId
    fields: tuple[CanonicalField, ...] = Field(min_length=1)
    required_fields: tuple[FieldName, ...]
    schema_hash: ContentHash

    @model_validator(mode="after")
    def validate_required_fields(self) -> Self:
        names = tuple(field.name for field in self.fields)
        if len(names) != len(set(names)):
            msg = "canonical schema field names must be unique"
            raise ValueError(msg)
        if not set(self.required_fields).issubset(names):
            msg = "required schema fields must be declared"
            raise ValueError(msg)
        flagged_required = {field.name for field in self.fields if field.required}
        if flagged_required != set(self.required_fields):
            msg = "required_fields must exactly match required field flags"
            raise ValueError(msg)
        if len(self.required_fields) != len(set(self.required_fields)):
            msg = "required schema fields must be unique"
            raise ValueError(msg)
        return self


class SchemaConflict(StrictContract):
    conflict_id: ConflictId
    field_name: FieldName
    existing_reference: NonEmptyStr
    incoming_reference: NonEmptyStr
    reason: NonEmptyStr
    existing_definition: FieldContract
    incoming_definition: FieldContract
    blocking: bool = True


class ContractCompiledPayload(StrictContract):
    contract_id: ContractId
    contract_hash: ContentHash
    schema_hash: ContentHash
    status: ContractStatus
    blocking_conflicts: int = Field(ge=0)
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash


class ContractCompilationMetrics(StrictContract):
    field_count: int = Field(ge=0)
    required_field_count: int = Field(ge=0)
    conflict_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)


class ContractCompilationResult(StrictContract):
    module_id: Literal["M03"] = "M03"
    task_id: TaskId
    run_id: RunId
    contract_version: SemanticVersion
    status: ContractStatus
    created_at: datetime
    producer_version: SemanticVersion
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    contract: ScientificDataContract
    canonical_schema: CanonicalSchema
    conflicts: tuple[SchemaConflict, ...] = ()
    warnings: tuple[NonEmptyStr, ...] = ()
    metrics: ContractCompilationMetrics
    event: EventEnvelope[ContractCompiledPayload]

    @field_validator("created_at")
    @classmethod
    def require_result_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "result timestamp must include a timezone"
            raise ValueError(msg)
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_linked_artifacts(self) -> Self:
        if self.status not in {ContractStatus.DRAFT, ContractStatus.NEEDS_REVIEW}:
            msg = "a compilation result must be draft or needs_review"
            raise ValueError(msg)
        requires_review = bool(self.conflicts or self.warnings or not self.contract.entity_keys)
        if requires_review != (self.status is ContractStatus.NEEDS_REVIEW):
            msg = "warnings, conflicts, or a missing entity key require needs_review status"
            raise ValueError(msg)
        if (
            self.contract.task_id != self.task_id
            or self.contract.run_id != self.run_id
            or self.contract.version != self.contract_version
            or self.contract.status is not self.status
            or self.contract.created_at != self.created_at
            or self.contract.producer_version != self.producer_version
        ):
            msg = "compiled contract must share result metadata"
            raise ValueError(msg)
        if (
            self.canonical_schema.contract_id != self.contract.contract_id
            or self.canonical_schema.schema_hash != self.contract.schema_hash
        ):
            msg = "canonical schema must refer to the compiled contract"
            raise ValueError(msg)
        payload = self.event.payload
        if (
            self.event.event_type.value != "contract.compiled"
            or self.event.task_id != self.task_id
            or self.event.run_id != self.run_id
            or self.event.occurred_at != self.created_at
            or payload.contract_id != self.contract.contract_id
            or payload.contract_hash != self.contract.contract_hash
            or payload.schema_hash != self.contract.schema_hash
            or payload.status is not self.status
            or payload.blocking_conflicts != sum(conflict.blocking for conflict in self.conflicts)
            or payload.input_hash != self.input_hash
            or payload.output_hash != self.output_hash
            or payload.idempotency_key != self.idempotency_key
        ):
            msg = "contract.compiled event must refer to this result"
            raise ValueError(msg)
        if self.metrics != ContractCompilationMetrics(
            field_count=len(self.contract.fields),
            required_field_count=sum(
                field.requirement is FieldRequirement.REQUIRED for field in self.contract.fields
            ),
            conflict_count=len(self.conflicts),
            warning_count=len(self.warnings),
        ):
            msg = "compilation metrics must be derived from result artifacts"
            raise ValueError(msg)
        return self


class ContractConfirmationPayload(StrictContract):
    contract_id: ContractId
    contract_hash: ContentHash
    confirmed_by: NonEmptyStr


class ContractConfirmationResult(StrictContract):
    contract: ScientificDataContract
    event: EventEnvelope[ContractConfirmationPayload]

    @model_validator(mode="after")
    def validate_confirmation(self) -> Self:
        if (
            self.contract.status is not ContractStatus.CONFIRMED
            or self.contract.confirmed_by is None
            or self.event.event_type.value != "contract.confirmed"
            or self.event.task_id != self.contract.task_id
            or self.event.run_id != self.contract.run_id
            or self.event.occurred_at != self.contract.confirmed_at
            or self.event.payload.contract_id != self.contract.contract_id
            or self.event.payload.contract_hash != self.contract.contract_hash
            or self.event.payload.confirmed_by != self.contract.confirmed_by
        ):
            msg = "contract.confirmed event must refer to the confirmed contract"
            raise ValueError(msg)
        return self


class FieldChange(StrictContract):
    field_name: FieldName
    change: Literal["added", "removed", "changed"]
    detail: NonEmptyStr


class ContractMetadataChange(StrictContract):
    area: Literal[
        "entity_keys",
        "acceptable_source_types",
        "quality_gates",
        "research_concepts",
        "selection_constraints",
        "assumptions",
        "output_formats",
        "license_policy",
    ]
    detail: NonEmptyStr
    breaking: bool


class ContractDiff(StrictContract):
    old_contract_id: ContractId
    new_contract_id: ContractId
    old_version: SemanticVersion
    new_version: SemanticVersion
    field_changes: tuple[FieldChange, ...]
    metadata_changes: tuple[ContractMetadataChange, ...] = ()

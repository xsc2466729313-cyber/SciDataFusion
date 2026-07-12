"""Deterministic M03 scientific data-contract compiler."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from scidatafusion.contracts.base import StrictContract, utc_now
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.problem import (
    OutputFormat,
    ScientificProblemSpec,
    ScopeIntent,
    VariableIntent,
)
from scidatafusion.contracts.routing import (
    PackReference,
    RoutingDecision,
    RoutingMode,
    RoutingStatus,
)
from scidatafusion.contracts.scientific import (
    CanonicalField,
    CanonicalSchema,
    ContractAssumption,
    ContractCompilationMetrics,
    ContractCompilationResult,
    ContractCompiledPayload,
    ContractConfirmationPayload,
    ContractConfirmationResult,
    ContractDiff,
    ContractMetadataChange,
    ContractStatus,
    DataType,
    FieldChange,
    FieldContract,
    FieldOrigin,
    FieldOriginKind,
    FieldRequirement,
    LicensePolicy,
    QualityGate,
    QualityGateKind,
    SchemaConflict,
    ScientificDataContract,
    SelectionConstraint,
    SelectionConstraintKind,
)
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.schema.registry import FieldTemplate, SchemaPack, SchemaPackRegistry

MetadataArea = Literal[
    "entity_keys",
    "acceptable_source_types",
    "quality_gates",
    "selection_constraints",
    "assumptions",
    "output_formats",
    "license_policy",
]
OutputContractFormat = Literal["csv", "parquet", "json", "notebook"]
_OUTPUT_FORMAT_MAP: dict[OutputFormat, OutputContractFormat] = {
    OutputFormat.CSV: "csv",
    OutputFormat.PARQUET: "parquet",
    OutputFormat.JSON: "json",
    OutputFormat.NOTEBOOK: "notebook",
}


def _stable_id(prefix: str, value: object, *, length: int = 32) -> str:
    return f"{prefix}_{canonical_hash(value)[:length]}"


def _unique(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _unique_casefold(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    unique: dict[str, str] = {}
    for value in values:
        unique.setdefault(value.casefold(), value)
    return tuple(unique.values())


def _json_compatible(value: object) -> object:
    if isinstance(value, StrictContract):
        return value.model_dump(mode="json")
    if isinstance(value, tuple):
        return [_json_compatible(item) for item in value]
    return value


def _requirement_rank(requirement: FieldRequirement) -> int:
    return {
        FieldRequirement.OPTIONAL: 0,
        FieldRequirement.DERIVED: 1,
        FieldRequirement.REQUIRED: 2,
    }[requirement]


def _contract_seed(
    *,
    task_id: str,
    run_id: str,
    problem_id: str,
    routing_ref: str,
    registry_hash: str,
    version: str,
    producer_version: str,
    fields: tuple[FieldContract, ...],
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "run_id": run_id,
        "problem_id": problem_id,
        "routing_ref": routing_ref,
        "registry_hash": registry_hash,
        "version": version,
        "producer_version": producer_version,
        "fields": [field.model_dump(mode="json") for field in fields],
    }


def _contract_content_hash(
    seed: dict[str, object],
    *,
    domain_profile: tuple[str, ...],
    task_archetypes: tuple[str, ...],
    entity_keys: tuple[str, ...],
    source_types: tuple[str, ...],
    quality_gates: tuple[QualityGate, ...],
    selection_constraints: tuple[SelectionConstraint, ...],
    assumptions: tuple[ContractAssumption, ...],
    provenance_level: str,
    output_formats: tuple[str, ...],
    license_policy: LicensePolicy,
    schema_hash: str,
) -> str:
    return canonical_hash(
        {
            **seed,
            "domain_profile": domain_profile,
            "task_archetypes": task_archetypes,
            "entity_keys": entity_keys,
            "source_types": source_types,
            "quality_gates": [gate.model_dump(mode="json") for gate in quality_gates],
            "selection_constraints": [
                item.model_dump(mode="json") for item in selection_constraints
            ],
            "assumptions": [item.model_dump(mode="json") for item in assumptions],
            "provenance_level": provenance_level,
            "output_formats": output_formats,
            "license_policy": license_policy.model_dump(mode="json"),
            "schema_hash": schema_hash,
        }
    )


class ContractCompiler:
    """Compose user intent and versioned pack fragments without silent conflict resolution."""

    def __init__(
        self,
        registry: SchemaPackRegistry | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
        producer_version: str = "1.0.0",
    ) -> None:
        self._registry = registry or SchemaPackRegistry.load_default()
        self._clock = clock
        self._producer_version = producer_version
        self._cache: dict[str, ContractCompilationResult] = {}
        self._confirmations: dict[str, ContractConfirmationResult] = {}
        self._confirmed_hash_by_id: dict[str, str] = {}

    def compile(
        self,
        problem: ScientificProblemSpec,
        routing: RoutingDecision,
        *,
        version: str = "1.0.0",
        force_recompute: bool = False,
    ) -> ContractCompilationResult:
        """Compile one immutable draft contract and its canonical machine schema."""

        if problem.task_id != routing.task_id or problem.run_id != routing.run_id:
            raise AppError(
                ErrorCode.VALIDATION_FAILED,
                "problem and routing decision must belong to the same task/run",
            )

        input_hash = canonical_hash(
            {
                "problem": problem.model_dump(mode="json"),
                "routing": routing.model_dump(mode="json"),
                "schema_registry_hash": self._registry.content_hash,
            }
        )
        idempotency_key = canonical_hash(
            {
                "task_id": problem.task_id,
                "module_id": "M03",
                "contract_version": version,
                "input_hash": input_hash,
                "producer_version": self._producer_version,
            }
        )
        cached = self._cache.get(idempotency_key)
        if cached is not None and not force_recompute:
            return cached

        warnings: list[str] = []
        conflicts: list[SchemaConflict] = []
        references = self._selected_references(routing)
        reference_identities = tuple(
            (reference.pack_type, reference.name, reference.version) for reference in references
        )
        if len(reference_identities) != len(set(reference_identities)):
            raise AppError(
                ErrorCode.VALIDATION_FAILED, "routing contains duplicate pack references"
            )
        used_proposed = not (
            routing.pack_selection.domain_packs or routing.pack_selection.task_packs
        ) and bool(references)
        if used_proposed:
            warnings.append("proposed pack schemas were used; route requires review")
        if (
            routing.status is not RoutingStatus.SUCCEEDED
            or routing.pack_selection.mode is not RoutingMode.FORMAL
        ):
            warnings.append("routing decision is not a succeeded formal route")

        fields: dict[str, FieldContract] = {}
        entity_keys: list[str] = []
        source_types: list[str] = []
        quality_gates: dict[str, QualityGate] = {}

        for reference in references:
            pack = self._registry.get(reference)
            if pack is None:
                warnings.append(
                    f"schema fragment missing for {reference.pack_type} pack {reference.name}"
                )
                continue
            entity_keys.extend(pack.entity_keys)
            source_types.extend(pack.source_types)
            for gate in pack.quality_gates:
                existing_gate = quality_gates.get(gate.gate_id)
                if existing_gate is not None and existing_gate != gate:
                    raise AppError(
                        ErrorCode.VALIDATION_FAILED,
                        f"conflicting quality gate id: {gate.gate_id}",
                    )
                quality_gates[gate.gate_id] = gate
            self._merge_pack(fields, conflicts, pack, reference)

        unresolved_variables = self._ground_problem_variables(fields, problem, source_types)
        warnings.extend(unresolved_variables)

        if not fields:
            raise AppError(
                ErrorCode.VALIDATION_FAILED,
                "no schema fields could be compiled from the selected packs or user request",
            )
        if not entity_keys:
            warnings.append("no entity key was resolved")

        ordered_fields = tuple(fields[name] for name in sorted(fields))
        required_names = tuple(
            field.name for field in ordered_fields if field.requirement is FieldRequirement.REQUIRED
        )
        if required_names:
            generated_gates = (
                QualityGate(
                    gate_id="required_fields_complete",
                    kind=QualityGateKind.REQUIRED_FIELDS,
                    fields=required_names,
                    threshold=1.0,
                    description="All required fields must be populated.",
                ),
                QualityGate(
                    gate_id="required_field_provenance",
                    kind=QualityGateKind.FIELD_PROVENANCE,
                    fields=required_names,
                    threshold=1.0,
                    description="Every required field must retain field-level evidence.",
                ),
            )
            for gate in generated_gates:
                existing_gate = quality_gates.get(gate.gate_id)
                if existing_gate is not None and existing_gate != gate:
                    raise AppError(
                        ErrorCode.VALIDATION_FAILED,
                        f"schema pack reserved a generated quality gate id: {gate.gate_id}",
                    )
                quality_gates[gate.gate_id] = gate

        contract_seed = _contract_seed(
            task_id=problem.task_id,
            run_id=problem.run_id,
            problem_id=problem.problem_id,
            routing_ref=routing.decision_hash,
            registry_hash=self._registry.content_hash,
            version=version,
            producer_version=self._producer_version,
            fields=ordered_fields,
        )
        provisional_contract_id = _stable_id("ctr", contract_seed)
        schema = self._canonical_schema(provisional_contract_id, ordered_fields)
        status = (
            ContractStatus.NEEDS_REVIEW
            if conflicts or warnings or not entity_keys
            else ContractStatus.DRAFT
        )
        domain_profile = (
            routing.domain_profile.primary_domain,
            *routing.domain_profile.secondary_domains,
        )
        task_archetypes = routing.task_archetypes.archetypes
        resolved_entity_keys = _unique(entity_keys)
        resolved_source_types = _unique(source_types) or ("paper_table",)
        resolved_quality_gates = tuple(quality_gates.values())
        selection_constraints = self._selection_constraints(problem)
        contract_assumptions = tuple(
            ContractAssumption(
                assumption_id=item.assumption_id,
                statement=item.statement,
                rationale=item.rationale,
                source_status=item.status.value,
                evidence_refs=tuple(
                    f"{problem.problem_id}:{span.start}-{span.end}" for span in item.evidence
                ),
            )
            for item in problem.assumptions
        )
        license_policy = LicensePolicy()
        requested_output_formats = tuple(
            dict.fromkeys(_OUTPUT_FORMAT_MAP[item.format] for item in problem.output_preferences)
        )
        output_formats: tuple[OutputContractFormat, ...] = requested_output_formats or (
            "csv",
            "parquet",
            "json",
        )
        contract_hash = _contract_content_hash(
            contract_seed,
            domain_profile=domain_profile,
            task_archetypes=task_archetypes,
            entity_keys=resolved_entity_keys,
            source_types=resolved_source_types,
            quality_gates=resolved_quality_gates,
            selection_constraints=selection_constraints,
            assumptions=contract_assumptions,
            provenance_level="field",
            output_formats=output_formats,
            license_policy=license_policy,
            schema_hash=schema.schema_hash,
        )
        contract_id = f"ctr_{contract_hash[:32]}"
        schema = self._canonical_schema(contract_id, ordered_fields)
        created_at = self._clock()
        contract = ScientificDataContract(
            contract_id=contract_id,
            task_id=problem.task_id,
            run_id=problem.run_id,
            problem_id=problem.problem_id,
            routing_ref=routing.decision_hash,
            schema_registry_hash=self._registry.content_hash,
            version=version,
            status=status,
            producer_version=self._producer_version,
            created_at=created_at,
            domain_profile=domain_profile,
            task_archetypes=task_archetypes,
            fields=ordered_fields,
            entity_keys=resolved_entity_keys,
            acceptable_source_types=resolved_source_types,
            quality_gates=resolved_quality_gates,
            selection_constraints=selection_constraints,
            assumptions=contract_assumptions,
            output_formats=output_formats,
            license_policy=license_policy,
            schema_hash=schema.schema_hash,
            contract_hash=contract_hash,
        )
        output_hash = canonical_hash(
            {
                "contract": contract.model_dump(mode="json", exclude={"created_at"}),
                "canonical_schema": schema.model_dump(mode="json"),
                "conflicts": [item.model_dump(mode="json") for item in conflicts],
                "warnings": warnings,
            }
        )
        payload = ContractCompiledPayload(
            contract_id=contract.contract_id,
            contract_hash=contract.contract_hash,
            schema_hash=contract.schema_hash,
            status=contract.status,
            blocking_conflicts=sum(item.blocking for item in conflicts),
            input_hash=input_hash,
            output_hash=output_hash,
            idempotency_key=idempotency_key,
        )
        event = EventEnvelope[ContractCompiledPayload](
            event_type=EventType.CONTRACT_COMPILED,
            task_id=problem.task_id,
            run_id=problem.run_id,
            occurred_at=created_at,
            producer=ProducerRef(component="contract_compiler", version=self._producer_version),
            payload=payload,
        )
        result = ContractCompilationResult(
            task_id=problem.task_id,
            run_id=problem.run_id,
            contract_version=version,
            status=contract.status,
            created_at=created_at,
            producer_version=self._producer_version,
            input_hash=input_hash,
            output_hash=output_hash,
            idempotency_key=idempotency_key,
            contract=contract,
            canonical_schema=schema,
            conflicts=tuple(conflicts),
            warnings=tuple(warnings),
            metrics=ContractCompilationMetrics(
                field_count=len(contract.fields),
                required_field_count=len(required_names),
                conflict_count=len(conflicts),
                warning_count=len(warnings),
            ),
            event=event,
        )
        self._cache[idempotency_key] = result
        return result

    def confirm(
        self,
        contract: ScientificDataContract,
        *,
        expected_contract_hash: str,
        confirmed_by: str,
    ) -> ContractConfirmationResult:
        """Confirm an unchanged draft using optimistic hash matching."""

        integrity_seed = _contract_seed(
            task_id=contract.task_id,
            run_id=contract.run_id,
            problem_id=contract.problem_id,
            routing_ref=contract.routing_ref,
            registry_hash=contract.schema_registry_hash,
            version=contract.version,
            producer_version=contract.producer_version,
            fields=contract.fields,
        )
        calculated_schema = self._canonical_schema(contract.contract_id, contract.fields)
        calculated_contract_hash = _contract_content_hash(
            integrity_seed,
            domain_profile=contract.domain_profile,
            task_archetypes=contract.task_archetypes,
            entity_keys=contract.entity_keys,
            source_types=contract.acceptable_source_types,
            quality_gates=contract.quality_gates,
            selection_constraints=contract.selection_constraints,
            assumptions=contract.assumptions,
            provenance_level=contract.provenance_level,
            output_formats=contract.output_formats,
            license_policy=contract.license_policy,
            schema_hash=calculated_schema.schema_hash,
        )
        calculated_contract_id = f"ctr_{calculated_contract_hash[:32]}"
        if not (
            hmac.compare_digest(contract.contract_id, calculated_contract_id)
            and hmac.compare_digest(contract.schema_hash, calculated_schema.schema_hash)
            and hmac.compare_digest(contract.contract_hash, calculated_contract_hash)
        ):
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "contract content does not match its immutable identifiers",
            )
        if not hmac.compare_digest(contract.contract_hash, expected_contract_hash):
            raise AppError(ErrorCode.VALIDATION_FAILED, "contract hash changed before confirmation")
        if contract.status is ContractStatus.NEEDS_REVIEW:
            raise AppError(
                ErrorCode.QUALITY_GATE_FAILED,
                "contract has unresolved warnings or conflicts and cannot be confirmed",
            )
        if contract.status is not ContractStatus.DRAFT:
            raise AppError(ErrorCode.INVALID_REQUEST, "only a draft contract can be confirmed")
        confirmed_hash = self._confirmed_hash_by_id.get(contract.contract_id)
        if confirmed_hash is not None and not hmac.compare_digest(
            confirmed_hash, contract.contract_hash
        ):
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "contract id was previously associated with different content",
            )
        prior_confirmation = self._confirmations.get(contract.contract_hash)
        if prior_confirmation is not None:
            if prior_confirmation.contract.confirmed_by == confirmed_by:
                return prior_confirmation
            raise AppError(
                ErrorCode.INVALID_REQUEST,
                "contract was already confirmed by a different reviewer",
            )
        confirmed_at = self._clock()
        confirmed = ScientificDataContract.model_validate(
            {
                **contract.model_dump(),
                "status": ContractStatus.CONFIRMED,
                "confirmed_at": confirmed_at,
                "confirmed_by": confirmed_by,
            }
        )
        payload = ContractConfirmationPayload(
            contract_id=confirmed.contract_id,
            contract_hash=confirmed.contract_hash,
            confirmed_by=confirmed_by,
        )
        event = EventEnvelope[ContractConfirmationPayload](
            event_type=EventType.CONTRACT_CONFIRMED,
            task_id=confirmed.task_id,
            run_id=confirmed.run_id,
            occurred_at=confirmed_at,
            producer=ProducerRef(component="contract_compiler", version=self._producer_version),
            payload=payload,
        )
        result = ContractConfirmationResult(contract=confirmed, event=event)
        self._confirmations[contract.contract_hash] = result
        self._confirmed_hash_by_id[contract.contract_id] = contract.contract_hash
        return result

    @staticmethod
    def render_markdown(contract: ScientificDataContract) -> str:
        """Render a compact human-review view from the machine contract."""

        lines = [
            f"# Scientific Data Contract {contract.version}",
            "",
            f"Status: {contract.status.value}",
            f"Contract hash: `{contract.contract_hash}`",
            "",
            "| Field | Requirement | Type | Unit | Origins |",
            "|---|---|---|---|---|",
        ]
        for field in contract.fields:
            origins = ", ".join(origin.kind.value for origin in field.origins)
            lines.append(
                f"| {field.name} | {field.requirement.value} | {field.data_type.value} | "
                f"{field.target_unit or '-'} | {origins} |"
            )
        return "\n".join(lines) + "\n"

    @staticmethod
    def json_schema(schema: CanonicalSchema) -> dict[str, object]:
        """Produce a deterministic JSON Schema for downstream validators."""

        properties: dict[str, object] = {}
        for field in schema.fields:
            definition: dict[str, object] = {
                "type": [field.json_type, "null"] if field.nullable else field.json_type,
                "description": field.description,
            }
            if field.format is not None:
                definition["format"] = field.format
            if field.minimum is not None:
                definition["minimum"] = field.minimum
            if field.maximum is not None:
                definition["maximum"] = field.maximum
            properties[field.name] = definition
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": list(schema.required_fields),
        }

    @staticmethod
    def _selected_references(routing: RoutingDecision) -> tuple[PackReference, ...]:
        enabled = (*routing.pack_selection.domain_packs, *routing.pack_selection.task_packs)
        if enabled:
            return enabled
        return (
            *routing.pack_selection.proposed_domain_packs,
            *routing.pack_selection.proposed_task_packs,
        )

    @staticmethod
    def _selection_constraints(
        problem: ScientificProblemSpec,
    ) -> tuple[SelectionConstraint, ...]:
        constraints: list[SelectionConstraint] = []
        for condition in problem.conditions:
            evidence_refs = tuple(
                f"{problem.problem_id}:{span.start}-{span.end}" for span in condition.evidence
            )
            constraints.append(
                SelectionConstraint(
                    constraint_id=_stable_id(
                        "cst",
                        ("condition", condition.expression, condition.negated, evidence_refs),
                        length=16,
                    ),
                    kind=SelectionConstraintKind.CONDITION,
                    expression=condition.expression,
                    qualifier=condition.kind.value,
                    negated=condition.negated,
                    evidence_refs=evidence_refs,
                )
            )
        scopes: tuple[tuple[SelectionConstraintKind, ScopeIntent | None], ...] = (
            (SelectionConstraintKind.TEMPORAL_SCOPE, problem.temporal_scope),
            (SelectionConstraintKind.SPATIAL_SCOPE, problem.spatial_scope),
        )
        for kind, scope in scopes:
            if scope is None:
                continue
            evidence_refs = tuple(
                f"{problem.problem_id}:{span.start}-{span.end}" for span in scope.evidence
            )
            constraints.append(
                SelectionConstraint(
                    constraint_id=_stable_id(
                        "cst", (kind.value, scope.expression, evidence_refs), length=16
                    ),
                    kind=kind,
                    expression=scope.expression,
                    qualifier=scope.dimension.value,
                    evidence_refs=evidence_refs,
                )
            )
        return tuple(constraints)

    def _merge_pack(
        self,
        fields: dict[str, FieldContract],
        conflicts: list[SchemaConflict],
        pack: SchemaPack,
        reference: PackReference,
    ) -> None:
        for template in pack.fields:
            incoming = self._from_template(template, reference)
            existing = fields.get(incoming.name)
            if existing is None:
                fields[incoming.name] = incoming
                continue
            reason = self._incompatibility(existing, incoming)
            if reason is not None:
                conflicts.append(
                    SchemaConflict(
                        conflict_id=_stable_id(
                            "cnf",
                            {
                                "field": incoming.name,
                                "existing": existing.origins[0].reference,
                                "incoming": incoming.origins[0].reference,
                                "reason": reason,
                            },
                            length=16,
                        ),
                        field_name=incoming.name,
                        existing_reference=existing.origins[0].reference,
                        incoming_reference=incoming.origins[0].reference,
                        reason=reason,
                        existing_definition=existing,
                        incoming_definition=incoming,
                    )
                )
                continue
            fields[incoming.name] = self._merge_compatible(existing, incoming)

    @staticmethod
    def _from_template(template: FieldTemplate, reference: PackReference) -> FieldContract:
        origin_kind = (
            FieldOriginKind.DOMAIN_PACK
            if reference.pack_type == "domain"
            else FieldOriginKind.TASK_PACK
        )
        return FieldContract(
            **template.model_dump(),
            origins=(
                FieldOrigin(
                    kind=origin_kind,
                    reference=f"{reference.name}@{reference.version}:{reference.content_hash}",
                    rationale=f"Declared by the selected {reference.pack_type} pack.",
                ),
            ),
        )

    @staticmethod
    def _incompatibility(existing: FieldContract, incoming: FieldContract) -> str | None:
        if existing.data_type is not incoming.data_type:
            return f"data type conflict: {existing.data_type.value} vs {incoming.data_type.value}"
        if existing.unit_dimension != incoming.unit_dimension:
            return (
                f"unit dimension conflict: {existing.unit_dimension or 'none'} vs "
                f"{incoming.unit_dimension or 'none'}"
            )
        if existing.semantic_type != incoming.semantic_type:
            return f"semantic type conflict: {existing.semantic_type} vs {incoming.semantic_type}"
        if (existing.derivation is None) != (incoming.derivation is None):
            return "derivation conflict: one definition is derived and the other is not"
        if existing.derivation != incoming.derivation:
            return "derivation conflict: formulas or input fields differ"
        if existing.valid_range != incoming.valid_range:
            return "valid range conflict"
        if (
            existing.target_unit is not None
            and incoming.target_unit is not None
            and existing.target_unit != incoming.target_unit
        ):
            return f"target unit conflict: {existing.target_unit} vs {incoming.target_unit}"
        return None

    @staticmethod
    def _merge_compatible(existing: FieldContract, incoming: FieldContract) -> FieldContract:
        requirement = max((existing.requirement, incoming.requirement), key=_requirement_rank)
        return FieldContract.model_validate(
            {
                **existing.model_dump(),
                "requirement": requirement,
                "nullable": existing.nullable and incoming.nullable,
                "aliases": _unique_casefold([*existing.aliases, *incoming.aliases]),
                "allowed_units": _unique([*existing.allowed_units, *incoming.allowed_units]),
                "target_unit": existing.target_unit or incoming.target_unit,
                "source_preference": _unique(
                    [*existing.source_preference, *incoming.source_preference]
                ),
                "quality_threshold": max(existing.quality_threshold, incoming.quality_threshold),
                "origins": (*existing.origins, *incoming.origins),
            }
        )

    def _ground_problem_variables(
        self,
        fields: dict[str, FieldContract],
        problem: ScientificProblemSpec,
        source_types: list[str],
    ) -> list[str]:
        warnings: list[str] = []
        for variable in problem.target_variables:
            matched, match_kind = self._match_variable(fields, variable)
            origin = FieldOrigin(
                kind=FieldOriginKind.PROBLEM,
                reference=f"{problem.problem_id}:{variable.evidence[0].start}-{variable.evidence[0].end}",
                rationale=f"Explicit target variable: {variable.name}",
            )
            if matched:
                unit_conflict = False
                if match_kind == "ambiguous":
                    warnings.append(
                        f"user variable '{variable.name}' matches multiple canonical fields"
                    )
                for field_name in matched:
                    current = fields[field_name]
                    requested_unit = variable.requested_unit
                    updated_origins = (
                        current.origins if origin in current.origins else (*current.origins, origin)
                    )
                    if requested_unit is not None and requested_unit not in current.allowed_units:
                        unit_conflict = True
                        fields[field_name] = FieldContract.model_validate(
                            {
                                **current.model_dump(),
                                "origins": updated_origins,
                            }
                        )
                        continue
                    update: dict[str, object] = {
                        "origins": updated_origins,
                    }
                    if (
                        match_kind == "exact"
                        and current.requirement is not FieldRequirement.DERIVED
                    ):
                        update.update(
                            {
                                "requirement": FieldRequirement.REQUIRED,
                                "nullable": False,
                            }
                        )
                    if requested_unit is not None:
                        update["target_unit"] = requested_unit
                    fields[field_name] = FieldContract.model_validate(
                        {**current.model_dump(), **update}
                    )
                if unit_conflict:
                    warnings.append(
                        f"requested unit for '{variable.name}' is not allowed by the matched field"
                    )
                continue
            name = f"requested_field_{hashlib.sha256(variable.name.encode()).hexdigest()[:8]}"
            fields[name] = FieldContract(
                name=name,
                description=f"Unresolved user-requested variable: {variable.name}",
                requirement=FieldRequirement.REQUIRED,
                data_type=DataType.STRING,
                semantic_type="unresolved_user_variable",
                aliases=(variable.name,),
                nullable=False,
                source_preference=_unique(source_types) or ("paper_table",),
                quality_threshold=1.0,
                origins=(origin,),
            )
            warnings.append(
                f"user variable '{variable.name}' has unresolved type/dimension and requires review"
            )
        return warnings

    @staticmethod
    def _match_variable(
        fields: dict[str, FieldContract], variable: VariableIntent
    ) -> tuple[tuple[str, ...], Literal["exact", "ambiguous", "product", "none"]]:
        normalized = ContractCompiler._normalize_concept(variable.name)
        exact_matches: list[str] = []
        for name, field in fields.items():
            candidates = (field.name, field.semantic_type, *field.aliases)
            if any(
                ContractCompiler._normalize_concept(value) == normalized for value in candidates
            ):
                exact_matches.append(name)
        if exact_matches:
            match_kind: Literal["exact", "ambiguous"] = (
                "exact" if len(exact_matches) == 1 else "ambiguous"
            )
            return tuple(exact_matches), match_kind
        product_fields = tuple(
            name
            for name, field in fields.items()
            if any(
                origin.kind is FieldOriginKind.TASK_PACK
                and ContractCompiler._normalize_concept(origin.reference.split("@", 1)[0])
                == normalized
                for origin in field.origins
            )
        )
        if product_fields:
            return product_fields, "product"
        return (), "none"

    @staticmethod
    def _normalize_concept(value: str) -> str:
        normalized = re.sub(r"[\W_]+", "", value.casefold())
        return normalized[:-1] if normalized.endswith("s") else normalized

    @staticmethod
    def _canonical_schema(contract_id: str, fields: tuple[FieldContract, ...]) -> CanonicalSchema:
        canonical_fields_list: list[CanonicalField] = []
        for field in fields:
            json_type: Literal["string", "integer", "number", "boolean"]
            if field.data_type in {DataType.STRING, DataType.DATETIME}:
                json_type = "string"
            elif field.data_type is DataType.INTEGER:
                json_type = "integer"
            elif field.data_type is DataType.NUMBER:
                json_type = "number"
            else:
                json_type = "boolean"
            canonical_fields_list.append(
                CanonicalField(
                    name=field.name,
                    json_type=json_type,
                    format="date-time" if field.data_type is DataType.DATETIME else None,
                    nullable=field.nullable,
                    required=field.requirement is FieldRequirement.REQUIRED,
                    description=field.description,
                    minimum=field.valid_range.minimum if field.valid_range is not None else None,
                    maximum=field.valid_range.maximum if field.valid_range is not None else None,
                )
            )
        canonical_fields = tuple(canonical_fields_list)
        required = tuple(field.name for field in canonical_fields if field.required)
        schema_hash = canonical_hash(
            {
                "fields": [field.model_dump(mode="json") for field in canonical_fields],
                "required": required,
            }
        )
        return CanonicalSchema(
            schema_id=_stable_id("sch", schema_hash),
            contract_id=contract_id,
            fields=canonical_fields,
            required_fields=required,
            schema_hash=schema_hash,
        )


class ContractDiffService:
    """Compare immutable contract versions without mutating either version."""

    @staticmethod
    def compare(old: ScientificDataContract, new: ScientificDataContract) -> ContractDiff:
        old_fields = {field.name: field for field in old.fields}
        new_fields = {field.name: field for field in new.fields}
        changes: list[FieldChange] = []
        for name in sorted(old_fields.keys() | new_fields.keys()):
            if name not in old_fields:
                changes.append(FieldChange(field_name=name, change="added", detail="field added"))
            elif name not in new_fields:
                changes.append(
                    FieldChange(field_name=name, change="removed", detail="field removed")
                )
            elif old_fields[name] != new_fields[name]:
                old_json = json.dumps(old_fields[name].model_dump(mode="json"), sort_keys=True)
                new_json = json.dumps(new_fields[name].model_dump(mode="json"), sort_keys=True)
                detail_hash = hashlib.sha256(f"{old_json}->{new_json}".encode()).hexdigest()[:12]
                changes.append(
                    FieldChange(
                        field_name=name,
                        change="changed",
                        detail=f"field definition changed ({detail_hash})",
                    )
                )
        metadata_changes: list[ContractMetadataChange] = []
        metadata_pairs: tuple[tuple[MetadataArea, object, object, bool], ...] = (
            ("entity_keys", old.entity_keys, new.entity_keys, True),
            (
                "acceptable_source_types",
                old.acceptable_source_types,
                new.acceptable_source_types,
                False,
            ),
            ("quality_gates", old.quality_gates, new.quality_gates, True),
            (
                "selection_constraints",
                old.selection_constraints,
                new.selection_constraints,
                True,
            ),
            ("assumptions", old.assumptions, new.assumptions, False),
            ("output_formats", old.output_formats, new.output_formats, False),
            ("license_policy", old.license_policy, new.license_policy, True),
        )
        for area, old_value, new_value, breaking in metadata_pairs:
            if old_value == new_value:
                continue
            change_hash = canonical_hash(
                {
                    "old": _json_compatible(old_value),
                    "new": _json_compatible(new_value),
                }
            )[:12]
            metadata_changes.append(
                ContractMetadataChange(
                    area=area,
                    detail=f"contract metadata changed ({change_hash})",
                    breaking=breaking,
                )
            )
        return ContractDiff(
            old_contract_id=old.contract_id,
            new_contract_id=new.contract_id,
            old_version=old.version,
            new_version=new.version,
            field_changes=tuple(changes),
            metadata_changes=tuple(metadata_changes),
        )

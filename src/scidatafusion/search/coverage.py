"""Deterministic M04 coverage-matrix initialization."""

from __future__ import annotations

from datetime import datetime

from scidatafusion.contracts.scientific import (
    FieldRequirement,
    QualityGateKind,
    ScientificDataContract,
)
from scidatafusion.contracts.search import (
    CoverageCell,
    CoverageGateTarget,
    CoverageMatrixTemplate,
    CoverageState,
    QueryFamily,
    QueryFamilyState,
    SourceCapability,
)
from scidatafusion.domain.registry import canonical_hash


def _stable_id(prefix: str, value: object) -> str:
    return f"{prefix}_{canonical_hash(value)[:16]}"


class CoveragePlanner:
    """Project contract field/source requirements into an initially uncovered matrix."""

    @staticmethod
    def build(
        contract: ScientificDataContract,
        families: tuple[QueryFamily, ...],
        capabilities: tuple[SourceCapability, ...],
        *,
        created_at: datetime,
        producer_version: str,
    ) -> CoverageMatrixTemplate:
        """Build exact field-by-source-preference cells and blocking gate targets."""

        family_by_source = {item.source_id: item for item in families}
        capability_by_source = {item.source_id: item for item in capabilities}
        blocking_gate_fields = {
            field_name
            for gate in contract.quality_gates
            if gate.blocking
            for field_name in gate.fields
        }
        cells: list[CoverageCell] = []
        for field in contract.fields:
            for contract_source_type in field.source_preference:
                matching_families = tuple(
                    family
                    for source_id, family in family_by_source.items()
                    if contract_source_type in capability_by_source[source_id].contract_source_types
                    and field.name in family.target_fields
                )
                planned_query_ids = tuple(
                    query.query_id
                    for family in matching_families
                    for query in family.queries
                    if field.name in query.target_fields
                )
                available_source_ids = tuple(
                    family.source_id
                    for family in matching_families
                    if family.state is not QueryFamilyState.CAPABILITY_UNAVAILABLE
                )
                if planned_query_ids:
                    state = CoverageState.PLANNED
                elif available_source_ids:
                    state = CoverageState.DEFERRED
                else:
                    state = CoverageState.UNAVAILABLE
                cells.append(
                    CoverageCell(
                        cell_id=_stable_id(
                            "cvg",
                            (contract.contract_hash, field.name, contract_source_type),
                        ),
                        field_name=field.name,
                        requirement=field.requirement,
                        contract_source_type=contract_source_type,
                        source_ids=available_source_ids,
                        planned_query_ids=planned_query_ids,
                        state=state,
                        critical=(
                            field.requirement is FieldRequirement.REQUIRED
                            or field.name in blocking_gate_fields
                        ),
                    )
                )

        gate_targets: list[CoverageGateTarget] = []
        for gate in contract.quality_gates:
            query_ids = tuple(
                query.query_id
                for family in families
                for query in family.queries
                if gate.gate_id in query.target_gate_ids
            )
            relevant_families = tuple(
                family for family in families if set(gate.fields).intersection(family.target_fields)
            )
            if query_ids:
                state = CoverageState.PLANNED
            elif any(item.state is QueryFamilyState.BUDGET_DEFERRED for item in relevant_families):
                state = CoverageState.DEFERRED
            else:
                state = CoverageState.UNAVAILABLE
            gate_targets.append(
                CoverageGateTarget(
                    gate_id=gate.gate_id,
                    fields=gate.fields,
                    match_mode=("any" if gate.kind is QualityGateKind.ANY_OF_FIELDS else "all"),
                    critical=gate.blocking,
                    planned_query_ids=query_ids,
                    state=state,
                )
            )
        return CoverageMatrixTemplate(
            task_id=contract.task_id,
            run_id=contract.run_id,
            contract_version=contract.version,
            created_at=created_at,
            producer_version=producer_version,
            contract_hash=contract.contract_hash,
            cells=tuple(cells),
            gate_targets=tuple(gate_targets),
        )

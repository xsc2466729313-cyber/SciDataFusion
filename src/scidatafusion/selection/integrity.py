"""Canonical M06 hashes and upstream-bound integrity verification."""

from __future__ import annotations

import hmac
from typing import NoReturn

from scidatafusion.connectors.integrity import verify_connector_execution_integrity
from scidatafusion.contracts.scientific import ContractStatus, FieldRequirement
from scidatafusion.contracts.selection import (
    CoverageReport,
    ScopeCoverageState,
    SearchGapSet,
    SelectedSourceSet,
    SourceSelectionRequest,
    SourceSelectionResult,
)
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.schema import ContractCompiler
from scidatafusion.search import verify_search_plan_integrity
from scidatafusion.search.stop import SearchStopPolicy
from scidatafusion.selection.projection import (
    applicable_categories,
    assess_license,
    candidate_claims,
    candidate_download_locators,
)


def calculate_selection_input_hash(request: SourceSelectionRequest) -> str:
    """Hash the exact immutable upstream snapshots and M06 policy state."""

    return canonical_hash(
        {
            "candidate_set_hash": request.connector_result.candidate_set.candidate_set_hash,
            "connector_output_hash": request.connector_result.output_hash,
            "contract_hash": request.contract.contract_hash,
            "policy": request.policy.model_dump(mode="json"),
            "round_context": request.round_context.model_dump(mode="json"),
            "search_plan_hash": request.search_plan.plan_hash,
        }
    )


def calculate_selected_source_set_hash(selected: SelectedSourceSet) -> str:
    """Recalculate the semantic hash of the selected source set."""

    payload = selected.model_dump(
        mode="json",
        exclude={"created_at", "selected_source_set_hash", "selection_id"},
    )
    return canonical_hash(payload)


def calculate_coverage_report_hash(report: CoverageReport) -> str:
    """Recalculate the candidate-only coverage report hash."""

    payload = report.model_dump(
        mode="json",
        exclude={"coverage_report_hash", "coverage_report_id", "created_at"},
    )
    return canonical_hash(payload)


def calculate_search_gap_set_hash(gaps: SearchGapSet) -> str:
    """Recalculate the search-gap set and deterministic directives hash."""

    payload = gaps.model_dump(
        mode="json",
        exclude={"created_at", "gap_set_id", "search_gap_set_hash"},
    )
    return canonical_hash(payload)


def calculate_source_selection_output_hash(result: SourceSelectionResult) -> str:
    """Hash every semantic M06 output field except the transport event envelope."""

    return canonical_hash(
        {
            "contract_version": result.contract_version,
            "coverage_report_hash": result.coverage_report.coverage_report_hash,
            "created_at": result.created_at.isoformat(),
            "idempotency_key": result.idempotency_key,
            "input_hash": result.input_hash,
            "metrics": result.metrics.model_dump(mode="json"),
            "producer_version": result.producer_version,
            "progress_snapshot": result.progress_snapshot.model_dump(mode="json"),
            "round_context": result.round_context.model_dump(mode="json"),
            "run_id": result.run_id,
            "search_gap_set_hash": result.search_gap_set.search_gap_set_hash,
            "selected_source_set_hash": (result.selected_source_set.selected_source_set_hash),
            "status": result.status.value,
            "stop_decision": result.stop_decision.model_dump(mode="json"),
            "stop_policy": result.stop_policy.model_dump(mode="json"),
            "task_id": result.task_id,
            "warnings": list(result.warnings),
        }
    )


def verify_selection_request_integrity(request: SourceSelectionRequest) -> None:
    """Reject tampered, unconfirmed, or cross-run M03/M04/M05 inputs."""

    ContractCompiler.verify_integrity(request.contract)
    verify_search_plan_integrity(request.search_plan)
    verify_connector_execution_integrity(request.connector_result)
    if request.contract.status is not ContractStatus.CONFIRMED:
        _fail("M06 requires an explicitly confirmed scientific data contract")
    if any(
        len(candidate.coverage_claims)
        != len({claim.field_name for claim in candidate.coverage_claims})
        for candidate in request.connector_result.candidate_set.candidates
    ):
        _fail("M06 candidates must contain at most one coverage claim per field")


def verify_source_selection_integrity(
    result: SourceSelectionResult,
    request: SourceSelectionRequest,
) -> None:
    """Verify M06 hashes, exact upstream projections, and reproducible stop policy."""

    verify_selection_request_integrity(request)
    selected = result.selected_source_set
    report = result.coverage_report
    candidates = request.connector_result.candidate_set.candidates
    expected_input_hash = calculate_selection_input_hash(request)
    if not (
        hmac.compare_digest(result.input_hash, expected_input_hash)
        and hmac.compare_digest(result.idempotency_key, expected_input_hash)
        and result.round_context == request.round_context
        and result.stop_policy == request.search_plan.stop_policy
        and selected.policy == request.policy
    ):
        _fail("M06 result does not match its immutable request snapshot")

    expected_duplicate_count = len(candidates) - len(
        {candidate.replica_group_key for candidate in candidates}
    )
    expected_available_bytes = max(
        0,
        request.search_plan.stop_policy.max_download_bytes - request.round_context.downloaded_bytes,
    )
    if not (
        selected.candidate_count == len(candidates)
        and selected.duplicate_replica_count == expected_duplicate_count
        and selected.applicable_source_category_count
        == len(applicable_categories(request.search_plan))
        and selected.applicable_contract_source_type_count
        == len(request.contract.acceptable_source_types)
        and selected.available_download_bytes == expected_available_bytes
    ):
        _fail("M06 selection counts and download budget are not upstream-derived")

    candidates_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    for source in selected.sources:
        candidate = candidates_by_id.get(source.candidate_id)
        if candidate is None:
            _fail("M06 selected a candidate that is absent from the M05 candidate set")
        expected_claims = candidate_claims(candidate, request.search_plan, request.policy)
        expected_license, expected_rationale = assess_license(candidate)
        if not (
            source.candidate_hash == candidate.candidate_hash
            and source.replica_group_key == candidate.replica_group_key
            and source.coverage_claims == expected_claims
            and source.source_ids == candidate.source_ids
            and source.categories == candidate.categories
            and source.record_types == candidate.record_types
            and source.download_locators == candidate_download_locators(candidate)
            and source.access_statuses == candidate.access_statuses
            and source.license_labels == candidate.license_labels
            and source.primary_source == candidate.primary_source
            and source.assessment_score == candidate.assessment.total_score
            and source.budget_reservation_bytes == request.policy.unknown_size_reservation_bytes
            and source.license_decision is expected_license
            and source.license_rationale == expected_rationale
        ):
            _fail("M06 selected-source metadata is not an exact M05 projection")

    critical_fields = {
        field.name
        for field in request.contract.fields
        if field.requirement is FieldRequirement.REQUIRED
    }
    critical_fields.update(
        field_name
        for gate in request.contract.quality_gates
        if gate.blocking
        for field_name in gate.fields
    )
    if tuple(
        (field.field_name, field.requirement, field.critical) for field in report.fields
    ) != tuple(
        (field.name, field.requirement, field.name in critical_fields)
        for field in request.contract.fields
    ):
        _fail("M06 field coverage must include the exact contract field universe")
    if tuple(
        (cell.cell_id, cell.field_name, cell.contract_source_type) for cell in report.cells
    ) != tuple(
        (cell.cell_id, cell.field_name, cell.contract_source_type)
        for cell in request.search_plan.coverage_matrix.cells
    ):
        _fail("M06 cell coverage must include the exact M04 coverage matrix")
    if tuple(
        (gate.gate_id, gate.kind, gate.fields, gate.blocking, gate.threshold)
        for gate in report.gates
    ) != tuple(
        (gate.gate_id, gate.kind, gate.fields, gate.blocking, gate.threshold)
        for gate in request.contract.quality_gates
    ):
        _fail("M06 gate coverage must include the exact contract quality gates")
    if tuple((scope.constraint_id, scope.kind) for scope in report.scopes) != tuple(
        (constraint.constraint_id, constraint.kind)
        for constraint in request.contract.selection_constraints
    ) or any(scope.state is not ScopeCoverageState.UNVERIFIED for scope in report.scopes):
        _fail("M06 scope coverage must preserve every constraint without inference")
    if tuple(item.contract_source_type for item in report.source_types) != (
        request.contract.acceptable_source_types
    ):
        _fail("M06 source-type coverage must include every acceptable contract source type")
    if report.entity_key_fields != request.contract.entity_keys:
        _fail("M06 entity-key coverage must preserve the confirmed contract keys")
    known_gate_ids = {gate.gate_id for gate in request.contract.quality_gates}
    known_constraint_ids = {
        constraint.constraint_id for constraint in request.contract.selection_constraints
    }
    if any(
        not set(gap.quality_gate_ids).issubset(known_gate_ids)
        or not set(gap.constraint_ids).issubset(known_constraint_ids)
        for gap in result.search_gap_set.gaps
    ):
        _fail("M06 search gaps must resolve to confirmed gates and selection constraints")

    projections_by_candidate = {
        candidate.candidate_id: candidate_claims(
            candidate,
            request.search_plan,
            request.policy,
        )
        for candidate in candidates
    }
    for cell in report.cells:
        expected_available = sum(
            any(
                claim.field_name == cell.field_name
                and cell.contract_source_type in claim.contract_source_types
                for claim in projections
            )
            for projections in projections_by_candidate.values()
        )
        if cell.available_candidate_count != expected_available:
            _fail("M06 coverage-cell availability is not derived from M05 candidates")

    selected_hash = calculate_selected_source_set_hash(selected)
    report_hash = calculate_coverage_report_hash(report)
    gap_hash = calculate_search_gap_set_hash(result.search_gap_set)
    output_hash = calculate_source_selection_output_hash(result)
    if not (
        hmac.compare_digest(selected.selected_source_set_hash, selected_hash)
        and hmac.compare_digest(selected.selection_id, f"sel_{selected_hash[:32]}")
        and hmac.compare_digest(report.coverage_report_hash, report_hash)
        and hmac.compare_digest(report.coverage_report_id, f"cvr_{report_hash[:32]}")
        and hmac.compare_digest(result.search_gap_set.search_gap_set_hash, gap_hash)
        and hmac.compare_digest(result.search_gap_set.gap_set_id, f"sgs_{gap_hash[:32]}")
        and hmac.compare_digest(result.output_hash, output_hash)
    ):
        _fail("M06 result content does not match its immutable hashes")
    expected_stop = SearchStopPolicy.evaluate(result.stop_policy, result.progress_snapshot)
    if result.stop_decision != expected_stop:
        _fail("M06 stop decision is not reproducible from the retained progress snapshot")


def _fail(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)

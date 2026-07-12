from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from scidatafusion.cli import _build_search_planning, _execute_offline_connectors
from scidatafusion.contracts.search import (
    SearchStopDecision,
    SearchStopOutcome,
    SearchStopReason,
)
from scidatafusion.contracts.selection import (
    SelectionGapCode,
    SelectionRoundContext,
    SourceSelectionRequest,
    SourceSelectionStatus,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.selection import (
    SourceSelectionService,
    calculate_selection_input_hash,
    calculate_source_selection_output_hash,
    candidate_claims,
    verify_selection_request_integrity,
    verify_source_selection_integrity,
)

_GOAL = "Study Type Ia supernova light curves using multi-source integration into CSV."


@pytest.fixture(scope="module")
def ia_request() -> SourceSelectionRequest:
    phase1, planning = _build_search_planning(_GOAL, "authenticated-m06-reviewer")
    assert planning is not None
    assert phase1.confirmation is not None
    connector_result = asyncio.run(_execute_offline_connectors(planning))
    return SourceSelectionRequest(
        contract=phase1.confirmation.contract,
        search_plan=planning.plan,
        connector_result=connector_result,
    )


def test_selection_request_verifies_every_upstream_hash(
    ia_request: SourceSelectionRequest,
) -> None:
    verify_selection_request_integrity(ia_request)

    tampered_contract = ia_request.contract.model_copy(
        update={"domain_profile": (*ia_request.contract.domain_profile, "forged_domain")}
    )
    tampered_request = ia_request.model_copy(update={"contract": tampered_contract})
    with pytest.raises(AppError) as exc_info:
        verify_selection_request_integrity(tampered_request)
    assert exc_info.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_selection_request_rejects_cross_run_connector_results(
    ia_request: SourceSelectionRequest,
) -> None:
    other_run = ia_request.connector_result.model_copy(update={"run_id": f"run_{'f' * 32}"})
    with pytest.raises(ValidationError, match="share result metadata"):
        SourceSelectionRequest(
            contract=ia_request.contract,
            search_plan=ia_request.search_plan,
            connector_result=other_run,
        )


def test_candidate_claims_are_matrix_bound_and_input_hash_covers_policy(
    ia_request: SourceSelectionRequest,
) -> None:
    projections = tuple(
        candidate_claims(candidate, ia_request.search_plan, ia_request.policy)
        for candidate in ia_request.connector_result.candidate_set.candidates
    )
    assert all(projections)
    assert all(
        claim.contract_source_types and claim.source_ids
        for candidate_projection in projections
        for claim in candidate_projection
    )

    changed_policy = ia_request.policy.model_copy(
        update={
            "unknown_size_reservation_bytes": (ia_request.policy.unknown_size_reservation_bytes + 1)
        }
    )
    changed_request = ia_request.model_copy(update={"policy": changed_policy})
    assert calculate_selection_input_hash(changed_request) != calculate_selection_input_hash(
        ia_request
    )


def test_ia_source_selection_is_idempotent_and_upstream_verifiable(
    ia_request: SourceSelectionRequest,
) -> None:
    service = SourceSelectionService()
    result = service.select(ia_request)
    replay = service.select(ia_request)

    assert replay is result
    assert result.event.event_type.value == "selection.completed"
    assert result.metrics.candidate_count == 5
    assert result.metrics.selected_source_count >= 3
    assert result.coverage_report.required_candidate_coverage == 1.0
    assert len({item.replica_group_key for item in result.selected_source_set.sources}) == len(
        result.selected_source_set.sources
    )
    assert result.metrics.continue_search
    verify_source_selection_integrity(result, ia_request)


def test_download_limit_produces_auditable_partial_stop(
    ia_request: SourceSelectionRequest,
) -> None:
    exhausted = SourceSelectionRequest(
        contract=ia_request.contract,
        search_plan=ia_request.search_plan,
        connector_result=ia_request.connector_result,
        policy=ia_request.policy,
        round_context=SelectionRoundContext(
            downloaded_bytes=ia_request.search_plan.stop_policy.max_download_bytes
        ),
    )
    result = SourceSelectionService().select(exhausted)

    assert result.status is SourceSelectionStatus.NEEDS_REVIEW
    assert result.selected_source_set.sources == ()
    assert result.stop_decision.reason is SearchStopReason.DOWNLOAD_LIMIT
    assert not result.metrics.continue_search
    assert SelectionGapCode.BUDGET_EXHAUSTED in {item.code for item in result.search_gap_set.gaps}
    verify_source_selection_integrity(result, exhausted)


def test_selection_integrity_rejects_body_and_stop_decision_tampering(
    ia_request: SourceSelectionRequest,
) -> None:
    result = SourceSelectionService().select(ia_request)
    source = result.selected_source_set.sources[0]
    altered_reason = source.reasons[0].model_copy(update={"detail": "forged reason"})
    altered_source = source.model_copy(update={"reasons": (altered_reason, *source.reasons[1:])})
    altered_set = result.selected_source_set.model_copy(
        update={"sources": (altered_source, *result.selected_source_set.sources[1:])}
    )
    with pytest.raises(AppError, match="immutable hashes"):
        verify_source_selection_integrity(
            result.model_copy(update={"selected_source_set": altered_set}),
            ia_request,
        )

    false_stop = SearchStopDecision(
        should_stop=True,
        reason=SearchStopReason.COVERAGE_SATURATED,
        outcome=SearchStopOutcome.SUCCEEDED,
        detail="Forged saturation decision.",
    )
    altered_result = result.model_copy(update={"stop_decision": false_stop})
    altered_result = altered_result.model_copy(
        update={"output_hash": calculate_source_selection_output_hash(altered_result)}
    )
    with pytest.raises(AppError, match="not reproducible"):
        verify_source_selection_integrity(altered_result, ia_request)

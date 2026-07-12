from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from scidatafusion.contracts.connectors import (
    CandidateIdentifier,
    IdentifierKind,
    SourceRecordType,
)
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.scientific import FieldRequirement
from scidatafusion.contracts.search import (
    SearchStopDecision,
    SearchStopOutcome,
    SearchStopReason,
    SourceCategory,
)
from scidatafusion.contracts.selection import (
    CandidateCoverageState,
    CoverageCellObservation,
    CoverageReport,
    DownloadReadiness,
    FieldCoverage,
    LicenseDecision,
    SearchCompletedPayload,
    SearchGapSet,
    SelectedSource,
    SelectedSourceSet,
    SelectionReason,
    SelectionReasonCode,
    SelectionRoundContext,
    SourceSelectionMetrics,
    SourceSelectionPolicy,
    SourceSelectionResult,
    SourceSelectionStatus,
    SourceTypeCoverage,
)

NOW = datetime(2026, 7, 12, 6, 0, tzinfo=UTC)
HASH = "a" * 64
OTHER_HASH = "b" * 64


def _selected_source() -> SelectedSource:
    return SelectedSource(
        candidate_id="src_11111111111111111111111111111111",
        candidate_hash=HASH,
        replica_group_key="replica:one",
        selection_rank=1,
        reasons=(
            SelectionReason(
                reason_id="srn_1111111111111111",
                code=SelectionReasonCode.REQUIRED_FIELD_GAIN,
                detail="Adds the required object identifier candidate claim.",
                target_fields=("object_id",),
                contract_source_types=("open_database",),
            ),
        ),
        covered_fields=("object_id",),
        covered_contract_source_types=("open_database",),
        source_ids=("vizier_tap",),
        categories=(SourceCategory.DOMAIN_DATABASE,),
        assigned_diversity_category=SourceCategory.DOMAIN_DATABASE,
        record_types=(SourceRecordType.CATALOG,),
        download_locators=(
            CandidateIdentifier(
                kind=IdentifierKind.EXTERNAL,
                value="vizier_tap:J/Test/1",
            ),
        ),
        evidence_ids=("sev_1111111111111111",),
        primary_source=True,
        assessment_score=0.8,
        marginal_required_coverage=1.0,
        cumulative_required_coverage=1.0,
        budget_reservation_bytes=1_000_000,
        download_readiness=DownloadReadiness.IDENTIFIER_RESOLUTION,
        license_decision=LicenseDecision.NEEDS_REVIEW,
    )


def _selected_set() -> SelectedSourceSet:
    return SelectedSourceSet(
        task_id="tsk_11111111111111111111111111111111",
        run_id="run_11111111111111111111111111111111",
        contract_version="1.0.0",
        created_at=NOW,
        producer_version="1.0.0",
        selection_id="sel_11111111111111111111111111111111",
        contract_id="ctr_11111111111111111111111111111111",
        contract_hash=HASH,
        search_plan_id="spl_11111111111111111111111111111111",
        search_plan_hash=OTHER_HASH,
        candidate_set_hash=HASH,
        policy=SourceSelectionPolicy(),
        candidate_count=1,
        duplicate_replica_count=0,
        available_download_bytes=5_000_000,
        reserved_download_bytes=1_000_000,
        sources=(_selected_source(),),
        selected_source_set_hash=OTHER_HASH,
    )


def _coverage_report() -> CoverageReport:
    return CoverageReport(
        task_id="tsk_11111111111111111111111111111111",
        run_id="run_11111111111111111111111111111111",
        contract_version="1.0.0",
        created_at=NOW,
        producer_version="1.0.0",
        coverage_report_id="cvr_11111111111111111111111111111111",
        contract_id="ctr_11111111111111111111111111111111",
        contract_hash=HASH,
        search_plan_hash=OTHER_HASH,
        candidate_set_hash=HASH,
        selected_source_set_hash=OTHER_HASH,
        fields=(
            FieldCoverage(
                field_name="object_id",
                requirement=FieldRequirement.REQUIRED,
                critical=True,
                state=CandidateCoverageState.CANDIDATE_COVERED,
                maximum_confidence=0.8,
                candidate_ids=("src_11111111111111111111111111111111",),
                evidence_ids=("sev_1111111111111111",),
                contract_source_types=("open_database",),
                source_ids=("vizier_tap",),
            ),
        ),
        cells=(
            CoverageCellObservation(
                cell_id="cvg_1111111111111111",
                field_name="object_id",
                contract_source_type="open_database",
                available_candidate_count=1,
                selected_candidate_count=1,
                selected_candidate_ids=("src_11111111111111111111111111111111",),
                evidence_ids=("sev_1111111111111111",),
                state=CandidateCoverageState.CANDIDATE_COVERED,
            ),
        ),
        source_types=(
            SourceTypeCoverage(
                contract_source_type="open_database",
                state=CandidateCoverageState.CANDIDATE_COVERED,
                selected_candidate_ids=("src_11111111111111111111111111111111",),
                fields=("object_id",),
            ),
        ),
        entity_key_fields=("object_id",),
        selected_categories=(SourceCategory.DOMAIN_DATABASE,),
        required_candidate_coverage=1.0,
        entity_key_candidate_coverage=1.0,
        source_type_candidate_coverage=1.0,
        has_primary_source=True,
        coverage_report_hash=HASH,
    )


def _gap_set() -> SearchGapSet:
    return SearchGapSet(
        task_id="tsk_11111111111111111111111111111111",
        run_id="run_11111111111111111111111111111111",
        contract_version="1.0.0",
        created_at=NOW,
        producer_version="1.0.0",
        gap_set_id="sgs_11111111111111111111111111111111",
        contract_id="ctr_11111111111111111111111111111111",
        contract_hash=HASH,
        search_plan_hash=OTHER_HASH,
        candidate_set_hash=HASH,
        selected_source_set_hash=OTHER_HASH,
        gaps=(),
        directives=(),
        search_gap_set_hash=HASH,
    )


def _result() -> SourceSelectionResult:
    metrics = SourceSelectionMetrics(
        candidate_count=1,
        selected_source_count=1,
        duplicate_replica_count=0,
        required_field_count=1,
        candidate_covered_required_field_count=1,
        uncertain_required_field_count=0,
        uncovered_required_field_count=0,
        applicable_source_type_count=1,
        covered_source_type_count=1,
        selected_source_category_count=1,
        primary_source_selected=True,
        gap_count=0,
        blocking_gap_count=0,
        reserved_download_bytes=1_000_000,
        continue_search=True,
    )
    payload = SearchCompletedPayload(
        status=SourceSelectionStatus.SUCCEEDED,
        selection_id="sel_11111111111111111111111111111111",
        selected_source_set_hash=OTHER_HASH,
        coverage_report_hash=HASH,
        search_gap_set_hash=HASH,
        stop_reason=SearchStopReason.CONTINUE_SEARCH,
        continue_search=True,
        input_hash=HASH,
        output_hash=OTHER_HASH,
        idempotency_key=HASH,
        selected_source_count=1,
        gap_count=0,
    )
    return SourceSelectionResult(
        task_id="tsk_11111111111111111111111111111111",
        run_id="run_11111111111111111111111111111111",
        contract_version="1.0.0",
        created_at=NOW,
        producer_version="1.0.0",
        status=SourceSelectionStatus.SUCCEEDED,
        input_hash=HASH,
        output_hash=OTHER_HASH,
        idempotency_key=HASH,
        selected_source_set=_selected_set(),
        coverage_report=_coverage_report(),
        search_gap_set=_gap_set(),
        stop_decision=SearchStopDecision(
            should_stop=False,
            reason=SearchStopReason.CONTINUE_SEARCH,
            outcome=SearchStopOutcome.CONTINUE,
            detail="A second low-gain round is required before saturation can stop search.",
        ),
        metrics=metrics,
        event=EventEnvelope[SearchCompletedPayload](
            event_type=EventType.SEARCH_COMPLETED,
            task_id="tsk_11111111111111111111111111111111",
            run_id="run_11111111111111111111111111111111",
            occurred_at=NOW,
            producer=ProducerRef(component="source_selection_service", version="1.0.0"),
            payload=payload,
        ),
    )


def test_selection_contracts_form_a_strict_cross_linked_result() -> None:
    result = _result()

    assert result.module_id == "M06"
    assert result.metrics.selected_source_count == 1
    assert result.coverage_report.candidate_only
    assert result.selected_source_set.sources[0].candidate_only

    payload = result.model_dump(mode="python")
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        SourceSelectionResult.model_validate(payload)


def test_selection_policy_round_history_and_url_locators_fail_closed() -> None:
    with pytest.raises(ValidationError, match="uncertain confidence"):
        SourceSelectionPolicy(
            minimum_claim_confidence=0.2,
            uncertain_claim_confidence=0.3,
        )
    with pytest.raises(ValidationError, match="current round"):
        SelectionRoundContext(completed_rounds=1, prior_marginal_gains=(0.1,))
    with pytest.raises(ValidationError, match="absolute HTTPS"):
        CandidateIdentifier(kind=IdentifierKind.URL, value="http://example.org/data")


def test_selected_set_and_coverage_ratios_are_derived() -> None:
    selected = _selected_set().model_dump(mode="python")
    selected["reserved_download_bytes"] = 2
    with pytest.raises(ValidationError, match="reserved bytes"):
        SelectedSourceSet.model_validate(selected)

    report = _coverage_report().model_dump(mode="python")
    report["required_candidate_coverage"] = 0.5
    with pytest.raises(ValidationError, match="coverage ratios"):
        CoverageReport.model_validate(report)


def test_result_rejects_tampered_metrics_status_warnings_and_event() -> None:
    result = _result()
    payload = result.model_dump(mode="python")
    payload["metrics"]["selected_source_count"] = 0
    with pytest.raises(ValidationError, match="metrics"):
        SourceSelectionResult.model_validate(payload)

    payload = result.model_dump(mode="python")
    payload["status"] = SourceSelectionStatus.PARTIAL
    payload["event"]["payload"]["status"] = SourceSelectionStatus.PARTIAL
    with pytest.raises(ValidationError, match="status and warnings"):
        SourceSelectionResult.model_validate(payload)

    payload = result.model_dump(mode="python")
    payload["event"]["payload"]["selected_source_count"] = 2
    with pytest.raises(ValidationError, match=r"search\.completed"):
        SourceSelectionResult.model_validate(payload)

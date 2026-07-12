"""Deterministic M06 coverage evaluation and source-combination selection."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from threading import RLock
from typing import TypeVar

from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.connectors import IdentifierKind, SourceCandidate
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.scientific import (
    FieldRequirement,
    QualityGate,
    QualityGateKind,
)
from scidatafusion.contracts.search import CoverageCell, SearchProgressSnapshot, SourceCategory
from scidatafusion.contracts.selection import (
    CandidateCoverageState,
    CoverageCellObservation,
    CoverageReport,
    DownloadReadiness,
    FieldCoverage,
    GapSearchDirective,
    GateCoverage,
    GateCoverageState,
    LicenseDecision,
    ScopeCoverage,
    ScopeCoverageState,
    SearchGapSet,
    SelectedCoverageClaim,
    SelectedSource,
    SelectedSourceSet,
    SelectionCompletedPayload,
    SelectionGap,
    SelectionGapCode,
    SelectionReason,
    SelectionReasonCode,
    SourceSelectionMetrics,
    SourceSelectionRequest,
    SourceSelectionResult,
    SourceSelectionStatus,
    SourceTypeCoverage,
)
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.search.stop import SearchStopPolicy
from scidatafusion.selection.integrity import (
    calculate_coverage_report_hash,
    calculate_search_gap_set_hash,
    calculate_selected_source_set_hash,
    calculate_selection_input_hash,
    calculate_source_selection_output_hash,
    verify_selection_request_integrity,
    verify_source_selection_integrity,
)
from scidatafusion.selection.projection import (
    applicable_categories,
    assess_license,
    candidate_claims,
    candidate_download_locators,
)

_ZERO_HASH = "0" * 64
_T = TypeVar("_T")


class SourceSelectionService:
    """Select a reproducible, budget-bounded set that maximizes contract coverage."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = utc_now,
        producer_version: str = "1.0.0",
    ) -> None:
        self._clock = clock
        self._producer_version = producer_version
        self._cache: dict[str, SourceSelectionResult] = {}
        self._lock = RLock()

    def select(self, request: SourceSelectionRequest) -> SourceSelectionResult:
        """Evaluate one immutable search round and return an idempotent M06 result."""

        verify_selection_request_integrity(request)
        input_hash = calculate_selection_input_hash(request)
        with self._lock:
            cached = self._cache.get(input_hash)
            if cached is not None:
                return cached
            result = self._build(request, input_hash=input_hash, created_at=self._clock())
            verify_source_selection_integrity(result, request)
            self._cache[input_hash] = result
            return result

    def _build(
        self,
        request: SourceSelectionRequest,
        *,
        input_hash: str,
        created_at: datetime,
    ) -> SourceSelectionResult:
        projections = {
            candidate.candidate_id: candidate_claims(
                candidate,
                request.search_plan,
                request.policy,
            )
            for candidate in request.connector_result.candidate_set.candidates
        }
        applicable = applicable_categories(request.search_plan)
        available_bytes = max(
            0,
            request.search_plan.stop_policy.max_download_bytes
            - request.round_context.downloaded_bytes,
        )
        selected_sources = self._select_sources(
            request,
            projections=projections,
            applicable=applicable,
            available_bytes=available_bytes,
        )
        candidates = request.connector_result.candidate_set.candidates
        selected_set = SelectedSourceSet(
            task_id=request.contract.task_id,
            run_id=request.contract.run_id,
            contract_version=request.contract.version,
            created_at=created_at,
            producer_version=self._producer_version,
            selection_id=f"sel_{'0' * 32}",
            contract_id=request.contract.contract_id,
            contract_hash=request.contract.contract_hash,
            search_plan_id=request.search_plan.plan_id,
            search_plan_hash=request.search_plan.plan_hash,
            candidate_set_hash=request.connector_result.candidate_set.candidate_set_hash,
            policy=request.policy,
            candidate_count=len(candidates),
            duplicate_replica_count=(
                len(candidates) - len({item.replica_group_key for item in candidates})
            ),
            applicable_source_category_count=len(applicable),
            applicable_contract_source_type_count=len(request.contract.acceptable_source_types),
            available_download_bytes=available_bytes,
            reserved_download_bytes=sum(item.budget_reservation_bytes for item in selected_sources),
            sources=selected_sources,
            selected_source_set_hash=_ZERO_HASH,
        )
        selected_hash = calculate_selected_source_set_hash(selected_set)
        selected_set = selected_set.model_copy(
            update={
                "selection_id": f"sel_{selected_hash[:32]}",
                "selected_source_set_hash": selected_hash,
            }
        )
        report = self._coverage_report(
            request,
            selected_set=selected_set,
            projections=projections,
            created_at=created_at,
        )
        gap_set = self._gap_set(
            request,
            selected_set=selected_set,
            report=report,
            applicable=applicable,
            created_at=created_at,
        )
        progress = self._progress(request, selected_set, report, gap_set)
        stop_decision = SearchStopPolicy.evaluate(request.search_plan.stop_policy, progress)
        metrics = self._metrics(selected_set, report, gap_set, stop_decision.should_stop)
        status = self._status(selected_set, metrics)
        warnings = tuple(f"{item.code.value}:{item.gap_id}" for item in gap_set.gaps)
        payload = SelectionCompletedPayload(
            status=status,
            selection_id=selected_set.selection_id,
            selected_source_set_hash=selected_set.selected_source_set_hash,
            coverage_report_hash=report.coverage_report_hash,
            search_gap_set_hash=gap_set.search_gap_set_hash,
            stop_reason=stop_decision.reason,
            continue_search=not stop_decision.should_stop,
            input_hash=input_hash,
            output_hash=_ZERO_HASH,
            idempotency_key=input_hash,
            selected_source_count=len(selected_set.sources),
            gap_count=len(gap_set.gaps),
        )
        event = EventEnvelope[SelectionCompletedPayload](
            event_id=_stable_id("evt", ("selection.completed", input_hash), length=32),
            event_type=EventType.SELECTION_COMPLETED,
            task_id=request.contract.task_id,
            run_id=request.contract.run_id,
            occurred_at=created_at,
            producer=ProducerRef(
                component="source_selection_service",
                version=self._producer_version,
            ),
            payload=payload,
        )
        result = SourceSelectionResult(
            task_id=request.contract.task_id,
            run_id=request.contract.run_id,
            contract_version=request.contract.version,
            created_at=created_at,
            producer_version=self._producer_version,
            status=status,
            input_hash=input_hash,
            output_hash=_ZERO_HASH,
            idempotency_key=input_hash,
            selected_source_set=selected_set,
            coverage_report=report,
            search_gap_set=gap_set,
            round_context=request.round_context,
            stop_policy=request.search_plan.stop_policy,
            progress_snapshot=progress,
            stop_decision=stop_decision,
            warnings=warnings,
            metrics=metrics,
            event=event,
        )
        output_hash = calculate_source_selection_output_hash(result)
        final_payload = payload.model_copy(update={"output_hash": output_hash})
        return SourceSelectionResult.model_validate(
            result.model_copy(
                update={
                    "output_hash": output_hash,
                    "event": event.model_copy(update={"payload": final_payload}),
                }
            ).model_dump(mode="python")
        )

    def _select_sources(
        self,
        request: SourceSelectionRequest,
        *,
        projections: dict[str, tuple[SelectedCoverageClaim, ...]],
        applicable: tuple[SourceCategory, ...],
        available_bytes: int,
    ) -> tuple[SelectedSource, ...]:
        candidates = _representative_candidates(
            request.connector_result.candidate_set.candidates,
            projections,
            request,
        )
        max_by_budget = available_bytes // request.policy.unknown_size_reservation_bytes
        limit = min(request.policy.max_selected_sources, max_by_budget)
        required_fields = tuple(
            field.name
            for field in request.contract.fields
            if field.requirement is FieldRequirement.REQUIRED
        )
        optional_fields = tuple(
            field.name
            for field in request.contract.fields
            if field.requirement is FieldRequirement.OPTIONAL
        )
        category_target = min(request.policy.minimum_source_categories, len(applicable))
        selected: list[SelectedSource] = []
        used_ids: set[str] = set()
        covered_required: set[str] = set()
        covered_optional: set[str] = set()
        covered_types: set[str] = set()
        assigned_categories: set[SourceCategory] = set()
        has_primary = False
        previous_coverage = 0.0
        while len(selected) < limit:
            ranked: list[tuple[tuple[object, ...], SourceCandidate, SourceCategory]] = []
            for candidate in candidates:
                if candidate.candidate_id in used_ids:
                    continue
                claims = projections[candidate.candidate_id]
                eligible_categories = tuple(
                    category for category in candidate.categories if category in applicable
                )
                if not claims or not eligible_categories:
                    continue
                assigned = next(
                    (
                        category
                        for category in eligible_categories
                        if category not in assigned_categories
                    ),
                    eligible_categories[0],
                )
                covered = {
                    claim.field_name
                    for claim in claims
                    if claim.state is CandidateCoverageState.CANDIDATE_COVERED
                }
                uncertain = {
                    claim.field_name
                    for claim in claims
                    if claim.state is CandidateCoverageState.UNCERTAIN
                }
                source_types = {
                    source_type
                    for claim in claims
                    if claim.state is CandidateCoverageState.CANDIDATE_COVERED
                    for source_type in claim.contract_source_types
                }
                required_gain = tuple(
                    field
                    for field in required_fields
                    if field in covered and field not in covered_required
                )
                uncertain_gain = tuple(
                    field
                    for field in required_fields
                    if field in uncertain and field not in covered_required
                )
                optional_gain = tuple(
                    field
                    for field in optional_fields
                    if field in covered and field not in covered_optional
                )
                type_gain = tuple(
                    source_type
                    for source_type in request.contract.acceptable_source_types
                    if source_type in source_types and source_type not in covered_types
                )
                primary_gain = (
                    request.policy.require_primary_source
                    and not has_primary
                    and (candidate.primary_source)
                )
                category_gain = (
                    len(assigned_categories) < category_target
                    and assigned not in assigned_categories
                )
                contributes = bool(
                    required_gain
                    or uncertain_gain
                    or optional_gain
                    or type_gain
                    or primary_gain
                    or category_gain
                )
                if not contributes:
                    continue
                direct = any(
                    item.kind is IdentifierKind.URL
                    for item in candidate_download_locators(candidate)
                )
                license_decision, _ = assess_license(candidate)
                rank_key: tuple[object, ...] = (
                    -len(required_gain),
                    -len(uncertain_gain),
                    -int(primary_gain),
                    -len(type_gain),
                    -int(category_gain),
                    -len(optional_gain),
                    -int(license_decision is LicenseDecision.ALLOWED),
                    -int(direct),
                    -candidate.assessment.total_score,
                    candidate.candidate_id,
                )
                ranked.append((rank_key, candidate, assigned))
            if not ranked:
                break
            _, candidate, assigned = min(ranked, key=lambda item: item[0])
            claims = projections[candidate.candidate_id]
            covered = {
                claim.field_name
                for claim in claims
                if claim.state is CandidateCoverageState.CANDIDATE_COVERED
            }
            source_types = {
                source_type
                for claim in claims
                if claim.state is CandidateCoverageState.CANDIDATE_COVERED
                for source_type in claim.contract_source_types
            }
            required_gain = tuple(
                field
                for field in required_fields
                if field in covered and field not in covered_required
            )
            optional_gain = tuple(
                field
                for field in optional_fields
                if field in covered and field not in covered_optional
            )
            type_gain = tuple(
                source_type
                for source_type in request.contract.acceptable_source_types
                if source_type in source_types and source_type not in covered_types
            )
            reasons = _selection_reasons(
                candidate,
                assigned=assigned,
                required_gain=required_gain,
                optional_gain=optional_gain,
                type_gain=type_gain,
                category_gain=assigned not in assigned_categories,
                primary_gain=candidate.primary_source and not has_primary,
            )
            covered_required.update(covered.intersection(required_fields))
            covered_optional.update(covered.intersection(optional_fields))
            covered_types.update(source_types)
            assigned_categories.add(assigned)
            has_primary = has_primary or candidate.primary_source
            cumulative = len(covered_required) / len(required_fields) if required_fields else 1.0
            locators = candidate_download_locators(candidate)
            license_decision, license_rationale = assess_license(candidate)
            selected.append(
                SelectedSource(
                    candidate_id=candidate.candidate_id,
                    candidate_hash=candidate.candidate_hash,
                    replica_group_key=candidate.replica_group_key,
                    selection_rank=len(selected) + 1,
                    reasons=reasons,
                    coverage_claims=claims,
                    covered_fields=tuple(
                        claim.field_name
                        for claim in claims
                        if claim.state is CandidateCoverageState.CANDIDATE_COVERED
                    ),
                    covered_contract_source_types=_unique(
                        source_type
                        for claim in claims
                        if claim.state is CandidateCoverageState.CANDIDATE_COVERED
                        for source_type in claim.contract_source_types
                    ),
                    source_ids=candidate.source_ids,
                    categories=candidate.categories,
                    assigned_diversity_category=assigned,
                    record_types=candidate.record_types,
                    download_locators=locators,
                    evidence_ids=_unique(
                        evidence_id for claim in claims for evidence_id in claim.evidence_ids
                    ),
                    access_statuses=candidate.access_statuses,
                    license_labels=candidate.license_labels,
                    primary_source=candidate.primary_source,
                    assessment_score=candidate.assessment.total_score,
                    marginal_required_coverage=max(0.0, cumulative - previous_coverage),
                    cumulative_required_coverage=cumulative,
                    budget_reservation_bytes=request.policy.unknown_size_reservation_bytes,
                    download_readiness=(
                        DownloadReadiness.DIRECT_URL
                        if any(item.kind is IdentifierKind.URL for item in locators)
                        else DownloadReadiness.IDENTIFIER_RESOLUTION
                    ),
                    license_decision=license_decision,
                    license_rationale=license_rationale,
                )
            )
            previous_coverage = cumulative
            used_ids.add(candidate.candidate_id)
        return tuple(selected)

    def _coverage_report(
        self,
        request: SourceSelectionRequest,
        *,
        selected_set: SelectedSourceSet,
        projections: dict[str, tuple[SelectedCoverageClaim, ...]],
        created_at: datetime,
    ) -> CoverageReport:
        selected_claims = tuple(
            (source, claim) for source in selected_set.sources for claim in source.coverage_claims
        )
        blocking_gate_fields = {
            field_name
            for gate in request.contract.quality_gates
            if gate.blocking
            for field_name in gate.fields
        }
        fields = tuple(
            _field_coverage(field.name, field.requirement, selected_claims, blocking_gate_fields)
            for field in request.contract.fields
        )
        fields_by_name = {item.field_name: item for item in fields}
        cells = tuple(
            _cell_coverage(cell, selected_claims, projections)
            for cell in request.search_plan.coverage_matrix.cells
        )
        gates = tuple(
            _gate_coverage(gate, fields_by_name) for gate in request.contract.quality_gates
        )
        scopes = tuple(
            ScopeCoverage(
                constraint_id=constraint.constraint_id,
                kind=constraint.kind,
                state=ScopeCoverageState.UNVERIFIED,
                detail=(
                    f"Constraint {constraint.constraint_id} requires record-level evidence after "
                    "parsing; discovery metadata is insufficient."
                ),
            )
            for constraint in request.contract.selection_constraints
        )
        source_types = tuple(
            _source_type_coverage(source_type, selected_claims)
            for source_type in request.contract.acceptable_source_types
        )
        required = tuple(item for item in fields if item.requirement is FieldRequirement.REQUIRED)
        required_ratio = (
            sum(item.state is CandidateCoverageState.CANDIDATE_COVERED for item in required)
            / len(required)
            if required
            else 1.0
        )
        entity_ratio = (
            sum(
                fields_by_name[name].state is CandidateCoverageState.CANDIDATE_COVERED
                for name in request.contract.entity_keys
            )
            / len(request.contract.entity_keys)
            if request.contract.entity_keys
            else 1.0
        )
        source_type_ratio = sum(
            item.state is CandidateCoverageState.CANDIDATE_COVERED for item in source_types
        ) / len(source_types)
        report = CoverageReport(
            task_id=request.contract.task_id,
            run_id=request.contract.run_id,
            contract_version=request.contract.version,
            created_at=created_at,
            producer_version=self._producer_version,
            coverage_report_id=f"cvr_{'0' * 32}",
            contract_id=request.contract.contract_id,
            contract_hash=request.contract.contract_hash,
            search_plan_hash=request.search_plan.plan_hash,
            candidate_set_hash=request.connector_result.candidate_set.candidate_set_hash,
            selected_source_set_hash=selected_set.selected_source_set_hash,
            fields=fields,
            cells=cells,
            gates=gates,
            scopes=scopes,
            source_types=source_types,
            entity_key_fields=request.contract.entity_keys,
            selected_categories=tuple(
                dict.fromkeys(item.assigned_diversity_category for item in selected_set.sources)
            ),
            required_candidate_coverage=required_ratio,
            entity_key_candidate_coverage=entity_ratio,
            source_type_candidate_coverage=source_type_ratio,
            has_primary_source=any(item.primary_source for item in selected_set.sources),
            coverage_report_hash=_ZERO_HASH,
        )
        report_hash = calculate_coverage_report_hash(report)
        return report.model_copy(
            update={
                "coverage_report_id": f"cvr_{report_hash[:32]}",
                "coverage_report_hash": report_hash,
            }
        )

    def _gap_set(
        self,
        request: SourceSelectionRequest,
        *,
        selected_set: SelectedSourceSet,
        report: CoverageReport,
        applicable: tuple[SourceCategory, ...],
        created_at: datetime,
    ) -> SearchGapSet:
        gaps: list[SelectionGap] = []
        for field in report.fields:
            if field.requirement is FieldRequirement.REQUIRED and (
                field.state is not CandidateCoverageState.CANDIDATE_COVERED
            ):
                code = (
                    SelectionGapCode.REQUIRED_FIELD_UNCERTAIN
                    if field.state is CandidateCoverageState.UNCERTAIN
                    else SelectionGapCode.REQUIRED_FIELD_UNCOVERED
                )
                gaps.append(
                    _gap(
                        code,
                        f"Required field {field.field_name} lacks a confident candidate source.",
                        blocking=True,
                        target_fields=(field.field_name,),
                        candidate_ids=field.candidate_ids,
                    )
                )
            elif (
                field.requirement is FieldRequirement.OPTIONAL
                and field.state is CandidateCoverageState.UNCOVERED
            ):
                gaps.append(
                    _gap(
                        SelectionGapCode.OPTIONAL_FIELD_UNCOVERED,
                        f"Optional field {field.field_name} has no candidate source.",
                        blocking=False,
                        target_fields=(field.field_name,),
                    )
                )
        for gate in report.gates:
            if gate.state is not GateCoverageState.CANDIDATE_SATISFIED:
                gaps.append(
                    _gap(
                        SelectionGapCode.QUALITY_GATE_UNSATISFIED,
                        f"Quality gate {gate.gate_id} lacks candidate coverage.",
                        blocking=gate.blocking,
                        target_fields=gate.missing_fields or gate.fields,
                        quality_gate_ids=(gate.gate_id,),
                    )
                )
        for scope in report.scopes:
            gaps.append(
                _gap(
                    SelectionGapCode.SCOPE_UNVERIFIED,
                    f"Selection constraint {scope.constraint_id} is unverified before parsing.",
                    blocking=True,
                    constraint_ids=(scope.constraint_id,),
                )
            )
        if request.policy.require_primary_source and not report.has_primary_source:
            gaps.append(
                _gap(
                    SelectionGapCode.PRIMARY_SOURCE_MISSING,
                    "No selected candidate is identified as a primary source.",
                    blocking=True,
                )
            )
        category_target = min(request.policy.minimum_source_categories, len(applicable))
        if len(report.selected_categories) < category_target:
            missing_categories = tuple(
                item for item in applicable if item not in report.selected_categories
            )
            gaps.append(
                _gap(
                    SelectionGapCode.SOURCE_CATEGORY_DIVERSITY,
                    "Selected candidates do not meet the applicable source-category target.",
                    blocking=True,
                    categories=missing_categories,
                )
            )
        type_target = min(
            request.policy.minimum_contract_source_types,
            len(report.source_types),
        )
        covered_type_count = sum(
            item.state is CandidateCoverageState.CANDIDATE_COVERED for item in report.source_types
        )
        if covered_type_count < type_target:
            missing_types = tuple(
                item.contract_source_type
                for item in report.source_types
                if item.state is not CandidateCoverageState.CANDIDATE_COVERED
            )
            gaps.append(
                _gap(
                    SelectionGapCode.CONTRACT_SOURCE_TYPE_MISSING,
                    "Selected candidates do not meet the contract source-type target.",
                    blocking=True,
                    contract_source_types=missing_types,
                )
            )
        for source in selected_set.sources:
            if source.license_decision is not LicenseDecision.ALLOWED:
                gaps.append(
                    _gap(
                        SelectionGapCode.LICENSE_REVIEW_REQUIRED,
                        f"Candidate {source.candidate_id} requires license review before reuse.",
                        blocking=True,
                        candidate_ids=(source.candidate_id,),
                    )
                )
        if selected_set.candidate_count == 0:
            gaps.append(
                _gap(
                    SelectionGapCode.NO_CANDIDATES,
                    "Connector execution produced no source candidates.",
                    blocking=True,
                )
            )
        targets_unmet = any(item.blocking for item in gaps)
        if (
            targets_unmet
            and len(selected_set.sources) < selected_set.candidate_count
            and selected_set.available_download_bytes
            < selected_set.reserved_download_bytes + request.policy.unknown_size_reservation_bytes
        ):
            gaps.append(
                _gap(
                    SelectionGapCode.BUDGET_EXHAUSTED,
                    "The remaining download budget cannot reserve another unknown-size source.",
                    blocking=True,
                )
            )
        directives = tuple(
            _directive(gap, request)
            for gap in gaps
            if gap.code
            not in {
                SelectionGapCode.BUDGET_EXHAUSTED,
                SelectionGapCode.LICENSE_REVIEW_REQUIRED,
            }
        )
        gap_set = SearchGapSet(
            task_id=request.contract.task_id,
            run_id=request.contract.run_id,
            contract_version=request.contract.version,
            created_at=created_at,
            producer_version=self._producer_version,
            gap_set_id=f"sgs_{'0' * 32}",
            contract_id=request.contract.contract_id,
            contract_hash=request.contract.contract_hash,
            search_plan_hash=request.search_plan.plan_hash,
            candidate_set_hash=request.connector_result.candidate_set.candidate_set_hash,
            selected_source_set_hash=selected_set.selected_source_set_hash,
            gaps=tuple(gaps),
            directives=directives,
            search_gap_set_hash=_ZERO_HASH,
        )
        gap_hash = calculate_search_gap_set_hash(gap_set)
        return gap_set.model_copy(
            update={
                "gap_set_id": f"sgs_{gap_hash[:32]}",
                "search_gap_set_hash": gap_hash,
            }
        )

    @staticmethod
    def _progress(
        request: SourceSelectionRequest,
        selected_set: SelectedSourceSet,
        report: CoverageReport,
        gap_set: SearchGapSet,
    ) -> SearchProgressSnapshot:
        current_gain = max(
            0.0,
            report.required_candidate_coverage
            - request.round_context.previous_required_field_coverage,
        )
        current_sources = max(
            0,
            len(selected_set.sources) - request.round_context.previous_selected_source_count,
        )
        target = min(
            request.policy.minimum_source_categories,
            selected_set.applicable_source_category_count,
        )
        category_coverage = min(1.0, len(report.selected_categories) / target) if target else 1.0
        return SearchProgressSnapshot(
            cancelled=request.round_context.cancelled,
            completed_rounds=request.round_context.completed_rounds,
            consumed_cost_micro_usd=request.round_context.consumed_cost_micro_usd,
            elapsed_seconds=request.round_context.elapsed_seconds,
            downloaded_bytes=request.round_context.downloaded_bytes,
            model_tokens=request.round_context.model_tokens,
            required_field_coverage=report.required_candidate_coverage,
            source_category_coverage=category_coverage,
            has_primary_source=report.has_primary_source,
            critical_gap_count=sum(item.blocking for item in gap_set.gaps),
            recent_marginal_gains=(
                *request.round_context.prior_marginal_gains,
                current_gain,
            ),
            recent_new_source_counts=(
                *request.round_context.prior_new_source_counts,
                current_sources,
            ),
        )

    @staticmethod
    def _metrics(
        selected_set: SelectedSourceSet,
        report: CoverageReport,
        gap_set: SearchGapSet,
        should_stop: bool,
    ) -> SourceSelectionMetrics:
        required = tuple(
            item for item in report.fields if item.requirement is FieldRequirement.REQUIRED
        )
        return SourceSelectionMetrics(
            candidate_count=selected_set.candidate_count,
            selected_source_count=len(selected_set.sources),
            duplicate_replica_count=selected_set.duplicate_replica_count,
            required_field_count=len(required),
            candidate_covered_required_field_count=sum(
                item.state is CandidateCoverageState.CANDIDATE_COVERED for item in required
            ),
            uncertain_required_field_count=sum(
                item.state is CandidateCoverageState.UNCERTAIN for item in required
            ),
            uncovered_required_field_count=sum(
                item.state is CandidateCoverageState.UNCOVERED for item in required
            ),
            applicable_source_type_count=len(report.source_types),
            covered_source_type_count=sum(
                item.state is CandidateCoverageState.CANDIDATE_COVERED
                for item in report.source_types
            ),
            selected_source_category_count=len(report.selected_categories),
            primary_source_selected=report.has_primary_source,
            gap_count=len(gap_set.gaps),
            blocking_gap_count=sum(item.blocking for item in gap_set.gaps),
            reserved_download_bytes=selected_set.reserved_download_bytes,
            continue_search=not should_stop,
        )

    @staticmethod
    def _status(
        selected_set: SelectedSourceSet,
        metrics: SourceSelectionMetrics,
    ) -> SourceSelectionStatus:
        if selected_set.candidate_count == 0:
            return SourceSelectionStatus.UNSUPPORTED
        if not selected_set.sources:
            return SourceSelectionStatus.NEEDS_REVIEW
        if metrics.blocking_gap_count:
            return SourceSelectionStatus.PARTIAL
        return SourceSelectionStatus.SUCCEEDED


def _representative_candidates(
    candidates: Sequence[SourceCandidate],
    projections: dict[str, tuple[SelectedCoverageClaim, ...]],
    request: SourceSelectionRequest,
) -> tuple[SourceCandidate, ...]:
    by_group: dict[str, list[SourceCandidate]] = {}
    required = {
        field.name
        for field in request.contract.fields
        if field.requirement is FieldRequirement.REQUIRED
    }
    for candidate in candidates:
        by_group.setdefault(candidate.replica_group_key, []).append(candidate)

    def score(candidate: SourceCandidate) -> tuple[object, ...]:
        claims = projections[candidate.candidate_id]
        covered_required = sum(
            claim.field_name in required and claim.state is CandidateCoverageState.CANDIDATE_COVERED
            for claim in claims
        )
        return (
            -covered_required,
            -len(claims),
            -int(candidate.primary_source),
            -candidate.assessment.total_score,
            candidate.candidate_id,
        )

    return tuple(min(group, key=score) for group in by_group.values())


def _selection_reasons(
    candidate: SourceCandidate,
    *,
    assigned: SourceCategory,
    required_gain: tuple[str, ...],
    optional_gain: tuple[str, ...],
    type_gain: tuple[str, ...],
    category_gain: bool,
    primary_gain: bool,
) -> tuple[SelectionReason, ...]:
    entries: list[tuple[SelectionReasonCode, str, tuple[str, ...], tuple[str, ...]]] = []
    if required_gain:
        entries.append(
            (
                SelectionReasonCode.REQUIRED_FIELD_GAIN,
                "Adds candidate coverage for required contract fields.",
                required_gain,
                (),
            )
        )
    if optional_gain:
        entries.append(
            (
                SelectionReasonCode.OPTIONAL_FIELD_GAIN,
                "Adds candidate coverage for optional contract fields.",
                optional_gain,
                (),
            )
        )
    if primary_gain:
        entries.append(
            (
                SelectionReasonCode.PRIMARY_SOURCE,
                "Retains an observed primary-source candidate.",
                (),
                (),
            )
        )
    if category_gain:
        entries.append(
            (
                SelectionReasonCode.SOURCE_CATEGORY_DIVERSITY,
                f"Adds the {assigned.value} source category without counting replicas twice.",
                (),
                (),
            )
        )
    if type_gain:
        entries.append(
            (
                SelectionReasonCode.CONTRACT_SOURCE_TYPE_COVERAGE,
                "Adds candidate coverage for contract source types.",
                (),
                type_gain,
            )
        )
    if not entries:
        entries.append(
            (
                SelectionReasonCode.SOURCE_QUALITY,
                "Retains the strongest evidence-backed candidate for an unmet target.",
                (),
                (),
            )
        )
    return tuple(
        SelectionReason(
            reason_id=_stable_id(
                "srn",
                (candidate.candidate_id, code.value, fields, source_types),
                length=16,
            ),
            code=code,
            detail=detail,
            target_fields=fields,
            contract_source_types=source_types,
        )
        for code, detail, fields, source_types in entries
    )


def _field_coverage(
    field_name: str,
    requirement: FieldRequirement,
    selected_claims: tuple[tuple[SelectedSource, SelectedCoverageClaim], ...],
    blocking_gate_fields: set[str],
) -> FieldCoverage:
    claims = tuple(
        (source, claim) for source, claim in selected_claims if claim.field_name == field_name
    )
    state = _aggregate_state(claim for _, claim in claims)
    return FieldCoverage(
        field_name=field_name,
        requirement=requirement,
        critical=(requirement is FieldRequirement.REQUIRED or field_name in blocking_gate_fields),
        state=state,
        maximum_confidence=max((claim.confidence for _, claim in claims), default=0.0),
        candidate_ids=_unique(source.candidate_id for source, _ in claims),
        evidence_ids=_unique(
            evidence_id for _, claim in claims for evidence_id in claim.evidence_ids
        ),
        contract_source_types=_unique(
            source_type for _, claim in claims for source_type in claim.contract_source_types
        ),
        source_ids=_unique(source_id for _, claim in claims for source_id in claim.source_ids),
    )


def _cell_coverage(
    cell: CoverageCell,
    selected_claims: tuple[tuple[SelectedSource, SelectedCoverageClaim], ...],
    projections: dict[str, tuple[SelectedCoverageClaim, ...]],
) -> CoverageCellObservation:
    field_name = cell.field_name
    source_type = cell.contract_source_type
    claims = tuple(
        (source, claim)
        for source, claim in selected_claims
        if claim.field_name == field_name and source_type in claim.contract_source_types
    )
    return CoverageCellObservation(
        cell_id=cell.cell_id,
        field_name=field_name,
        contract_source_type=source_type,
        available_candidate_count=sum(
            any(
                claim.field_name == field_name and source_type in claim.contract_source_types
                for claim in candidate_claims_projection
            )
            for candidate_claims_projection in projections.values()
        ),
        selected_candidate_count=len(_unique(source.candidate_id for source, _ in claims)),
        selected_candidate_ids=_unique(source.candidate_id for source, _ in claims),
        evidence_ids=_unique(
            evidence_id for _, claim in claims for evidence_id in claim.evidence_ids
        ),
        state=_aggregate_state(claim for _, claim in claims),
    )


def _gate_coverage(
    gate: QualityGate,
    fields_by_name: dict[str, FieldCoverage],
) -> GateCoverage:
    covered = tuple(
        field_name
        for field_name in gate.fields
        if fields_by_name[field_name].state is CandidateCoverageState.CANDIDATE_COVERED
    )
    ratio = len(covered) / len(gate.fields)
    if gate.kind is QualityGateKind.ANY_OF_FIELDS:
        satisfied = bool(covered)
    else:
        satisfied = ratio >= gate.threshold
    state = (
        GateCoverageState.CANDIDATE_SATISFIED
        if satisfied
        else GateCoverageState.PARTIAL
        if covered
        else GateCoverageState.UNSATISFIED
    )
    return GateCoverage(
        gate_id=gate.gate_id,
        kind=gate.kind,
        fields=gate.fields,
        covered_fields=covered,
        state=state,
        blocking=gate.blocking,
        threshold=gate.threshold,
        candidate_coverage_ratio=ratio,
        missing_fields=tuple(item for item in gate.fields if item not in covered),
    )


def _source_type_coverage(
    source_type: str,
    selected_claims: tuple[tuple[SelectedSource, SelectedCoverageClaim], ...],
) -> SourceTypeCoverage:
    claims = tuple(
        (source, claim)
        for source, claim in selected_claims
        if source_type in claim.contract_source_types
    )
    return SourceTypeCoverage(
        contract_source_type=source_type,
        state=_aggregate_state(claim for _, claim in claims),
        selected_candidate_ids=_unique(source.candidate_id for source, _ in claims),
        fields=_unique(claim.field_name for _, claim in claims),
    )


def _aggregate_state(claims: Iterable[SelectedCoverageClaim]) -> CandidateCoverageState:
    states = tuple(claim.state for claim in claims)
    if CandidateCoverageState.CANDIDATE_COVERED in states:
        return CandidateCoverageState.CANDIDATE_COVERED
    if CandidateCoverageState.UNCERTAIN in states:
        return CandidateCoverageState.UNCERTAIN
    return CandidateCoverageState.UNCOVERED


def _gap(
    code: SelectionGapCode,
    detail: str,
    *,
    blocking: bool,
    target_fields: tuple[str, ...] = (),
    contract_source_types: tuple[str, ...] = (),
    categories: tuple[SourceCategory, ...] = (),
    candidate_ids: tuple[str, ...] = (),
    quality_gate_ids: tuple[str, ...] = (),
    constraint_ids: tuple[str, ...] = (),
) -> SelectionGap:
    payload = (
        code.value,
        blocking,
        detail,
        target_fields,
        contract_source_types,
        tuple(item.value for item in categories),
        candidate_ids,
        quality_gate_ids,
        constraint_ids,
    )
    return SelectionGap(
        gap_id=_stable_id("sgp", payload, length=16),
        code=code,
        blocking=blocking,
        detail=detail,
        target_fields=target_fields,
        contract_source_types=contract_source_types,
        categories=categories,
        candidate_ids=candidate_ids,
        quality_gate_ids=quality_gate_ids,
        constraint_ids=constraint_ids,
    )


def _directive(gap: SelectionGap, request: SourceSelectionRequest) -> GapSearchDirective:
    field_preferences = _unique(
        source_type
        for field in request.contract.fields
        if field.name in gap.target_fields
        for source_type in field.source_preference
    )
    preferred_types = gap.contract_source_types or field_preferences
    priority = 1 if gap.blocking else 50
    return GapSearchDirective(
        directive_id=_stable_id("gqd", (gap.gap_id, preferred_types), length=16),
        gap_ids=(gap.gap_id,),
        target_fields=gap.target_fields,
        preferred_contract_source_types=preferred_types,
        preferred_categories=gap.categories,
        priority=priority,
        rationale="Run another evidence search focused on this explicit coverage gap.",
    )


def _stable_id(prefix: str, payload: object, *, length: int) -> str:
    return f"{prefix}_{canonical_hash(payload)[:length]}"


def _unique(values: Iterable[_T]) -> tuple[_T, ...]:
    return tuple(dict.fromkeys(values))

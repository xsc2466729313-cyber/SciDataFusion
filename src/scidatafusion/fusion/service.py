"""Idempotent M17 conflict-preserving fusion over verified entity clusters."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from concurrent.futures import Future
from threading import RLock

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.entity_resolution import EntityResolutionStatus
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.fusion import (
    Conflict,
    ConflictClass,
    ConflictSet,
    FusedField,
    FusedRecord,
    FusedRecordSet,
    FusionCandidate,
    FusionCandidateSet,
    FusionCompletedPayload,
    FusionDecision,
    FusionMetrics,
    FusionRequest,
    FusionResult,
    FusionStatus,
    GoldCandidateDataset,
    GoldFieldCandidate,
    GoldRecordCandidate,
    ResolutionDecision,
    ResolutionDecisionSet,
)
from scidatafusion.contracts.normalization import NormalizedField
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.fusion.checkpoints import FusionCheckpointStore, MemoryFusionCheckpointStore
from scidatafusion.fusion.integrity import (
    calculate_conflict_hash,
    calculate_conflict_set_hash,
    calculate_fused_field_hash,
    calculate_fused_record_hash,
    calculate_fused_record_set_hash,
    calculate_fusion_candidate_hash,
    calculate_fusion_candidate_set_hash,
    calculate_fusion_event_id,
    calculate_fusion_idempotency_key,
    calculate_fusion_input_hash,
    calculate_fusion_output_hash,
    calculate_fusion_policy_hash,
    calculate_gold_dataset_hash,
    calculate_gold_record_hash,
    calculate_resolution_decision_hash,
    calculate_resolution_decision_set_hash,
    verify_fusion_request,
    verify_fusion_result,
)
from scidatafusion.fusion.rules import candidate_comparison_hash, decide_candidates


class ConflictPreservingFusionService:
    """Retain every candidate and select only exact, evidence-backed consensus."""

    def __init__(
        self,
        *,
        bronze_store: BronzeByteStore,
        checkpoints: FusionCheckpointStore | None = None,
        producer_version: str = "1.0.0",
    ) -> None:
        self._bronze_store = bronze_store
        self._checkpoints = checkpoints or MemoryFusionCheckpointStore()
        self._producer_version = producer_version
        self._cache: dict[str, FusionResult] = {}
        self._inflight: dict[str, Future[FusionResult]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = RLock()

    async def execute(self, request: FusionRequest) -> FusionResult:
        """Verify, replay, or execute one cancellation-isolated M17 request."""

        verify_fusion_request(request, self._bronze_store)
        key = calculate_fusion_idempotency_key(request, self._producer_version)
        if not request.force_recompute:
            with self._lock:
                cached = self._cache.get(key)
            if cached is not None:
                verify_fusion_result(cached, request, self._bronze_store)
                return cached
            checkpoint = self._checkpoints.load(key)
            if checkpoint is not None:
                verify_fusion_result(checkpoint, request, self._bronze_store)
                with self._lock:
                    return self._cache.setdefault(key, checkpoint)
        with self._lock:
            pending = self._inflight.get(key)
            if pending is None:
                pending = Future()
                self._inflight[key] = pending
                self._tasks[key] = asyncio.create_task(self._produce(request, key, pending))
        return await asyncio.shield(asyncio.wrap_future(pending))

    async def _produce(
        self, request: FusionRequest, key: str, pending: Future[FusionResult]
    ) -> None:
        try:
            result = await self._execute_once(request, key)
            result = self._checkpoints.save(result)
            verify_fusion_result(result, request, self._bronze_store)
        except BaseException as exc:
            with self._lock:
                self._inflight.pop(key, None)
                self._tasks.pop(key, None)
            if not pending.done():
                pending.set_exception(exc)
            return
        with self._lock:
            existing = self._cache.setdefault(key, result)
            self._inflight.pop(key, None)
            self._tasks.pop(key, None)
        if not pending.done():
            pending.set_result(existing)

    async def _execute_once(self, request: FusionRequest, key: str) -> FusionResult:
        await asyncio.sleep(0)
        upstream = request.entity_result
        normalized_records = {
            item.normalized_record_id: item
            for item in request.entity_request.normalization_result.record_set.records
        }
        candidate_count = sum(
            len(normalized_records[record_id].fields)
            for cluster in upstream.cluster_set.clusters
            for record_id in cluster.member_record_ids
        )
        if candidate_count > request.policy.max_candidates:
            raise AppError(ErrorCode.BUDGET_EXCEEDED, "M17 candidate count exceeds policy")
        candidates: list[FusionCandidate] = []
        conflicts: list[Conflict] = []
        decisions: list[ResolutionDecision] = []
        fused_records: list[FusedRecord] = []
        gold_records: list[GoldRecordCandidate] = []
        for cluster in upstream.cluster_set.clusters:
            by_field: dict[str, list[FusionCandidate]] = defaultdict(list)
            for record_id in cluster.member_record_ids:
                record = normalized_records[record_id]
                for field in record.fields:
                    candidate = _fusion_candidate(
                        request,
                        cluster.entity_cluster_id,
                        record_id,
                        field,
                        self._producer_version,
                    )
                    candidates.append(candidate)
                    by_field[field.field_name].append(candidate)
            fused_fields: list[FusedField] = []
            gold_fields: list[GoldFieldCandidate] = []
            withheld: list[str] = []
            for field_name in sorted(by_field):
                field_candidates = tuple(
                    sorted(by_field[field_name], key=lambda item: item.fusion_candidate_id)
                )
                decision_kind, selected, conflicted = decide_candidates(field_candidates)
                conflict = (
                    _conflict(
                        request,
                        cluster.entity_cluster_id,
                        field_name,
                        field_candidates,
                        self._producer_version,
                    )
                    if conflicted
                    else None
                )
                if conflict is not None:
                    conflicts.append(conflict)
                    if len(conflicts) > request.policy.max_conflicts:
                        raise AppError(
                            ErrorCode.BUDGET_EXCEEDED, "M17 conflict count exceeds policy"
                        )
                decision = _resolution_decision(
                    request,
                    cluster.entity_cluster_id,
                    field_name,
                    field_candidates,
                    decision_kind,
                    selected,
                    conflict,
                    self._producer_version,
                )
                decisions.append(decision)
                fused = _fused_field(
                    request,
                    cluster.entity_cluster_id,
                    field_name,
                    field_candidates,
                    decision,
                    selected,
                    conflict,
                    self._producer_version,
                )
                fused_fields.append(fused)
                if selected is None:
                    withheld.append(field_name)
                else:
                    gold_fields.append(_gold_field(fused, decision, selected, field_candidates))
            fused_record = _fused_record(
                request,
                cluster.entity_cluster_id,
                cluster.member_record_ids,
                tuple(fused_fields),
                self._producer_version,
            )
            fused_records.append(fused_record)
            gold_records.append(
                _gold_record(
                    request,
                    cluster.entity_cluster_id,
                    fused_record,
                    tuple(gold_fields),
                    tuple(withheld),
                    self._producer_version,
                )
            )
        return _aggregate(
            request,
            key,
            tuple(candidates),
            tuple(fused_records),
            tuple(conflicts),
            tuple(decisions),
            tuple(gold_records),
            self._producer_version,
        )


def _metadata(request: FusionRequest, producer_version: str) -> dict[str, object]:
    return {
        "task_id": request.entity_result.task_id,
        "run_id": request.entity_result.run_id,
        "contract_version": request.entity_result.contract_version,
        "created_at": request.runtime.checked_at,
        "producer_version": producer_version,
    }


def _fusion_candidate(
    request: FusionRequest,
    cluster_id: str,
    record_id: str,
    field: NormalizedField,
    producer_version: str,
) -> FusionCandidate:
    draft = FusionCandidate.model_validate(
        {
            **_metadata(request, producer_version),
            "fusion_candidate_id": "fca_" + "0" * 32,
            "entity_cluster_id": cluster_id,
            "normalized_record_id": record_id,
            "normalized_field_id": field.normalized_field_id,
            "normalized_field_hash": field.normalized_field_hash,
            "field_name": field.field_name,
            "raw_value": field.raw_value,
            "raw_value_sha256": field.raw_value_sha256,
            "normalized_value": field.normalized_value,
            "normalized_value_sha256": field.normalized_value_sha256,
            "evidence_ids": field.evidence_ids,
            "upstream_issue_count": len(field.issue_ids),
            "eligible_for_gold": field.eligible_for_m16,
            "candidate_hash": "0" * 64,
        }
    )
    value = calculate_fusion_candidate_hash(draft)
    return draft.model_copy(
        update={"fusion_candidate_id": f"fca_{value[:32]}", "candidate_hash": value}
    )


def _conflict(
    request: FusionRequest,
    cluster_id: str,
    field_name: str,
    candidates: tuple[FusionCandidate, ...],
    producer_version: str,
) -> Conflict:
    draft = Conflict.model_validate(
        {
            **_metadata(request, producer_version),
            "conflict_id": "cfl_" + "0" * 32,
            "entity_cluster_id": cluster_id,
            "field_name": field_name,
            "candidate_ids": tuple(item.fusion_candidate_id for item in candidates),
            "candidate_value_hashes": tuple(candidate_comparison_hash(item) for item in candidates),
            "classification": ConflictClass.UNRESOLVED,
            "reason": "distinct_candidate_values_without_registered_reconciliation_rule",
            "conflict_hash": "0" * 64,
        }
    )
    value = calculate_conflict_hash(draft)
    return draft.model_copy(update={"conflict_id": f"cfl_{value[:32]}", "conflict_hash": value})


def _resolution_decision(
    request: FusionRequest,
    cluster_id: str,
    field_name: str,
    candidates: tuple[FusionCandidate, ...],
    decision: FusionDecision,
    selected: FusionCandidate | None,
    conflict: Conflict | None,
    producer_version: str,
) -> ResolutionDecision:
    draft = ResolutionDecision.model_validate(
        {
            **_metadata(request, producer_version),
            "decision_id": "fdr_" + "0" * 32,
            "entity_cluster_id": cluster_id,
            "field_name": field_name,
            "candidate_ids": tuple(item.fusion_candidate_id for item in candidates),
            "decision": decision,
            "selected_candidate_id": selected.fusion_candidate_id if selected else None,
            "conflict_id": conflict.conflict_id if conflict else None,
            "rule_id": request.runtime.rule.rule_id,
            "confidence": 1.0 if selected else 0.0,
            "decision_hash": "0" * 64,
        }
    )
    value = calculate_resolution_decision_hash(draft)
    return draft.model_copy(update={"decision_id": f"fdr_{value[:32]}", "decision_hash": value})


def _fused_field(
    request: FusionRequest,
    cluster_id: str,
    field_name: str,
    candidates: tuple[FusionCandidate, ...],
    decision: ResolutionDecision,
    selected: FusionCandidate | None,
    conflict: Conflict | None,
    producer_version: str,
) -> FusedField:
    draft = FusedField.model_validate(
        {
            **_metadata(request, producer_version),
            "fused_field_id": "ffd_" + "0" * 32,
            "entity_cluster_id": cluster_id,
            "field_name": field_name,
            "candidate_ids": tuple(item.fusion_candidate_id for item in candidates),
            "decision_id": decision.decision_id,
            "conflict_id": conflict.conflict_id if conflict else None,
            "selected_candidate_id": selected.fusion_candidate_id if selected else None,
            "selected_value": selected.normalized_value if selected else None,
            "selected_value_sha256": selected.normalized_value_sha256 if selected else None,
            "fused_field_hash": "0" * 64,
        }
    )
    value = calculate_fused_field_hash(draft)
    return draft.model_copy(
        update={"fused_field_id": f"ffd_{value[:32]}", "fused_field_hash": value}
    )


def _fused_record(
    request: FusionRequest,
    cluster_id: str,
    member_record_ids: tuple[str, ...],
    fields: tuple[FusedField, ...],
    producer_version: str,
) -> FusedRecord:
    draft = FusedRecord.model_validate(
        {
            **_metadata(request, producer_version),
            "fused_record_id": "frc_" + "0" * 32,
            "entity_cluster_id": cluster_id,
            "member_record_ids": member_record_ids,
            "fields": fields,
            "fused_record_hash": "0" * 64,
        }
    )
    value = calculate_fused_record_hash(draft)
    return draft.model_copy(
        update={"fused_record_id": f"frc_{value[:32]}", "fused_record_hash": value}
    )


def _gold_field(
    fused: FusedField,
    decision: ResolutionDecision,
    selected: FusionCandidate,
    candidates: tuple[FusionCandidate, ...],
) -> GoldFieldCandidate:
    if selected.normalized_value is None or selected.normalized_value_sha256 is None:
        raise AppError(
            ErrorCode.ARTIFACT_INTEGRITY_ERROR,
            "M17 selected candidate is missing its normalized value",
        )
    return GoldFieldCandidate(
        field_name=fused.field_name,
        fused_field_id=fused.fused_field_id,
        decision_id=decision.decision_id,
        selected_candidate_id=selected.fusion_candidate_id,
        all_candidate_ids=tuple(item.fusion_candidate_id for item in candidates),
        value=selected.normalized_value,
        value_sha256=selected.normalized_value_sha256,
        evidence_ids=tuple(
            dict.fromkeys(evidence_id for item in candidates for evidence_id in item.evidence_ids)
        ),
    )


def _gold_record(
    request: FusionRequest,
    cluster_id: str,
    fused_record: FusedRecord,
    fields: tuple[GoldFieldCandidate, ...],
    withheld: tuple[str, ...],
    producer_version: str,
) -> GoldRecordCandidate:
    draft = GoldRecordCandidate.model_validate(
        {
            **_metadata(request, producer_version),
            "gold_record_id": "gcr_" + "0" * 32,
            "entity_cluster_id": cluster_id,
            "fused_record_id": fused_record.fused_record_id,
            "fields": fields,
            "withheld_field_names": withheld,
            "gold_record_hash": "0" * 64,
        }
    )
    value = calculate_gold_record_hash(draft)
    return draft.model_copy(
        update={"gold_record_id": f"gcr_{value[:32]}", "gold_record_hash": value}
    )


def _aggregate(
    request: FusionRequest,
    key: str,
    candidates: tuple[FusionCandidate, ...],
    fused_records: tuple[FusedRecord, ...],
    conflicts: tuple[Conflict, ...],
    decisions: tuple[ResolutionDecision, ...],
    gold_records: tuple[GoldRecordCandidate, ...],
    producer_version: str,
) -> FusionResult:
    upstream = request.entity_result
    metadata = _metadata(request, producer_version)
    candidate_set_draft = FusionCandidateSet.model_validate(
        {
            **metadata,
            "candidate_set_id": "fcs_" + "0" * 32,
            "candidates": candidates,
            "candidate_set_hash": "0" * 64,
        }
    )
    candidate_set_hash = calculate_fusion_candidate_set_hash(candidate_set_draft)
    candidate_set = candidate_set_draft.model_copy(
        update={
            "candidate_set_id": f"fcs_{candidate_set_hash[:32]}",
            "candidate_set_hash": candidate_set_hash,
        }
    )
    fused_set_draft = FusedRecordSet.model_validate(
        {
            **metadata,
            "fused_record_set_id": "frs_" + "0" * 32,
            "contract_id": upstream.contract_id,
            "contract_hash": upstream.contract_hash,
            "upstream_cluster_set_hash": upstream.cluster_set.cluster_set_hash,
            "records": fused_records,
            "fused_record_set_hash": "0" * 64,
        }
    )
    fused_set_hash = calculate_fused_record_set_hash(fused_set_draft)
    fused_set = fused_set_draft.model_copy(
        update={
            "fused_record_set_id": f"frs_{fused_set_hash[:32]}",
            "fused_record_set_hash": fused_set_hash,
        }
    )
    conflict_set_draft = ConflictSet.model_validate(
        {
            **metadata,
            "conflict_set_id": "cfs_" + "0" * 32,
            "conflicts": conflicts,
            "conflict_set_hash": "0" * 64,
        }
    )
    conflict_set_hash = calculate_conflict_set_hash(conflict_set_draft)
    conflict_set = conflict_set_draft.model_copy(
        update={
            "conflict_set_id": f"cfs_{conflict_set_hash[:32]}",
            "conflict_set_hash": conflict_set_hash,
        }
    )
    decision_set_draft = ResolutionDecisionSet.model_validate(
        {
            **metadata,
            "decision_set_id": "fds_" + "0" * 32,
            "decisions": decisions,
            "decision_set_hash": "0" * 64,
        }
    )
    decision_set_hash = calculate_resolution_decision_set_hash(decision_set_draft)
    decision_set = decision_set_draft.model_copy(
        update={
            "decision_set_id": f"fds_{decision_set_hash[:32]}",
            "decision_set_hash": decision_set_hash,
        }
    )
    gold_draft = GoldCandidateDataset.model_validate(
        {
            **metadata,
            "dataset_id": "gds_" + "0" * 32,
            "contract_id": upstream.contract_id,
            "contract_hash": upstream.contract_hash,
            "records": gold_records,
            "dataset_hash": "0" * 64,
        }
    )
    gold_hash = calculate_gold_dataset_hash(gold_draft)
    gold = gold_draft.model_copy(
        update={"dataset_id": f"gds_{gold_hash[:32]}", "dataset_hash": gold_hash}
    )
    fields = tuple(field for record in fused_records for field in record.fields)
    selected = sum(field.selected_candidate_id is not None for field in fields)
    withheld = len(fields) - selected
    exact_consensus = sum(item.decision is FusionDecision.EXACT_CONSENSUS for item in decisions)
    metrics = FusionMetrics(
        input_cluster_count=len(upstream.cluster_set.clusters),
        input_record_count=sum(
            len(item.member_record_ids) for item in upstream.cluster_set.clusters
        ),
        candidate_count=len(candidates),
        fused_record_count=len(fused_records),
        fused_field_count=len(fields),
        selected_field_count=selected,
        withheld_field_count=withheld,
        exact_consensus_field_count=exact_consensus,
        conflict_count=len(conflicts),
        unresolved_conflict_count=len(conflicts),
        gold_evidence_coverage=1.0
        if all(field.evidence_ids for record in gold_records for field in record.fields)
        else 0.0,
    )
    status = (
        FusionStatus.UNSUPPORTED
        if not upstream.cluster_set.clusters
        else FusionStatus.NEEDS_REVIEW
        if not selected
        else FusionStatus.PARTIAL
        if withheld or conflicts or upstream.status is not EntityResolutionStatus.SUCCEEDED
        else FusionStatus.SUCCEEDED
    )
    warnings = tuple(
        item
        for item in (
            f"upstream_entity_status:{upstream.status.value}"
            if upstream.status is not EntityResolutionStatus.SUCCEEDED
            else "",
            f"withheld_field_count:{withheld}" if withheld else "",
            f"unresolved_conflict_count:{len(conflicts)}" if conflicts else "",
        )
        if item
    )
    input_hash = calculate_fusion_input_hash(request)
    payload = FusionCompletedPayload(
        status=status,
        contract_id=upstream.contract_id,
        upstream_cluster_set_hash=upstream.cluster_set.cluster_set_hash,
        candidate_set_hash=candidate_set.candidate_set_hash,
        fused_record_set_hash=fused_set.fused_record_set_hash,
        conflict_set_hash=conflict_set.conflict_set_hash,
        decision_set_hash=decision_set.decision_set_hash,
        gold_dataset_hash=gold.dataset_hash,
        candidate_count=len(candidates),
        conflict_count=len(conflicts),
        selected_field_count=selected,
        input_hash=input_hash,
        output_hash="0" * 64,
        idempotency_key=key,
    )
    event = EventEnvelope[FusionCompletedPayload](
        event_id=calculate_fusion_event_id(key),
        event_type=EventType.FUSION_COMPLETED,
        task_id=upstream.task_id,
        run_id=upstream.run_id,
        occurred_at=request.runtime.checked_at,
        producer=ProducerRef(
            component="conflict-preserving-fusion-service", version=producer_version
        ),
        payload=payload,
        correlation_id=upstream.task_id,
        causation_event_id=upstream.event.event_id,
    )
    result_draft = FusionResult.model_validate(
        {
            **metadata,
            "status": status,
            "contract_id": upstream.contract_id,
            "contract_hash": upstream.contract_hash,
            "upstream_entity_input_hash": upstream.input_hash,
            "upstream_entity_output_hash": upstream.output_hash,
            "policy": request.policy,
            "policy_hash": calculate_fusion_policy_hash(request.policy),
            "runtime": request.runtime,
            "input_hash": input_hash,
            "output_hash": "0" * 64,
            "idempotency_key": key,
            "candidate_set": candidate_set,
            "fused_record_set": fused_set,
            "conflict_set": conflict_set,
            "decision_set": decision_set,
            "gold_dataset": gold,
            "warnings": warnings,
            "metrics": metrics,
            "event": event,
        }
    )
    output_hash = calculate_fusion_output_hash(result_draft)
    return result_draft.model_copy(
        update={
            "output_hash": output_hash,
            "event": event.model_copy(
                update={"payload": payload.model_copy(update={"output_hash": output_hash})}
            ),
        }
    )

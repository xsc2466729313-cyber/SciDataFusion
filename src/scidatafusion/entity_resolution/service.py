"""Idempotent, conservative M16 entity resolution over verified normalized records."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from concurrent.futures import Future
from threading import RLock

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.entity_resolution import (
    ClusterDecision,
    DuplicateGroup,
    DuplicateGroupSet,
    DuplicateMethod,
    EntityCluster,
    EntityClusterSet,
    EntityResolutionEvidence,
    EntityResolutionEvidenceSet,
    EntityResolutionMetrics,
    EntityResolutionRequest,
    EntityResolutionResult,
    EntityResolutionStatus,
    EntityResolvedPayload,
    ResolutionMethod,
)
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.normalization import NormalizationStatus, NormalizedRecord
from scidatafusion.entity_resolution.checkpoints import (
    EntityResolutionCheckpointStore,
    MemoryEntityResolutionCheckpointStore,
)
from scidatafusion.entity_resolution.integrity import (
    calculate_duplicate_group_hash,
    calculate_duplicate_group_set_hash,
    calculate_entity_cluster_hash,
    calculate_entity_cluster_set_hash,
    calculate_entity_event_id,
    calculate_entity_idempotency_key,
    calculate_entity_input_hash,
    calculate_entity_output_hash,
    calculate_entity_policy_hash,
    calculate_resolution_evidence_hash,
    calculate_resolution_evidence_set_hash,
    verify_entity_request,
    verify_entity_result,
)
from scidatafusion.entity_resolution.rules import (
    entity_fingerprint,
    entity_key_fields,
    exact_record_fingerprint,
)
from scidatafusion.errors import AppError, ErrorCode


class EntityResolutionService:
    """Resolve exact stable identifiers without fuzzy or model-driven auto-merges."""

    def __init__(
        self,
        *,
        bronze_store: BronzeByteStore,
        checkpoints: EntityResolutionCheckpointStore | None = None,
        producer_version: str = "1.0.0",
    ) -> None:
        self._bronze_store = bronze_store
        self._checkpoints = checkpoints or MemoryEntityResolutionCheckpointStore()
        self._producer_version = producer_version
        self._cache: dict[str, EntityResolutionResult] = {}
        self._inflight: dict[str, Future[EntityResolutionResult]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = RLock()

    async def execute(self, request: EntityResolutionRequest) -> EntityResolutionResult:
        """Verify, replay, or execute one cancellation-isolated M16 request."""

        verify_entity_request(request, self._bronze_store)
        key = calculate_entity_idempotency_key(request, self._producer_version)
        if not request.force_recompute:
            with self._lock:
                cached = self._cache.get(key)
            if cached is not None:
                verify_entity_result(cached, request, self._bronze_store)
                return cached
            checkpoint = self._checkpoints.load(key)
            if checkpoint is not None:
                verify_entity_result(checkpoint, request, self._bronze_store)
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
        self,
        request: EntityResolutionRequest,
        key: str,
        pending: Future[EntityResolutionResult],
    ) -> None:
        try:
            result = await self._execute_once(request, key)
            result = self._checkpoints.save(result)
            verify_entity_result(result, request, self._bronze_store)
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

    async def _execute_once(
        self, request: EntityResolutionRequest, key: str
    ) -> EntityResolutionResult:
        await asyncio.sleep(0)
        records = request.normalization_result.record_set.records
        if len(records) > request.policy.max_records:
            raise AppError(ErrorCode.BUDGET_EXCEEDED, "M16 record count exceeds policy")
        required_keys = (
            request.normalization_request.mapping_request.extraction_request.contract.entity_keys
        )
        evidence_records: list[EntityResolutionEvidence] = []
        unresolved: list[str] = []
        record_buckets: dict[str, list[NormalizedRecord]] = defaultdict(list)
        evidence_by_record: dict[str, EntityResolutionEvidence] = {}
        for record in records:
            selected = entity_key_fields(record, required_keys)
            if selected is None:
                unresolved.append(record.normalized_record_id)
                continue
            fingerprint = entity_fingerprint(selected)
            evidence = _resolution_evidence(
                request,
                record,
                selected,
                fingerprint,
                self._producer_version,
            )
            evidence_records.append(evidence)
            evidence_by_record[record.normalized_record_id] = evidence
            record_buckets[fingerprint].append(record)
        candidate_pairs = sum(
            len(bucket) * (len(bucket) - 1) // 2 for bucket in record_buckets.values()
        )
        if candidate_pairs > request.policy.max_candidate_pairs:
            raise AppError(ErrorCode.BUDGET_EXCEEDED, "M16 candidate-pair count exceeds policy")
        clusters: list[EntityCluster] = []
        duplicate_groups: list[DuplicateGroup] = []
        for fingerprint in sorted(record_buckets):
            bucket = tuple(
                sorted(record_buckets[fingerprint], key=lambda item: item.normalized_record_id)
            )
            cluster = _entity_cluster(
                request,
                fingerprint,
                bucket,
                evidence_by_record,
                self._producer_version,
            )
            clusters.append(cluster)
            duplicates: dict[str, list[NormalizedRecord]] = defaultdict(list)
            for record in bucket:
                duplicates[exact_record_fingerprint(record)].append(record)
            for record_fingerprint in sorted(duplicates):
                duplicate_records = tuple(
                    sorted(
                        duplicates[record_fingerprint],
                        key=lambda item: item.normalized_record_id,
                    )
                )
                if len(duplicate_records) > 1:
                    duplicate_groups.append(
                        _duplicate_group(
                            request,
                            cluster,
                            record_fingerprint,
                            duplicate_records,
                            self._producer_version,
                        )
                    )
        return _aggregate(
            request,
            key,
            tuple(evidence_records),
            tuple(clusters),
            tuple(duplicate_groups),
            tuple(unresolved),
            self._producer_version,
        )


def _resolution_evidence(
    request: EntityResolutionRequest,
    record: NormalizedRecord,
    selected: tuple[tuple[str, str, str], ...],
    fingerprint: str,
    producer_version: str,
) -> EntityResolutionEvidence:
    fields = {item.normalized_field_id: item for item in record.fields}
    field_ids = tuple(item[1] for item in selected)
    evidence_ids = tuple(
        dict.fromkeys(
            evidence_id
            for field_id in field_ids
            for evidence_id in (
                *fields[field_id].evidence_ids,
                *fields[field_id].entity_evidence_ids,
            )
        )
    )
    draft = EntityResolutionEvidence(
        task_id=request.normalization_result.task_id,
        run_id=request.normalization_result.run_id,
        contract_version=request.normalization_result.contract_version,
        created_at=request.runtime.checked_at,
        producer_version=producer_version,
        resolution_evidence_id="ere_" + "0" * 32,
        normalized_record_id=record.normalized_record_id,
        normalized_record_hash=record.record_hash,
        entity_key_fields=tuple(item[0] for item in selected),
        entity_key_field_ids=field_ids,
        entity_key_value_hashes=tuple(item[2] for item in selected),
        entity_key_fingerprint=fingerprint,
        evidence_ids=evidence_ids,
        method=ResolutionMethod.EXACT_STABLE_IDENTIFIER,
        resolution_evidence_hash="0" * 64,
    )
    value = calculate_resolution_evidence_hash(draft)
    return draft.model_copy(
        update={
            "resolution_evidence_id": f"ere_{value[:32]}",
            "resolution_evidence_hash": value,
        }
    )


def _entity_cluster(
    request: EntityResolutionRequest,
    fingerprint: str,
    records: tuple[NormalizedRecord, ...],
    evidence_by_record: dict[str, EntityResolutionEvidence],
    producer_version: str,
) -> EntityCluster:
    decision = ClusterDecision.SINGLETON if len(records) == 1 else ClusterDecision.AUTO_MERGED
    draft = EntityCluster(
        task_id=request.normalization_result.task_id,
        run_id=request.normalization_result.run_id,
        contract_version=request.normalization_result.contract_version,
        created_at=request.runtime.checked_at,
        producer_version=producer_version,
        entity_cluster_id="ecl_" + "0" * 32,
        entity_key_fingerprint=fingerprint,
        member_record_ids=tuple(item.normalized_record_id for item in records),
        member_record_hashes=tuple(item.record_hash for item in records),
        resolution_evidence_ids=tuple(
            evidence_by_record[item.normalized_record_id].resolution_evidence_id for item in records
        ),
        decision=decision,
        automatic_merge=decision is ClusterDecision.AUTO_MERGED,
        cluster_hash="0" * 64,
    )
    value = calculate_entity_cluster_hash(draft)
    return draft.model_copy(
        update={"entity_cluster_id": f"ecl_{value[:32]}", "cluster_hash": value}
    )


def _duplicate_group(
    request: EntityResolutionRequest,
    cluster: EntityCluster,
    fingerprint: str,
    records: tuple[NormalizedRecord, ...],
    producer_version: str,
) -> DuplicateGroup:
    draft = DuplicateGroup(
        task_id=request.normalization_result.task_id,
        run_id=request.normalization_result.run_id,
        contract_version=request.normalization_result.contract_version,
        created_at=request.runtime.checked_at,
        producer_version=producer_version,
        duplicate_group_id="dpg_" + "0" * 32,
        entity_cluster_id=cluster.entity_cluster_id,
        exact_record_fingerprint=fingerprint,
        member_record_ids=tuple(item.normalized_record_id for item in records),
        method=DuplicateMethod.EXACT_ELIGIBLE_FIELD_FINGERPRINT,
        duplicate_group_hash="0" * 64,
    )
    value = calculate_duplicate_group_hash(draft)
    return draft.model_copy(
        update={"duplicate_group_id": f"dpg_{value[:32]}", "duplicate_group_hash": value}
    )


def _aggregate(
    request: EntityResolutionRequest,
    key: str,
    evidence: tuple[EntityResolutionEvidence, ...],
    clusters: tuple[EntityCluster, ...],
    duplicates: tuple[DuplicateGroup, ...],
    unresolved: tuple[str, ...],
    producer_version: str,
) -> EntityResolutionResult:
    upstream = request.normalization_result
    metadata = {
        "task_id": upstream.task_id,
        "run_id": upstream.run_id,
        "contract_version": upstream.contract_version,
        "created_at": request.runtime.checked_at,
        "producer_version": producer_version,
    }
    evidence_set_draft = EntityResolutionEvidenceSet.model_validate(
        {
            **metadata,
            "evidence_set_id": "ers_" + "0" * 32,
            "records": evidence,
            "evidence_set_hash": "0" * 64,
        }
    )
    evidence_set_hash = calculate_resolution_evidence_set_hash(evidence_set_draft)
    evidence_set = evidence_set_draft.model_copy(
        update={
            "evidence_set_id": f"ers_{evidence_set_hash[:32]}",
            "evidence_set_hash": evidence_set_hash,
        }
    )
    cluster_set_draft = EntityClusterSet.model_validate(
        {
            **metadata,
            "cluster_set_id": "ecs_" + "0" * 32,
            "contract_id": upstream.contract_id,
            "contract_hash": upstream.contract_hash,
            "upstream_normalization_output_hash": upstream.output_hash,
            "clusters": clusters,
            "cluster_set_hash": "0" * 64,
        }
    )
    cluster_set_hash = calculate_entity_cluster_set_hash(cluster_set_draft)
    cluster_set = cluster_set_draft.model_copy(
        update={
            "cluster_set_id": f"ecs_{cluster_set_hash[:32]}",
            "cluster_set_hash": cluster_set_hash,
        }
    )
    duplicate_set_draft = DuplicateGroupSet.model_validate(
        {
            **metadata,
            "duplicate_group_set_id": "dgs_" + "0" * 32,
            "groups": duplicates,
            "duplicate_group_set_hash": "0" * 64,
        }
    )
    duplicate_set_hash = calculate_duplicate_group_set_hash(duplicate_set_draft)
    duplicate_set = duplicate_set_draft.model_copy(
        update={
            "duplicate_group_set_id": f"dgs_{duplicate_set_hash[:32]}",
            "duplicate_group_set_hash": duplicate_set_hash,
        }
    )
    record_count = len(upstream.record_set.records)
    resolved_count = sum(len(item.member_record_ids) for item in clusters)
    candidate_pairs = sum(
        len(item.member_record_ids) * (len(item.member_record_ids) - 1) // 2 for item in clusters
    )
    total_pairs = record_count * (record_count - 1) // 2
    metrics = EntityResolutionMetrics(
        input_record_count=record_count,
        resolvable_record_count=resolved_count,
        unresolved_record_count=len(unresolved),
        candidate_pair_count=candidate_pairs,
        total_possible_pair_count=total_pairs,
        candidate_pair_reduction_rate=1.0
        if not total_pairs
        else 1.0 - candidate_pairs / total_pairs,
        exact_match_pair_count=candidate_pairs,
        entity_cluster_count=len(clusters),
        singleton_cluster_count=sum(
            item.decision is ClusterDecision.SINGLETON for item in clusters
        ),
        automatic_merge_cluster_count=sum(item.automatic_merge for item in clusters),
        duplicate_group_count=len(duplicates),
        m17_eligible_cluster_count=sum(item.eligible_for_m17 for item in clusters),
    )
    status = (
        EntityResolutionStatus.UNSUPPORTED
        if not record_count
        else EntityResolutionStatus.NEEDS_REVIEW
        if not resolved_count
        else EntityResolutionStatus.PARTIAL
        if unresolved or upstream.status is not NormalizationStatus.SUCCEEDED
        else EntityResolutionStatus.SUCCEEDED
    )
    input_hash = calculate_entity_input_hash(request)
    payload = EntityResolvedPayload(
        status=status,
        contract_id=upstream.contract_id,
        upstream_normalization_output_hash=upstream.output_hash,
        evidence_set_hash=evidence_set.evidence_set_hash,
        cluster_set_hash=cluster_set.cluster_set_hash,
        duplicate_group_set_hash=duplicate_set.duplicate_group_set_hash,
        record_count=record_count,
        cluster_count=len(clusters),
        duplicate_group_count=len(duplicates),
        input_hash=input_hash,
        output_hash="0" * 64,
        idempotency_key=key,
    )
    event = EventEnvelope[EntityResolvedPayload](
        event_id=calculate_entity_event_id(key),
        event_type=EventType.ENTITY_RESOLVED,
        task_id=upstream.task_id,
        run_id=upstream.run_id,
        occurred_at=request.runtime.checked_at,
        producer=ProducerRef(component="entity-resolution-service", version=producer_version),
        payload=payload,
        correlation_id=upstream.task_id,
        causation_event_id=upstream.event.event_id,
    )
    warnings = tuple(
        [
            f"upstream_normalization_status:{upstream.status.value}"
            if upstream.status is not NormalizationStatus.SUCCEEDED
            else ""
        ]
        + [f"unresolved_record:{item}" for item in unresolved]
    )
    warnings = tuple(item for item in warnings if item)
    result_draft = EntityResolutionResult.model_validate(
        {
            **metadata,
            "status": status,
            "contract_id": upstream.contract_id,
            "contract_hash": upstream.contract_hash,
            "upstream_normalization_input_hash": upstream.input_hash,
            "upstream_normalization_output_hash": upstream.output_hash,
            "policy": request.policy,
            "policy_hash": calculate_entity_policy_hash(request.policy),
            "runtime": request.runtime,
            "input_hash": input_hash,
            "output_hash": "0" * 64,
            "idempotency_key": key,
            "resolution_evidence_set": evidence_set,
            "cluster_set": cluster_set,
            "duplicate_group_set": duplicate_set,
            "unresolved_record_ids": unresolved,
            "warnings": warnings,
            "metrics": metrics,
            "event": event,
        }
    )
    output_hash = calculate_entity_output_hash(result_draft)
    return EntityResolutionResult.model_validate(
        result_draft.model_copy(
            update={
                "output_hash": output_hash,
                "event": event.model_copy(
                    update={"payload": payload.model_copy(update={"output_hash": output_hash})}
                ),
            }
        )
    )

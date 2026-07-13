"""Idempotent evidence-backed canonical mapping of M13 field candidates."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from threading import RLock

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.extraction import ExtractedFieldCandidate, ExtractionGapCode
from scidatafusion.contracts.mapping import (
    FieldMappedPayload,
    FieldMapping,
    FieldMappingSet,
    MappingDecision,
    MappingEvidence,
    MappingMethod,
    MappingMetrics,
    MappingRequest,
    MappingResult,
    MappingStatus,
    UnmappedField,
    UnmappedFieldSet,
    UnmappedReason,
)
from scidatafusion.contracts.scientific import FieldContract
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.extraction.integrity import calculate_field_contract_hash
from scidatafusion.mapping.checkpoints import MappingCheckpointStore, MemoryMappingCheckpointStore
from scidatafusion.mapping.integrity import (
    calculate_field_mapping_hash,
    calculate_mapping_event_id,
    calculate_mapping_evidence_hash,
    calculate_mapping_idempotency_key,
    calculate_mapping_input_hash,
    calculate_mapping_output_hash,
    calculate_mapping_policy_hash,
    calculate_mapping_set_hash,
    calculate_unmapped_field_hash,
    calculate_unmapped_set_hash,
    verify_mapping_request,
    verify_mapping_result,
)
from scidatafusion.mapping.rules import is_value_kind_compatible, registered_alias_suggestions


class FieldMappingService:
    """Validate exact canonical mappings without changing candidate scientific values."""

    def __init__(
        self,
        *,
        bronze_store: BronzeByteStore,
        checkpoints: MappingCheckpointStore | None = None,
        producer_version: str = "1.0.0",
    ) -> None:
        self._bronze_store = bronze_store
        self._checkpoints = checkpoints or MemoryMappingCheckpointStore()
        self._producer_version = producer_version
        self._cache: dict[str, MappingResult] = {}
        self._inflight: dict[str, Future[MappingResult]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = RLock()

    async def execute(self, request: MappingRequest) -> MappingResult:
        """Verify, replay, or execute one cancellation-isolated M14 request."""

        verify_mapping_request(request, self._bronze_store)
        key = calculate_mapping_idempotency_key(request, self._producer_version)
        if not request.force_recompute:
            with self._lock:
                cached = self._cache.get(key)
            if cached is not None:
                verify_mapping_result(cached, request, self._bronze_store)
                return cached
            checkpoint = self._checkpoints.load(key)
            if checkpoint is not None:
                verify_mapping_result(checkpoint, request, self._bronze_store)
                with self._lock:
                    return self._cache.setdefault(key, checkpoint)
        with self._lock:
            pending = self._inflight.get(key)
            if pending is None:
                pending = Future()
                self._inflight[key] = pending
                task = asyncio.create_task(self._produce(request, key, pending))
                self._tasks[key] = task
        return await asyncio.shield(asyncio.wrap_future(pending))

    async def _produce(
        self,
        request: MappingRequest,
        key: str,
        pending: Future[MappingResult],
    ) -> None:
        try:
            result = await self._execute_once(request, key)
            result = self._checkpoints.save(result)
            verify_mapping_result(result, request, self._bronze_store)
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

    async def _execute_once(self, request: MappingRequest, key: str) -> MappingResult:
        await asyncio.sleep(0)
        extraction = request.extraction_result
        candidates = extraction.candidate_set.candidates
        unmapped_gaps = tuple(
            item for item in extraction.gaps if item.code is ExtractionGapCode.UNMAPPED_HEADER
        )
        if len(candidates) > request.policy.max_mappings:
            raise AppError(ErrorCode.BUDGET_EXCEEDED, "M14 mapping count exceeds policy")
        if len(unmapped_gaps) > request.policy.max_unmapped_fields:
            raise AppError(ErrorCode.BUDGET_EXCEEDED, "M14 unmapped field count exceeds policy")
        fields = {item.name: item for item in request.extraction_request.contract.fields}
        evidence_records: list[MappingEvidence] = []
        mappings: list[FieldMapping] = []
        for candidate in candidates:
            field = fields[candidate.field_name]
            evidence = _mapping_evidence(
                request,
                candidate,
                field,
                self._producer_version,
            )
            evidence_records.append(evidence)
            mappings.append(
                _field_mapping(
                    request,
                    candidate,
                    field,
                    evidence,
                    self._producer_version,
                )
            )
        tables = {
            item.table_id: item for item in request.extraction_request.table_parsing_result.tables
        }
        unmapped: list[UnmappedField] = []
        for gap in unmapped_gaps:
            if gap.table_id is None or gap.source_cell_id is None:
                raise AppError(
                    ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "M14 unmapped header gap lacks source-cell evidence",
                )
            table = tables[gap.table_id]
            header = next(item for item in table.cells if item.cell_id == gap.source_cell_id)
            unmapped.append(
                _unmapped_field(
                    request,
                    gap.gap_id,
                    table.table_id,
                    header.cell_id,
                    header.cell_hash,
                    registered_alias_suggestions(
                        header.decoded_text,
                        request.extraction_request.contract.fields,
                    ),
                    self._producer_version,
                )
            )
        return _aggregate(
            request,
            key,
            tuple(evidence_records),
            tuple(mappings),
            tuple(unmapped),
            self._producer_version,
        )


def _mapping_evidence(
    request: MappingRequest,
    candidate: ExtractedFieldCandidate,
    field: FieldContract,
    producer_version: str,
) -> MappingEvidence:
    draft = MappingEvidence(
        task_id=request.extraction_result.task_id,
        run_id=request.extraction_result.run_id,
        contract_version=request.extraction_result.contract_version,
        created_at=request.runtime.checked_at,
        producer_version=producer_version,
        mapping_evidence_id="mpe_" + "0" * 32,
        source_candidate_id=candidate.candidate_id,
        source_header_cell_id=candidate.source_header_cell_id,
        source_header_cell_hash=candidate.source_header_cell_hash,
        source_value_kind=candidate.value_kind,
        source_evidence_ids=candidate.evidence_ids,
        entity_evidence_ids=candidate.entity_evidence_ids,
        target_field_name=field.name,
        target_field_contract_hash=calculate_field_contract_hash(field),
        method=MappingMethod.EXACT_CONTRACT_FIELD,
        rule_id=request.runtime.rule.rule_id,
        rule_hash=request.runtime.rule.rule_hash,
        score=1.0,
        evidence_hash="0" * 64,
    )
    value = calculate_mapping_evidence_hash(draft)
    return draft.model_copy(
        update={"mapping_evidence_id": f"mpe_{value[:32]}", "evidence_hash": value}
    )


def _field_mapping(
    request: MappingRequest,
    candidate: ExtractedFieldCandidate,
    field: FieldContract,
    evidence: MappingEvidence,
    producer_version: str,
) -> FieldMapping:
    compatible = is_value_kind_compatible(candidate.value_kind, field)
    threshold = request.policy.auto_accept_threshold
    decision = (
        MappingDecision.BLOCKED_TYPE_CONFLICT
        if not compatible
        else MappingDecision.BLOCKED_BELOW_THRESHOLD
        if 1.0 < threshold
        else MappingDecision.AUTO_ACCEPTED
    )
    draft = FieldMapping(
        task_id=request.extraction_result.task_id,
        run_id=request.extraction_result.run_id,
        contract_version=request.extraction_result.contract_version,
        created_at=request.runtime.checked_at,
        producer_version=producer_version,
        mapping_id="fmp_" + "0" * 32,
        contract_id=request.extraction_result.contract_id,
        contract_hash=request.extraction_result.contract_hash,
        source_candidate_id=candidate.candidate_id,
        source_candidate_hash=candidate.candidate_hash,
        source_field_name=candidate.field_name,
        target_field_name=field.name,
        target_field_contract_hash=calculate_field_contract_hash(field),
        mapping_evidence_id=evidence.mapping_evidence_id,
        mapping_evidence_hash=evidence.evidence_hash,
        source_evidence_ids=candidate.evidence_ids,
        entity_evidence_ids=candidate.entity_evidence_ids,
        method=MappingMethod.EXACT_CONTRACT_FIELD,
        score=1.0,
        threshold=threshold,
        type_compatible=compatible,
        decision=decision,
        eligible_for_m15=decision is MappingDecision.AUTO_ACCEPTED,
        mapping_hash="0" * 64,
    )
    value = calculate_field_mapping_hash(draft)
    return draft.model_copy(update={"mapping_id": f"fmp_{value[:32]}", "mapping_hash": value})


def _unmapped_field(
    request: MappingRequest,
    gap_id: str,
    table_id: str,
    header_cell_id: str,
    header_cell_hash: str,
    suggestions: tuple[str, ...],
    producer_version: str,
) -> UnmappedField:
    draft = UnmappedField(
        task_id=request.extraction_result.task_id,
        run_id=request.extraction_result.run_id,
        contract_version=request.extraction_result.contract_version,
        created_at=request.runtime.checked_at,
        producer_version=producer_version,
        unmapped_field_id="umf_" + "0" * 32,
        upstream_gap_id=gap_id,
        source_table_id=table_id,
        source_header_cell_id=header_cell_id,
        source_header_cell_hash=header_cell_hash,
        reason=UnmappedReason.UPSTREAM_HEADER_WITHOUT_VALUE_EVIDENCE,
        suggested_field_names=suggestions,
        detail="source header retained; automatic mapping blocked without M13 value evidence",
        unmapped_hash="0" * 64,
    )
    value = calculate_unmapped_field_hash(draft)
    return draft.model_copy(
        update={"unmapped_field_id": f"umf_{value[:32]}", "unmapped_hash": value}
    )


def _aggregate(
    request: MappingRequest,
    key: str,
    evidence: tuple[MappingEvidence, ...],
    mappings: tuple[FieldMapping, ...],
    unmapped: tuple[UnmappedField, ...],
    producer_version: str,
) -> MappingResult:
    extraction = request.extraction_result
    contract = request.extraction_request.contract
    metadata = {
        "task_id": extraction.task_id,
        "run_id": extraction.run_id,
        "contract_version": extraction.contract_version,
        "created_at": request.runtime.checked_at,
        "producer_version": producer_version,
    }
    mapping_set_draft = FieldMappingSet.model_validate(
        {
            **metadata,
            "mapping_set_id": "fms_" + "0" * 32,
            "contract_id": extraction.contract_id,
            "contract_hash": extraction.contract_hash,
            "upstream_extraction_output_hash": extraction.output_hash,
            "mappings": mappings,
            "mapping_set_hash": "0" * 64,
        }
    )
    mapping_set_hash = calculate_mapping_set_hash(mapping_set_draft)
    mapping_set = mapping_set_draft.model_copy(
        update={
            "mapping_set_id": f"fms_{mapping_set_hash[:32]}",
            "mapping_set_hash": mapping_set_hash,
        }
    )
    unmapped_set_draft = UnmappedFieldSet.model_validate(
        {
            **metadata,
            "unmapped_set_id": "ums_" + "0" * 32,
            "contract_id": extraction.contract_id,
            "contract_hash": extraction.contract_hash,
            "upstream_extraction_output_hash": extraction.output_hash,
            "fields": unmapped,
            "unmapped_set_hash": "0" * 64,
        }
    )
    unmapped_set_hash = calculate_unmapped_set_hash(unmapped_set_draft)
    unmapped_set = unmapped_set_draft.model_copy(
        update={
            "unmapped_set_id": f"ums_{unmapped_set_hash[:32]}",
            "unmapped_set_hash": unmapped_set_hash,
        }
    )
    accepted = sum(item.eligible_for_m15 for item in mappings)
    if mappings and accepted == len(mappings) and not extraction.gaps:
        status = MappingStatus.SUCCEEDED
    elif accepted:
        status = MappingStatus.PARTIAL
    elif mappings or extraction.gaps:
        status = MappingStatus.NEEDS_REVIEW
    else:
        status = MappingStatus.UNSUPPORTED
    evidence_coverage = 1.0 if not mappings else len(evidence) / len(mappings)
    acceptance_rate = 1.0 if not mappings else accepted / len(mappings)
    metrics = MappingMetrics(
        input_candidate_count=len(extraction.candidate_set.candidates),
        mapping_count=len(mappings),
        auto_accepted_count=accepted,
        blocked_mapping_count=len(mappings) - accepted,
        unmapped_field_count=len(unmapped),
        alias_suggestion_count=sum(len(item.suggested_field_names) for item in unmapped),
        upstream_gap_count=len(extraction.gaps),
        mapping_evidence_count=len(evidence),
        evidence_coverage=evidence_coverage,
        automatic_acceptance_rate=acceptance_rate,
        m15_eligible_count=accepted,
    )
    input_hash = calculate_mapping_input_hash(request)
    payload = FieldMappedPayload(
        status=status,
        contract_id=contract.contract_id,
        contract_hash=contract.contract_hash,
        upstream_extraction_output_hash=extraction.output_hash,
        mapping_set_hash=mapping_set.mapping_set_hash,
        unmapped_set_hash=unmapped_set.unmapped_set_hash,
        mapping_count=len(mappings),
        accepted_count=accepted,
        unmapped_count=len(unmapped),
        input_hash=input_hash,
        output_hash="0" * 64,
        idempotency_key=key,
    )
    event = EventEnvelope[FieldMappedPayload](
        event_id=calculate_mapping_event_id(key),
        event_type=EventType.FIELD_MAPPED,
        task_id=contract.task_id,
        run_id=contract.run_id,
        occurred_at=request.runtime.checked_at,
        producer=ProducerRef(component="field-mapping-service", version=producer_version),
        payload=payload,
        correlation_id=contract.task_id,
        causation_event_id=extraction.event.event_id,
    )
    upstream_gap_ids = tuple(item.gap_id for item in extraction.gaps)
    draft = MappingResult.model_validate(
        {
            **metadata,
            "status": status,
            "contract_id": contract.contract_id,
            "contract_hash": contract.contract_hash,
            "upstream_extraction_input_hash": extraction.input_hash,
            "upstream_extraction_output_hash": extraction.output_hash,
            "policy": request.policy,
            "policy_hash": calculate_mapping_policy_hash(request.policy),
            "runtime": request.runtime,
            "input_hash": input_hash,
            "output_hash": "0" * 64,
            "idempotency_key": key,
            "mapping_evidence": evidence,
            "mapping_set": mapping_set,
            "unmapped_set": unmapped_set,
            "upstream_gap_ids": upstream_gap_ids,
            "warnings": tuple(f"upstream_gap:{item}" for item in upstream_gap_ids),
            "metrics": metrics,
            "event": event,
        }
    )
    output_hash = calculate_mapping_output_hash(draft)
    final_event = event.model_copy(
        update={"payload": payload.model_copy(update={"output_hash": output_hash})}
    )
    return MappingResult.model_validate(
        draft.model_copy(update={"output_hash": output_hash, "event": final_event})
    )

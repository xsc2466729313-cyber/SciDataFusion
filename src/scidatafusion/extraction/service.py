"""Idempotent evidence-first extraction from quality-passed M10 TableIR."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from threading import RLock

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.extraction import (
    CandidateOrigin,
    EvidenceAtom,
    EvidenceAtomSet,
    EvidenceSourceKind,
    ExtractedFieldCandidate,
    ExtractedFieldCandidateSet,
    ExtractionGap,
    ExtractionGapCode,
    ExtractionMetrics,
    ExtractionRequest,
    ExtractionResult,
    ExtractionStatus,
    FieldExtractedPayload,
)
from scidatafusion.contracts.scientific import FieldContract, FieldRequirement
from scidatafusion.contracts.tables import CellIR, TableIR
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.extraction.checkpoints import (
    ExtractionCheckpointStore,
    MemoryExtractionCheckpointStore,
)
from scidatafusion.extraction.integrity import (
    calculate_candidate_hash,
    calculate_candidate_set_hash,
    calculate_evidence_hash,
    calculate_evidence_set_hash,
    calculate_extraction_event_id,
    calculate_extraction_gap_id,
    calculate_extraction_idempotency_key,
    calculate_extraction_input_hash,
    calculate_extraction_output_hash,
    calculate_extraction_policy_hash,
    calculate_field_contract_hash,
    text_sha256,
    verify_extraction_request,
    verify_extraction_result,
)


class EvidenceFirstExtractionService:
    """Create explicit field candidates only after exact cell evidence exists."""

    def __init__(
        self,
        *,
        bronze_store: BronzeByteStore,
        checkpoints: ExtractionCheckpointStore | None = None,
        producer_version: str = "1.0.0",
    ) -> None:
        self._bronze_store = bronze_store
        self._checkpoints = checkpoints or MemoryExtractionCheckpointStore()
        self._producer_version = producer_version
        self._cache: dict[str, ExtractionResult] = {}
        self._inflight: dict[str, Future[ExtractionResult]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = RLock()

    async def execute(self, request: ExtractionRequest) -> ExtractionResult:
        """Verify, replay, or execute one cancellation-isolated M13 request."""

        verify_extraction_request(request, self._bronze_store)
        key = calculate_extraction_idempotency_key(request, self._producer_version)
        if not request.force_recompute:
            with self._lock:
                cached = self._cache.get(key)
            if cached is not None:
                verify_extraction_result(cached, request, self._bronze_store)
                return cached
            checkpoint = self._checkpoints.load(key)
            if checkpoint is not None:
                verify_extraction_result(checkpoint, request, self._bronze_store)
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
        request: ExtractionRequest,
        key: str,
        pending: Future[ExtractionResult],
    ) -> None:
        try:
            result = await self._execute_once(request, key)
            result = self._checkpoints.save(result)
            verify_extraction_result(result, request, self._bronze_store)
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

    async def _execute_once(self, request: ExtractionRequest, key: str) -> ExtractionResult:
        await asyncio.sleep(0)
        if len(request.table_parsing_result.tables) > request.policy.max_tables:
            raise AppError(ErrorCode.BUDGET_EXCEEDED, "M13 table count exceeds policy")
        fields = {item.name: item for item in request.contract.fields}
        required_fields = tuple(
            item.name
            for item in request.contract.fields
            if item.requirement is FieldRequirement.REQUIRED
        )
        atoms: list[EvidenceAtom] = []
        candidates: list[ExtractedFieldCandidate] = []
        gaps: list[ExtractionGap] = []
        accepted_tables = 0
        input_rows = 0
        extracted_rows = 0
        for table in request.table_parsing_result.tables:
            context_headers = set(request.context_headers)
            data_rows = table.row_count - table.header_hierarchy.header_row_count
            input_rows += max(0, data_rows)
            if input_rows > request.policy.max_rows:
                raise AppError(ErrorCode.BUDGET_EXCEEDED, "M13 row count exceeds policy")
            if not table.quality.passed:
                gaps.append(
                    _gap(
                        ExtractionGapCode.TABLE_QUALITY_FAILED,
                        table=table,
                        detail="TableIR quality gates did not all pass",
                    )
                )
                continue
            if table.header_hierarchy.header_row_count != 1:
                gaps.append(
                    _gap(
                        ExtractionGapCode.HEADER_STRUCTURE_UNSUPPORTED,
                        table=table,
                        detail="exact-header extraction requires one header row",
                    )
                )
                continue
            accepted_tables += 1
            headers = table.cells[: table.column_count]
            header_by_name = {item.decoded_text: item for item in headers}
            for field_name in required_fields:
                if field_name not in header_by_name:
                    gaps.append(
                        _gap(
                            ExtractionGapCode.REQUIRED_FIELD_HEADER_MISSING,
                            table=table,
                            field_name=field_name,
                            detail="required contract field has no exact table header",
                        )
                    )
            for header in headers:
                if header.decoded_text not in fields and header.decoded_text not in context_headers:
                    gaps.append(
                        _gap(
                            ExtractionGapCode.UNMAPPED_HEADER,
                            table=table,
                            source_cell=header,
                            blocking=False,
                            detail="table header has no exact contract field",
                        )
                    )
            for row_index in range(1, table.row_count):
                row_cells = table.cells[
                    row_index * table.column_count : (row_index + 1) * table.column_count
                ]
                mapped = {
                    header.decoded_text: (header, value)
                    for header, value in zip(headers, row_cells, strict=True)
                    if (
                        header.decoded_text in context_headers
                        or (
                            header.decoded_text in fields
                            and fields[header.decoded_text].requirement
                            is not FieldRequirement.DERIVED
                        )
                    )
                }
                missing_entity = tuple(
                    name
                    for name in request.contract.entity_keys
                    if name not in mapped or not mapped[name][1].decoded_text
                )
                if missing_entity:
                    gaps.append(
                        _gap(
                            ExtractionGapCode.ENTITY_BINDING_MISSING,
                            table=table,
                            row_index=row_index,
                            field_name=missing_entity[0],
                            detail="row is missing an explicit entity-key value",
                        )
                    )
                    continue
                row_atoms: dict[str, EvidenceAtom] = {}
                for field_name, (_, value_cell) in mapped.items():
                    if not value_cell.decoded_text:
                        if fields[field_name].requirement is FieldRequirement.REQUIRED:
                            gaps.append(
                                _gap(
                                    ExtractionGapCode.REQUIRED_VALUE_EMPTY,
                                    table=table,
                                    row_index=row_index,
                                    field_name=field_name,
                                    detail="required field cell is empty",
                                )
                            )
                        continue
                    atom = _evidence_atom(request, table, value_cell, self._producer_version)
                    atoms.append(atom)
                    row_atoms[field_name] = atom
                entity_ids = tuple(
                    row_atoms[name].evidence_id for name in request.contract.entity_keys
                )
                row_group_id = _row_group_id(table, row_index, entity_ids)
                row_candidate_count = 0
                for field_name, (header, value_cell) in mapped.items():
                    if field_name not in fields:
                        continue
                    selected_atom = row_atoms.get(field_name)
                    if selected_atom is None:
                        continue
                    candidate = _candidate(
                        request,
                        fields[field_name],
                        table,
                        header,
                        value_cell,
                        selected_atom,
                        entity_ids,
                        row_group_id,
                        self._producer_version,
                    )
                    candidates.append(candidate)
                    row_candidate_count += 1
                if row_candidate_count:
                    extracted_rows += 1
                if (
                    len(atoms) > request.policy.max_evidence_atoms
                    or len(candidates) > request.policy.max_candidates
                ):
                    raise AppError(ErrorCode.BUDGET_EXCEEDED, "M13 output count exceeds policy")
        return _aggregate(
            request,
            key,
            tuple(atoms),
            tuple(candidates),
            tuple(gaps),
            required_fields,
            accepted_tables,
            input_rows,
            extracted_rows,
            self._producer_version,
        )


def _evidence_atom(
    request: ExtractionRequest,
    table: TableIR,
    cell: CellIR,
    producer_version: str,
) -> EvidenceAtom:
    draft = EvidenceAtom(
        task_id=request.contract.task_id,
        run_id=request.contract.run_id,
        contract_version=request.contract.version,
        created_at=request.runtime.checked_at,
        producer_version=producer_version,
        evidence_id="evi_" + "0" * 32,
        source_kind=EvidenceSourceKind.TABLE_CELL,
        object_id=table.object_id,
        artifact_hash=table.source_byte_sha256,
        table_id=table.table_id,
        table_hash=table.table_hash,
        cell_id=cell.cell_id,
        cell_hash=cell.cell_hash,
        row_index=cell.row_index,
        column_index=cell.column_index,
        start_byte=cell.source.start_byte,
        end_byte=cell.source.end_byte,
        raw_lexeme=cell.raw_text,
        raw_lexeme_sha256=text_sha256(cell.raw_text),
        raw_value=cell.decoded_text,
        raw_value_sha256=text_sha256(cell.decoded_text),
        extraction_method="deterministic_exact_header_table_cell",
        evidence_hash="0" * 64,
    )
    value = calculate_evidence_hash(draft)
    return draft.model_copy(update={"evidence_id": f"evi_{value[:32]}", "evidence_hash": value})


def _candidate(
    request: ExtractionRequest,
    field: FieldContract,
    table: TableIR,
    header: CellIR,
    value_cell: CellIR,
    atom: EvidenceAtom,
    entity_ids: tuple[str, ...],
    row_group_id: str,
    producer_version: str,
) -> ExtractedFieldCandidate:
    draft = ExtractedFieldCandidate(
        task_id=request.contract.task_id,
        run_id=request.contract.run_id,
        contract_version=request.contract.version,
        created_at=request.runtime.checked_at,
        producer_version=producer_version,
        candidate_id="fcd_" + "0" * 32,
        contract_id=request.contract.contract_id,
        contract_hash=request.contract.contract_hash,
        field_name=field.name,
        field_contract_hash=calculate_field_contract_hash(field),
        source_header_cell_id=header.cell_id,
        source_header_cell_hash=header.cell_hash,
        source_value_cell_id=value_cell.cell_id,
        source_value_cell_hash=value_cell.cell_hash,
        source_table_id=table.table_id,
        source_table_hash=table.table_hash,
        source_row_index=value_cell.row_index,
        row_group_id=row_group_id,
        raw_value=value_cell.decoded_text,
        raw_value_sha256=text_sha256(value_cell.decoded_text),
        value_kind=value_cell.inferred_kind,
        origin=CandidateOrigin.EXPLICIT,
        evidence_ids=(atom.evidence_id,),
        entity_evidence_ids=entity_ids,
        candidate_hash="0" * 64,
    )
    value = calculate_candidate_hash(draft)
    return draft.model_copy(update={"candidate_id": f"fcd_{value[:32]}", "candidate_hash": value})


def _row_group_id(table: TableIR, row_index: int, entity_ids: tuple[str, ...]) -> str:
    value = canonical_hash(
        {"entity_evidence_ids": entity_ids, "row_index": row_index, "table_hash": table.table_hash}
    )
    return f"row_{value[:32]}"


def _gap(
    code: ExtractionGapCode,
    *,
    table: TableIR | None = None,
    source_cell: CellIR | None = None,
    row_index: int | None = None,
    field_name: str | None = None,
    blocking: bool = True,
    detail: str,
) -> ExtractionGap:
    draft = ExtractionGap(
        gap_id="xgp_" + "0" * 16,
        code=code,
        table_id=None if table is None else table.table_id,
        source_cell_id=None if source_cell is None else source_cell.cell_id,
        row_index=row_index,
        field_name=field_name,
        blocking=blocking,
        detail=detail,
    )
    return draft.model_copy(update={"gap_id": calculate_extraction_gap_id(draft)})


def _aggregate(
    request: ExtractionRequest,
    key: str,
    atoms: tuple[EvidenceAtom, ...],
    candidates: tuple[ExtractedFieldCandidate, ...],
    gaps: tuple[ExtractionGap, ...],
    required_fields: tuple[str, ...],
    accepted_tables: int,
    input_rows: int,
    extracted_rows: int,
    producer_version: str,
) -> ExtractionResult:
    metadata = {
        "task_id": request.contract.task_id,
        "run_id": request.contract.run_id,
        "contract_version": request.contract.version,
        "created_at": request.runtime.checked_at,
        "producer_version": producer_version,
    }
    evidence_draft = EvidenceAtomSet.model_validate(
        {
            **metadata,
            "evidence_set_id": "evs_" + "0" * 32,
            "contract_id": request.contract.contract_id,
            "contract_hash": request.contract.contract_hash,
            "upstream_table_output_hash": request.table_parsing_result.output_hash,
            "atoms": atoms,
            "evidence_set_hash": "0" * 64,
        }
    )
    evidence_hash = calculate_evidence_set_hash(evidence_draft)
    evidence_set = evidence_draft.model_copy(
        update={"evidence_set_id": f"evs_{evidence_hash[:32]}", "evidence_set_hash": evidence_hash}
    )
    candidate_draft = ExtractedFieldCandidateSet.model_validate(
        {
            **metadata,
            "candidate_set_id": "fcs_" + "0" * 32,
            "contract_id": request.contract.contract_id,
            "contract_hash": request.contract.contract_hash,
            "upstream_table_output_hash": request.table_parsing_result.output_hash,
            "candidates": candidates,
            "candidate_set_hash": "0" * 64,
        }
    )
    candidate_hash = calculate_candidate_set_hash(candidate_draft)
    candidate_set = candidate_draft.model_copy(
        update={
            "candidate_set_id": f"fcs_{candidate_hash[:32]}",
            "candidate_set_hash": candidate_hash,
        }
    )
    field_names = {item.field_name for item in candidates}
    extracted_required = tuple(item for item in required_fields if item in field_names)
    required_coverage = (
        1.0 if not required_fields else len(extracted_required) / len(required_fields)
    )
    if candidates and gaps:
        status = ExtractionStatus.PARTIAL
    elif candidates:
        status = ExtractionStatus.SUCCEEDED
    elif gaps:
        status = ExtractionStatus.NEEDS_REVIEW
    else:
        status = ExtractionStatus.UNSUPPORTED
    metrics = ExtractionMetrics(
        input_table_count=len(request.table_parsing_result.tables),
        accepted_table_count=accepted_tables,
        input_data_row_count=input_rows,
        extracted_row_count=extracted_rows,
        evidence_atom_count=len(atoms),
        candidate_count=len(candidates),
        explicit_candidate_count=len(candidates),
        evidence_coverage=1.0,
        required_field_coverage=required_coverage,
        entity_bound_candidate_count=len(candidates),
        gap_count=len(gaps),
    )
    input_hash = calculate_extraction_input_hash(request)
    payload = FieldExtractedPayload(
        status=status,
        contract_id=request.contract.contract_id,
        contract_hash=request.contract.contract_hash,
        upstream_table_output_hash=request.table_parsing_result.output_hash,
        evidence_set_hash=evidence_set.evidence_set_hash,
        candidate_set_hash=candidate_set.candidate_set_hash,
        evidence_count=len(atoms),
        candidate_count=len(candidates),
        gap_count=len(gaps),
        input_hash=input_hash,
        output_hash="0" * 64,
        idempotency_key=key,
    )
    event = EventEnvelope[FieldExtractedPayload](
        event_id=calculate_extraction_event_id(key),
        event_type=EventType.FIELD_EXTRACTED,
        task_id=request.contract.task_id,
        run_id=request.contract.run_id,
        occurred_at=request.runtime.checked_at,
        producer=ProducerRef(
            component="evidence-first-extraction-service", version=producer_version
        ),
        payload=payload,
        correlation_id=request.contract.task_id,
        causation_event_id=request.table_parsing_result.event.event_id,
    )
    draft = ExtractionResult.model_validate(
        {
            **metadata,
            "status": status,
            "contract_id": request.contract.contract_id,
            "contract_hash": request.contract.contract_hash,
            "upstream_table_input_hash": request.table_parsing_result.input_hash,
            "upstream_table_output_hash": request.table_parsing_result.output_hash,
            "policy": request.policy,
            "policy_hash": calculate_extraction_policy_hash(request.policy),
            "runtime": request.runtime,
            "input_hash": input_hash,
            "output_hash": "0" * 64,
            "idempotency_key": key,
            "evidence_set": evidence_set,
            "candidate_set": candidate_set,
            "required_field_names": required_fields,
            "extracted_required_field_names": extracted_required,
            "gaps": gaps,
            "warnings": tuple(f"{item.code.value}:{item.gap_id}" for item in gaps),
            "metrics": metrics,
            "event": event,
        }
    )
    output_hash = calculate_extraction_output_hash(draft)
    final_event = event.model_copy(
        update={"payload": payload.model_copy(update={"output_hash": output_hash})}
    )
    return ExtractionResult.model_validate(
        draft.model_copy(update={"output_hash": output_hash, "event": final_event})
    )

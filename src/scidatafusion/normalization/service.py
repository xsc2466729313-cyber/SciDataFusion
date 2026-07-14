"""Idempotent, no-guess M15 normalization over verified M14 mappings."""

from __future__ import annotations

import asyncio
import hashlib
from concurrent.futures import Future
from threading import RLock
from typing import Literal

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.extraction import EvidenceAtom, ExtractedFieldCandidate
from scidatafusion.contracts.mapping import FieldMapping
from scidatafusion.contracts.normalization import (
    NormalizationIssue,
    NormalizationIssueCode,
    NormalizationIssueSet,
    NormalizationMetrics,
    NormalizationRequest,
    NormalizationResult,
    NormalizationStatus,
    NormalizedField,
    NormalizedFieldStatus,
    NormalizedRecord,
    NormalizedRecordSet,
    NormalizedValueKind,
    RecordNormalizedPayload,
    TransformationKind,
    TransformationRecord,
    TransformationRecordSet,
)
from scidatafusion.contracts.scientific import DataType, FieldContract
from scidatafusion.contracts.tables import TableIR
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.normalization.checkpoints import (
    MemoryNormalizationCheckpointStore,
    NormalizationCheckpointStore,
)
from scidatafusion.normalization.integrity import (
    calculate_issue_hash,
    calculate_issue_set_hash,
    calculate_normalization_event_id,
    calculate_normalization_idempotency_key,
    calculate_normalization_input_hash,
    calculate_normalization_output_hash,
    calculate_normalization_policy_hash,
    calculate_normalized_field_hash,
    calculate_record_hash,
    calculate_record_set_hash,
    calculate_transformation_hash,
    calculate_transformation_set_hash,
    verify_normalization_request,
    verify_normalization_result,
)
from scidatafusion.normalization.rules import jd_to_mjd_exact, parse_decimal_exact


class ScientificNormalizationService:
    """Parse exact values while blocking scientifically ambiguous conversions."""

    def __init__(
        self,
        *,
        bronze_store: BronzeByteStore,
        checkpoints: NormalizationCheckpointStore | None = None,
        producer_version: str = "1.0.0",
    ) -> None:
        self._bronze_store = bronze_store
        self._checkpoints = checkpoints or MemoryNormalizationCheckpointStore()
        self._producer_version = producer_version
        self._cache: dict[str, NormalizationResult] = {}
        self._inflight: dict[str, Future[NormalizationResult]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = RLock()

    async def execute(self, request: NormalizationRequest) -> NormalizationResult:
        verify_normalization_request(request, self._bronze_store)
        key = calculate_normalization_idempotency_key(request, self._producer_version)
        if not request.force_recompute:
            with self._lock:
                cached = self._cache.get(key)
            if cached is not None:
                verify_normalization_result(cached, request, self._bronze_store)
                return cached
            checkpoint = self._checkpoints.load(key)
            if checkpoint is not None:
                verify_normalization_result(checkpoint, request, self._bronze_store)
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
        self, request: NormalizationRequest, key: str, pending: Future[NormalizationResult]
    ) -> None:
        try:
            result = await self._execute_once(request, key)
            result = self._checkpoints.save(result)
            verify_normalization_result(result, request, self._bronze_store)
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

    async def _execute_once(self, request: NormalizationRequest, key: str) -> NormalizationResult:
        await asyncio.sleep(0)
        mappings = request.mapping_result.mapping_set.mappings
        if len(mappings) > request.policy.max_fields:
            raise AppError(ErrorCode.BUDGET_EXCEEDED, "M15 field count exceeds policy")
        candidates = {
            item.candidate_id: item
            for item in request.mapping_request.extraction_result.candidate_set.candidates
        }
        evidence = request.mapping_request.extraction_result.evidence_set.atoms
        tables = request.mapping_request.extraction_request.table_parsing_result.tables
        context_by_row = _context_evidence_by_row(tables, evidence)
        contracts = {
            item.name: item for item in request.mapping_request.extraction_request.contract.fields
        }
        transformations: list[TransformationRecord] = []
        issues: list[NormalizationIssue] = []
        fields: list[NormalizedField] = []
        for mapping in mappings:
            candidate = candidates[mapping.source_candidate_id]
            contract = contracts[mapping.target_field_name]
            field_transformations, field_issues, field = _normalize_field(
                request,
                mapping,
                candidate,
                contract,
                context_by_row,
                self._producer_version,
            )
            transformations.extend(field_transformations)
            issues.extend(field_issues)
            fields.append(field)
        if len(issues) > request.policy.max_issues:
            raise AppError(ErrorCode.BUDGET_EXCEEDED, "M15 issue count exceeds policy")
        return _aggregate(
            request,
            key,
            tuple(fields),
            tuple(transformations),
            tuple(issues),
            self._producer_version,
        )


def _normalize_field(
    request: NormalizationRequest,
    mapping: FieldMapping,
    candidate: ExtractedFieldCandidate,
    contract: FieldContract,
    context_by_row: dict[tuple[str, int, str], tuple[str, str]],
    producer_version: str,
) -> tuple[tuple[TransformationRecord, ...], tuple[NormalizationIssue, ...], NormalizedField]:
    transformations: list[TransformationRecord] = []
    issue_codes: list[NormalizationIssueCode] = []
    normalized: str | None = candidate.raw_value
    kind: NormalizedValueKind | None = NormalizedValueKind.STRING
    source_unit: str | None = None
    time_scale: str | None = None
    context_evidence_ids: tuple[str, ...] = ()
    if not mapping.eligible_for_m15:
        normalized = None
        kind = None
        issue_codes.append(NormalizationIssueCode.MAPPING_NOT_ELIGIBLE)
    elif contract.data_type in {DataType.INTEGER, DataType.NUMBER}:
        try:
            parsed = parse_decimal_exact(candidate.raw_value)
        except AppError:
            normalized = None
            kind = None
            issue_codes.append(NormalizationIssueCode.INVALID_DECIMAL)
        else:
            normalized = parsed.text
            kind = NormalizedValueKind.DECIMAL
            transformations.append(
                _transformation(
                    request,
                    mapping,
                    candidate,
                    normalized,
                    parsed.decimal_places,
                    parsed.significant_digits,
                    producer_version,
                )
            )
    if request.context_evidence_enabled and mapping.eligible_for_m15:
        unit_name = {
            "observation_time": "observation_time_unit",
            "magnitude": "magnitude_unit",
        }.get(contract.name)
        unit_evidence = (
            None
            if unit_name is None
            else context_by_row.get(
                (candidate.source_table_id, candidate.source_row_index, unit_name)
            )
        )
        if unit_evidence is not None and unit_evidence[0] in contract.allowed_units:
            source_unit = unit_evidence[0]
            context_evidence_ids = (*context_evidence_ids, unit_evidence[1])
        if contract.semantic_type == "astronomical_time":
            scale_evidence = context_by_row.get(
                (
                    candidate.source_table_id,
                    candidate.source_row_index,
                    "observation_time_scale",
                )
            )
            if scale_evidence is not None and scale_evidence[0] == "UTC":
                time_scale = scale_evidence[0]
                context_evidence_ids = (*context_evidence_ids, scale_evidence[1])
    if (
        request.jd_to_mjd_conversion_enabled
        and mapping.eligible_for_m15
        and contract.name == "observation_time"
        and source_unit == "JD"
        and contract.target_unit == "MJD"
        and normalized is not None
    ):
        converted = jd_to_mjd_exact(candidate.raw_value)
        normalized = converted.text
        transformations.append(
            _transformation(
                request,
                mapping,
                candidate,
                normalized,
                converted.decimal_places,
                converted.significant_digits,
                producer_version,
                kind=TransformationKind.JD_TO_MJD_EXACT,
            )
        )
    if mapping.eligible_for_m15 and contract.unit_dimension is not None and source_unit is None:
        issue_codes.append(NormalizationIssueCode.SOURCE_UNIT_MISSING)
    if (
        mapping.eligible_for_m15
        and contract.semantic_type == "astronomical_time"
        and time_scale is None
        and not (
            request.jd_to_mjd_conversion_enabled
            and source_unit == "JD"
            and contract.target_unit == "MJD"
        )
    ):
        issue_codes.append(NormalizationIssueCode.TIME_SCALE_MISSING)
    issue_records = tuple(
        _issue(request, mapping, candidate, code, producer_version) for code in issue_codes
    )
    transformation_ids = tuple(item.transformation_id for item in transformations)
    issue_ids = tuple(item.issue_id for item in issue_records)
    status = (
        NormalizedFieldStatus.BLOCKED
        if normalized is None
        else NormalizedFieldStatus.NEEDS_REVIEW
        if issue_ids
        else NormalizedFieldStatus.NORMALIZED
    )
    draft = NormalizedField(
        task_id=request.mapping_result.task_id,
        run_id=request.mapping_result.run_id,
        contract_version=request.mapping_result.contract_version,
        created_at=request.runtime.checked_at,
        producer_version=producer_version,
        normalized_field_id="nfd_" + "0" * 32,
        row_group_id=candidate.row_group_id,
        mapping_id=mapping.mapping_id,
        mapping_hash=mapping.mapping_hash,
        source_candidate_id=candidate.candidate_id,
        source_candidate_hash=candidate.candidate_hash,
        field_name=contract.name,
        raw_value=candidate.raw_value,
        raw_value_sha256=candidate.raw_value_sha256,
        normalized_value=normalized,
        normalized_value_sha256=_sha(normalized) if normalized is not None else None,
        value_kind=kind,
        target_unit=contract.target_unit,
        transformation_ids=transformation_ids,
        issue_ids=issue_ids,
        evidence_ids=candidate.evidence_ids,
        entity_evidence_ids=candidate.entity_evidence_ids,
        source_unit=source_unit,
        time_scale=time_scale,
        context_evidence_ids=tuple(dict.fromkeys(context_evidence_ids)),
        status=status,
        eligible_for_m16=status is NormalizedFieldStatus.NORMALIZED,
        normalized_field_hash="0" * 64,
    )
    value = calculate_normalized_field_hash(draft)
    return (
        tuple(transformations),
        issue_records,
        draft.model_copy(
            update={"normalized_field_id": f"nfd_{value[:32]}", "normalized_field_hash": value}
        ),
    )


def _context_evidence_by_row(
    tables: tuple[TableIR, ...],
    evidence: tuple[EvidenceAtom, ...],
) -> dict[tuple[str, int, str], tuple[str, str]]:
    result: dict[tuple[str, int, str], tuple[str, str]] = {}
    evidence_by_cell = {item.cell_id: item for item in evidence}
    for table in tables:
        headers = table.cells[: table.column_count]
        for row_index in range(1, table.row_count):
            row = table.cells[row_index * table.column_count : (row_index + 1) * table.column_count]
            for header, cell in zip(headers, row, strict=True):
                atom = evidence_by_cell.get(cell.cell_id)
                if atom is not None:
                    result[(table.table_id, row_index, header.decoded_text)] = (
                        atom.raw_value,
                        atom.evidence_id,
                    )
    return result


def _transformation(
    request: NormalizationRequest,
    mapping: FieldMapping,
    candidate: ExtractedFieldCandidate,
    normalized: str,
    decimal_places: int,
    significant_digits: int,
    producer_version: str,
    *,
    kind: TransformationKind = TransformationKind.PARSE_DECIMAL_EXACT,
) -> TransformationRecord:
    formula: Literal[
        "Decimal(raw_value); require finite; format(value, 'f')",
        "Decimal(raw_value) - Decimal('2400000.5'); require finite; format(value, 'f')",
    ] = (
        "Decimal(raw_value) - Decimal('2400000.5'); require finite; format(value, 'f')"
        if kind is TransformationKind.JD_TO_MJD_EXACT
        else "Decimal(raw_value); require finite; format(value, 'f')"
    )
    draft = TransformationRecord(
        task_id=request.mapping_result.task_id,
        run_id=request.mapping_result.run_id,
        contract_version=request.mapping_result.contract_version,
        created_at=request.runtime.checked_at,
        producer_version=producer_version,
        transformation_id="trn_" + "0" * 32,
        mapping_id=mapping.mapping_id,
        source_candidate_id=candidate.candidate_id,
        field_name=mapping.target_field_name,
        kind=kind,
        raw_value=candidate.raw_value,
        raw_value_sha256=candidate.raw_value_sha256,
        normalized_value=normalized,
        normalized_value_sha256=_sha(normalized),
        formula=formula,
        library="python.decimal",
        library_version=request.runtime.decimal_library_version,
        reversible=True,
        decimal_places=decimal_places,
        significant_digits=significant_digits,
        evidence_ids=candidate.evidence_ids,
        transformation_hash="0" * 64,
    )
    value = calculate_transformation_hash(draft)
    return draft.model_copy(
        update={"transformation_id": f"trn_{value[:32]}", "transformation_hash": value}
    )


_ISSUE_DETAILS = {
    NormalizationIssueCode.MAPPING_NOT_ELIGIBLE: "M14 mapping is not eligible for deterministic normalization",
    NormalizationIssueCode.SOURCE_UNIT_MISSING: "source unit is not evidenced; target unit was not applied",
    NormalizationIssueCode.TIME_SCALE_MISSING: "time scale is not evidenced; astronomical time conversion was not applied",
    NormalizationIssueCode.INVALID_DECIMAL: "source value is not a finite exact decimal",
}


def _issue(
    request: NormalizationRequest,
    mapping: FieldMapping,
    candidate: ExtractedFieldCandidate,
    code: NormalizationIssueCode,
    producer_version: str,
) -> NormalizationIssue:
    draft = NormalizationIssue(
        task_id=request.mapping_result.task_id,
        run_id=request.mapping_result.run_id,
        contract_version=request.mapping_result.contract_version,
        created_at=request.runtime.checked_at,
        producer_version=producer_version,
        issue_id="nis_" + "0" * 32,
        mapping_id=mapping.mapping_id,
        source_candidate_id=candidate.candidate_id,
        field_name=mapping.target_field_name,
        code=code,
        detail=_ISSUE_DETAILS[code],
        evidence_ids=candidate.evidence_ids,
        issue_hash="0" * 64,
    )
    value = calculate_issue_hash(draft)
    return draft.model_copy(update={"issue_id": f"nis_{value[:32]}", "issue_hash": value})


def _aggregate(
    request: NormalizationRequest,
    key: str,
    fields: tuple[NormalizedField, ...],
    transformations: tuple[TransformationRecord, ...],
    issues: tuple[NormalizationIssue, ...],
    producer_version: str,
) -> NormalizationResult:
    mapping = request.mapping_result
    metadata = {
        "task_id": mapping.task_id,
        "run_id": mapping.run_id,
        "contract_version": mapping.contract_version,
        "created_at": request.runtime.checked_at,
        "producer_version": producer_version,
    }
    groups: dict[str, list[NormalizedField]] = {}
    for field in fields:
        groups.setdefault(field.row_group_id, []).append(field)
    records: list[NormalizedRecord] = []
    for row_group_id, row_fields in groups.items():
        draft = NormalizedRecord.model_validate(
            {
                **metadata,
                "normalized_record_id": "nrc_" + "0" * 32,
                "row_group_id": row_group_id,
                "fields": tuple(row_fields),
                "eligible_field_count": sum(item.eligible_for_m16 for item in row_fields),
                "record_hash": "0" * 64,
            }
        )
        value = calculate_record_hash(draft)
        records.append(
            draft.model_copy(
                update={"normalized_record_id": f"nrc_{value[:32]}", "record_hash": value}
            )
        )
    record_set_draft = NormalizedRecordSet.model_validate(
        {
            **metadata,
            "record_set_id": "nrs_" + "0" * 32,
            "contract_id": mapping.contract_id,
            "contract_hash": mapping.contract_hash,
            "upstream_mapping_output_hash": mapping.output_hash,
            "records": tuple(records),
            "record_set_hash": "0" * 64,
        }
    )
    record_set_hash = calculate_record_set_hash(record_set_draft)
    record_set = record_set_draft.model_copy(
        update={"record_set_id": f"nrs_{record_set_hash[:32]}", "record_set_hash": record_set_hash}
    )
    transformation_draft = TransformationRecordSet.model_validate(
        {
            **metadata,
            "transformation_set_id": "trs_" + "0" * 32,
            "records": transformations,
            "transformation_set_hash": "0" * 64,
        }
    )
    transformation_hash = calculate_transformation_set_hash(transformation_draft)
    transformation_set = transformation_draft.model_copy(
        update={
            "transformation_set_id": f"trs_{transformation_hash[:32]}",
            "transformation_set_hash": transformation_hash,
        }
    )
    issue_draft = NormalizationIssueSet.model_validate(
        {
            **metadata,
            "issue_set_id": "nss_" + "0" * 32,
            "issues": issues,
            "issue_set_hash": "0" * 64,
        }
    )
    issue_hash = calculate_issue_set_hash(issue_draft)
    issue_set = issue_draft.model_copy(
        update={"issue_set_id": f"nss_{issue_hash[:32]}", "issue_set_hash": issue_hash}
    )
    eligible = sum(item.eligible_for_m16 for item in fields)
    status = (
        NormalizationStatus.SUCCEEDED
        if fields and eligible == len(fields)
        else NormalizationStatus.PARTIAL
        if eligible
        else NormalizationStatus.NEEDS_REVIEW
        if fields
        else NormalizationStatus.UNSUPPORTED
    )
    metrics = NormalizationMetrics(
        input_mapping_count=len(mapping.mapping_set.mappings),
        normalized_field_count=len(fields),
        record_count=len(records),
        transformation_count=len(transformations),
        non_identity_transformation_count=len(transformations),
        issue_count=len(issues),
        m16_eligible_field_count=eligible,
        transformation_coverage=1.0,
        reversible_transformation_rate=1.0,
    )
    input_hash = calculate_normalization_input_hash(request)
    payload = RecordNormalizedPayload(
        status=status,
        contract_id=mapping.contract_id,
        upstream_mapping_output_hash=mapping.output_hash,
        record_set_hash=record_set.record_set_hash,
        transformation_set_hash=transformation_set.transformation_set_hash,
        issue_set_hash=issue_set.issue_set_hash,
        field_count=len(fields),
        eligible_count=eligible,
        issue_count=len(issues),
        input_hash=input_hash,
        output_hash="0" * 64,
        idempotency_key=key,
    )
    event = EventEnvelope[RecordNormalizedPayload](
        event_id=calculate_normalization_event_id(key),
        event_type=EventType.RECORD_NORMALIZED,
        task_id=mapping.task_id,
        run_id=mapping.run_id,
        occurred_at=request.runtime.checked_at,
        producer=ProducerRef(
            component="scientific-normalization-service", version=producer_version
        ),
        payload=payload,
        correlation_id=mapping.task_id,
        causation_event_id=mapping.event.event_id,
    )
    result_draft = NormalizationResult.model_validate(
        {
            **metadata,
            "status": status,
            "contract_id": mapping.contract_id,
            "contract_hash": mapping.contract_hash,
            "upstream_mapping_input_hash": mapping.input_hash,
            "upstream_mapping_output_hash": mapping.output_hash,
            "policy": request.policy,
            "policy_hash": calculate_normalization_policy_hash(request.policy),
            "runtime": request.runtime,
            "input_hash": input_hash,
            "output_hash": "0" * 64,
            "idempotency_key": key,
            "record_set": record_set,
            "transformation_set": transformation_set,
            "issue_set": issue_set,
            "metrics": metrics,
            "warnings": tuple(
                f"normalization_issue:{item.code.value}:{item.field_name}" for item in issues
            ),
            "event": event,
        }
    )
    output_hash = calculate_normalization_output_hash(result_draft)
    return NormalizationResult.model_validate(
        result_draft.model_copy(
            update={
                "output_hash": output_hash,
                "event": event.model_copy(
                    update={"payload": payload.model_copy(update={"output_hash": output_hash})}
                ),
            }
        )
    )


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()

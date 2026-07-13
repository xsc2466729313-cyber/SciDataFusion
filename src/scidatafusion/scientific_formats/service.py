"""Idempotent M12 service for deterministic scientific-file parsing."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from threading import RLock

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.datasets import (
    CoordinateIR,
    DatasetIR,
    DatasetParsedPayload,
    FormatMetadata,
    ScalarKind,
    ScientificParsingMetrics,
    ScientificParsingRequest,
    ScientificParsingResult,
    ScientificParsingStatus,
    ScientificQualityReport,
    ScientificScalar,
    TransformationKind,
    TransformationRecord,
    VariableIR,
)
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.scientific_formats.base import RawDataset, RawVariable
from scidatafusion.scientific_formats.checkpoints import (
    MemoryScientificCheckpointStore,
    ScientificCheckpointStore,
)
from scidatafusion.scientific_formats.integrity import (
    calculate_coordinate_hash,
    calculate_dataset_hash,
    calculate_format_metadata_hash,
    calculate_quality_hash,
    calculate_scalar_hash,
    calculate_scientific_event_id,
    calculate_scientific_idempotency_key,
    calculate_scientific_input_hash,
    calculate_scientific_output_hash,
    calculate_scientific_policy_hash,
    calculate_variable_hash,
    verify_scientific_request,
    verify_scientific_result,
)
from scidatafusion.scientific_formats.registry import PluginParserRegistry
from scidatafusion.scientific_formats.storage import DatasetIRStore, MemoryDatasetIRStore


class ScientificParsingService:
    """Parse a bounded scientific dataset with exact replay validation."""

    def __init__(
        self,
        *,
        bronze_store: BronzeByteStore,
        dataset_store: DatasetIRStore | None = None,
        checkpoints: ScientificCheckpointStore | None = None,
        registry: PluginParserRegistry | None = None,
        producer_version: str = "1.0.0",
    ) -> None:
        self._bronze_store = bronze_store
        self._dataset_store = dataset_store or MemoryDatasetIRStore()
        self._checkpoints = checkpoints or MemoryScientificCheckpointStore()
        self._registry = registry or PluginParserRegistry()
        self._producer_version = producer_version
        self._cache: dict[str, ScientificParsingResult] = {}
        self._inflight: dict[str, Future[ScientificParsingResult]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = RLock()

    async def execute(self, request: ScientificParsingRequest) -> ScientificParsingResult:
        """Verify, replay, or execute one cancellation-isolated M12 request."""

        verify_scientific_request(request, self._bronze_store)
        self._registry.resolve(request.runtime)
        key = calculate_scientific_idempotency_key(request, self._producer_version)
        if not request.force_recompute:
            with self._lock:
                cached = self._cache.get(key)
            if cached is not None:
                self._verify_replay(cached, request)
                return cached
            checkpoint = self._checkpoints.load(key)
            if checkpoint is not None:
                self._verify_replay(checkpoint, request)
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
        request: ScientificParsingRequest,
        key: str,
        pending: Future[ScientificParsingResult],
    ) -> None:
        try:
            result = await self._execute_once(request, key)
            result = self._checkpoints.save(result)
            self._verify_replay(result, request)
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
        self, request: ScientificParsingRequest, key: str
    ) -> ScientificParsingResult:
        await asyncio.sleep(0)
        parser = self._registry.resolve(request.runtime)
        raw = parser.parse(
            self._bronze_store.read(request.artifact.byte_sha256),
            request.subset,
            request.policy,
        )
        dataset = _build_dataset(request, raw, self._producer_version)
        reference = self._dataset_store.put(dataset)
        missing = sum(
            item.kind is ScalarKind.MISSING
            for variable in dataset.variables
            for item in variable.values
        )
        transformed = sum(
            item.transformation.kind is TransformationKind.LINEAR_SCALE
            for item in dataset.variables
        )
        quality_draft = ScientificQualityReport(
            selected_variable_coverage=1.0,
            report_hash="0" * 64,
        )
        quality = quality_draft.model_copy(
            update={"report_hash": calculate_quality_hash(quality_draft)}
        )
        metrics = ScientificParsingMetrics(
            input_byte_count=request.artifact.size_bytes,
            hdu_count=raw.hdu_count,
            source_row_count=raw.source_row_count,
            source_variable_count=raw.source_column_count,
            selected_row_count=request.subset.row_stop - request.subset.row_start,
            selected_variable_count=len(dataset.variables),
            materialized_cell_count=sum(len(item.values) for item in dataset.variables),
            missing_value_count=missing,
            transformation_count=transformed,
        )
        input_hash = calculate_scientific_input_hash(request)
        payload = DatasetParsedPayload(
            status=ScientificParsingStatus.SUCCEEDED,
            contract_id=request.artifact.contract_id,
            object_id=request.artifact.object_id,
            route_id=request.artifact.route_id,
            dataset_hash=dataset.dataset_hash,
            quality_report_hash=quality.report_hash,
            input_hash=input_hash,
            output_hash="0" * 64,
            idempotency_key=key,
        )
        event = EventEnvelope[DatasetParsedPayload](
            event_id=calculate_scientific_event_id(key),
            event_type=EventType.DATASET_PARSED,
            task_id=request.artifact.task_id,
            run_id=request.artifact.run_id,
            occurred_at=request.runtime.checked_at,
            producer=ProducerRef(
                component="scientific-format-parser", version=self._producer_version
            ),
            payload=payload,
            correlation_id=request.artifact.contract_id,
        )
        draft = ScientificParsingResult(
            task_id=request.artifact.task_id,
            run_id=request.artifact.run_id,
            contract_version=request.artifact.contract_version,
            created_at=request.runtime.checked_at,
            producer_version=self._producer_version,
            status=ScientificParsingStatus.SUCCEEDED,
            contract_id=request.artifact.contract_id,
            policy=request.policy,
            policy_hash=calculate_scientific_policy_hash(request),
            runtime=request.runtime,
            input_hash=input_hash,
            output_hash="0" * 64,
            idempotency_key=key,
            dataset_ref=reference,
            quality=quality,
            warnings=("first_slice_supports_selected_fits_binary_table_only",),
            metrics=metrics,
            event=event,
        )
        output_hash = calculate_scientific_output_hash(draft)
        return draft.model_copy(
            update={
                "output_hash": output_hash,
                "event": event.model_copy(
                    update={"payload": payload.model_copy(update={"output_hash": output_hash})}
                ),
            }
        )

    def _verify_replay(
        self, result: ScientificParsingResult, request: ScientificParsingRequest
    ) -> None:
        dataset = self._dataset_store.read(result.dataset_ref.artifact_sha256)
        parser = self._registry.resolve(request.runtime)
        raw = parser.parse(
            self._bronze_store.read(request.artifact.byte_sha256),
            request.subset,
            request.policy,
        )
        expected = _build_dataset(request, raw, result.producer_version)
        if dataset != expected:
            from scidatafusion.errors import AppError, ErrorCode

            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "M12 DatasetIR does not replay from immutable FITS bytes",
            )
        verify_scientific_result(result, request, dataset, self._bronze_store)


def _build_dataset(
    request: ScientificParsingRequest, raw: RawDataset, producer_version: str
) -> DatasetIR:
    coordinate_draft = CoordinateIR(
        coordinate_id="cor_" + "0" * 32,
        source_start=request.subset.row_start,
        source_stop=request.subset.row_stop,
        values=tuple(range(request.subset.row_start, request.subset.row_stop)),
        coordinate_hash="0" * 64,
    )
    coordinate_hash = calculate_coordinate_hash(coordinate_draft)
    coordinate = coordinate_draft.model_copy(
        update={"coordinate_id": f"cor_{coordinate_hash[:32]}", "coordinate_hash": coordinate_hash}
    )
    metadata_draft = FormatMetadata(
        metadata_id="fmt_" + "0" * 32,
        format=request.artifact.format,
        hdu_index=raw.hdu_index,
        hdu_name=raw.hdu_name,
        hdu_type="BinTableHDU",
        hdu_count=raw.hdu_count,
        source_row_count=raw.source_row_count,
        source_column_count=raw.source_column_count,
        header_cards=raw.header_cards,
        metadata_hash="0" * 64,
    )
    metadata_hash = calculate_format_metadata_hash(metadata_draft)
    metadata = metadata_draft.model_copy(
        update={"metadata_id": f"fmt_{metadata_hash[:32]}", "metadata_hash": metadata_hash}
    )
    variables = tuple(_variable(item, coordinate.coordinate_id) for item in raw.variables)
    draft = DatasetIR(
        task_id=request.artifact.task_id,
        run_id=request.artifact.run_id,
        contract_version=request.artifact.contract_version,
        created_at=request.runtime.checked_at,
        producer_version=producer_version,
        dataset_id="dsr_" + "0" * 32,
        artifact=request.artifact,
        format_metadata=metadata,
        coordinates=(coordinate,),
        variables=variables,
        dataset_hash="0" * 64,
    )
    value = calculate_dataset_hash(draft)
    return draft.model_copy(update={"dataset_id": f"dsr_{value[:32]}", "dataset_hash": value})


def _variable(raw: RawVariable, coordinate_id: str) -> VariableIR:
    scalars = []
    for item in raw.values:
        scalar_draft = ScientificScalar(
            row_index=item.row_index,
            kind=ScalarKind(item.kind),
            raw_value=item.raw_value,
            physical_value=item.physical_value,
            missing_reason=item.missing_reason,
            scalar_hash="0" * 64,
        )
        scalars.append(
            scalar_draft.model_copy(update={"scalar_hash": calculate_scalar_hash(scalar_draft)})
        )
    kind = (
        TransformationKind.IDENTITY
        if raw.scale_factor == "1" and raw.zero_offset == "0"
        else TransformationKind.LINEAR_SCALE
    )
    variable_draft = VariableIR(
        variable_id="var_" + "0" * 32,
        name=raw.name,
        source_column_index=raw.source_column_index,
        fits_format=raw.fits_format,
        storage_dtype=raw.storage_dtype,
        unit=raw.unit,
        null_marker=raw.null_marker,
        transformation=TransformationRecord(
            kind=kind,
            scale_factor=raw.scale_factor,
            zero_offset=raw.zero_offset,
            formula="physical = raw * scale_factor + zero_offset",
        ),
        coordinate_id=coordinate_id,
        values=tuple(scalars),
        variable_hash="0" * 64,
    )
    value = calculate_variable_hash(variable_draft)
    return variable_draft.model_copy(
        update={"variable_id": f"var_{value[:32]}", "variable_hash": value}
    )

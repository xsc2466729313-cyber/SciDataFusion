"""Idempotent M20 delivery orchestrator."""

from __future__ import annotations

from threading import RLock

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.delivery import (
    DeliveryArtifact,
    DeliveryArtifactKind,
    DeliveryCompletedPayload,
    DeliveryManifest,
    DeliveryMetrics,
    DeliveryRequest,
    DeliveryResult,
    DeliveryStatus,
)
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.quality import QualityStatus
from scidatafusion.delivery.checkpoints import (
    DeliveryCheckpointStore,
    MemoryDeliveryCheckpointStore,
)
from scidatafusion.delivery.exporters import (
    DataDictionaryBuilder,
    ProvenanceExporter,
    ReportGenerator,
    TabularExporter,
    canonical_json_bytes,
)
from scidatafusion.delivery.integrity import (
    calculate_delivery_event_id,
    calculate_delivery_idempotency_key,
    calculate_delivery_input_hash,
    calculate_delivery_manifest_hash,
    calculate_delivery_output_hash,
    calculate_delivery_policy_hash,
    verify_delivery_request,
    verify_delivery_result,
)
from scidatafusion.delivery.packager import NotebookGenerator, ReproducibilityPackager
from scidatafusion.delivery.storage import DeliveryByteStore, MemoryDeliveryStore
from scidatafusion.errors import AppError, ErrorCode


class DeliveryOrchestrator:
    """Create quality-gated, content-addressed exports and a deterministic ZIP."""

    def __init__(
        self,
        *,
        bronze_store: BronzeByteStore,
        delivery_store: DeliveryByteStore | None = None,
        checkpoints: DeliveryCheckpointStore | None = None,
        producer_version: str = "1.0.0",
    ) -> None:
        self._bronze_store = bronze_store
        self._delivery_store = delivery_store or MemoryDeliveryStore()
        self._checkpoints = checkpoints or MemoryDeliveryCheckpointStore()
        self._producer_version = producer_version
        self._cache: dict[str, DeliveryResult] = {}
        self._lock = RLock()

    @property
    def delivery_store(self) -> DeliveryByteStore:
        """Return the immutable byte store used by download boundaries."""

        return self._delivery_store

    async def execute(self, request: DeliveryRequest) -> DeliveryResult:
        """Verify M19 and create or replay one M20 delivery."""

        verify_delivery_request(request, self._bronze_store)
        key = calculate_delivery_idempotency_key(request, self._producer_version)
        if not request.force_recompute:
            with self._lock:
                cached = self._cache.get(key)
            if cached is not None:
                verify_delivery_result(cached, request, self._bronze_store, self._delivery_store)
                return cached
            checkpoint = self._checkpoints.load(key)
            if checkpoint is not None:
                verify_delivery_result(
                    checkpoint, request, self._bronze_store, self._delivery_store
                )
                with self._lock:
                    self._cache[key] = checkpoint
                return checkpoint
        result = self._build(request, key)
        verify_delivery_result(result, request, self._bronze_store, self._delivery_store)
        result = self._checkpoints.save(result)
        with self._lock:
            existing = self._cache.setdefault(key, result)
        if existing != result:
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "M20 idempotency key already has a different result",
            )
        return existing

    def _build(self, request: DeliveryRequest, key: str) -> DeliveryResult:
        knowledge = request.knowledge_result
        quality = request.knowledge_request.quality_result
        contract = request.knowledge_request.quality_request.fusion_request.entity_request.normalization_request.mapping_request.extraction_request.contract
        formal = quality.formal_gold_dataset
        status = (
            DeliveryStatus.SUCCEEDED
            if formal is not None
            else DeliveryStatus.UNSUPPORTED
            if quality.status is QualityStatus.UNSUPPORTED
            else DeliveryStatus.NEEDS_REVIEW
        )
        limitations = (
            ()
            if formal is not None
            else (
                "Formal Gold is unavailable because the upstream quality gate did not pass.",
                "CSV and Parquet are withheld; resolve review items and rerun M18-M20.",
            )
        )
        files: dict[str, bytes] = {}
        files["data_dictionary.json"] = DataDictionaryBuilder().build(contract)
        provenance, provenance_count = ProvenanceExporter().build(formal)
        files["provenance.jsonl"] = provenance
        files["quality_report.json"] = ReportGenerator().build(quality)
        if request.policy.include_evidence_graph:
            files["evidence_graph.json"] = canonical_json_bytes(
                knowledge.graph.model_dump(mode="json")
            )
        if request.policy.include_notebook:
            files["verify_delivery.ipynb"] = NotebookGenerator().build()
        files["reproduction.json"] = canonical_json_bytes(
            {
                "task_id": knowledge.task_id,
                "run_id": knowledge.run_id,
                "contract_id": knowledge.contract_id,
                "contract_version": knowledge.contract_version,
                "contract_hash": knowledge.contract_hash,
                "code_revision": request.runtime.code_revision,
                "producer_version": self._producer_version,
                "parser_version": request.runtime.parser_version,
                "knowledge_input_hash": knowledge.input_hash,
                "knowledge_output_hash": knowledge.output_hash,
                "quality_output_hash": quality.output_hash,
                "model_execution_enabled": False,
                "external_network_enabled": False,
                "known_limitations": limitations,
            }
        )
        consistency = 0.0
        formal_count = 0
        if formal is not None:
            exported = TabularExporter().export(formal, contract)
            formal_count = exported.row_count
            consistency = 1.0
            if request.policy.include_csv:
                files["gold.csv"] = exported.csv_bytes
            if request.policy.include_parquet:
                files["gold.parquet"] = exported.parquet_bytes
        files["run_metrics.json"] = canonical_json_bytes(
            {
                "formal_gold_record_count": formal_count,
                "provenance_record_count": provenance_count,
                "quality_issue_count": quality.metrics.issue_count,
                "csv_parquet_consistency": consistency,
                "notebook_validation_passed": request.policy.include_notebook,
                "scientific_value_mutation_count": 0,
                "model_attempt_count": 0,
                "network_attempt_count": 0,
            }
        )
        artifact_files = tuple(
            self._artifact(request, filename, payload)
            for filename, payload in sorted(files.items())
        )
        manifest_draft = DeliveryManifest(
            manifest_id="dmf_" + "0" * 32,
            task_id=knowledge.task_id,
            run_id=knowledge.run_id,
            contract_id=knowledge.contract_id,
            contract_version=knowledge.contract_version,
            created_at=request.requested_at,
            producer_version=self._producer_version,
            status=status,
            files=artifact_files,
            known_limitations=limitations,
            manifest_hash="0" * 64,
        )
        manifest_hash = calculate_delivery_manifest_hash(manifest_draft)
        manifest = manifest_draft.model_copy(
            update={
                "manifest_id": f"dmf_{manifest_hash[:32]}",
                "manifest_hash": manifest_hash,
            }
        )
        manifest_bytes = canonical_json_bytes(manifest.model_dump(mode="json"))
        package_bytes = ReproducibilityPackager().build(
            files,
            manifest_bytes,
            request.policy.maximum_package_bytes,
        )
        package = self._artifact(request, "scidatafusion-reproduction.zip", package_bytes)
        package = package.model_copy(update={"kind": DeliveryArtifactKind.REPRODUCTION_PACKAGE})
        metrics = DeliveryMetrics(
            formal_gold_record_count=formal_count,
            artifact_count=len(artifact_files) + 1,
            package_entry_count=len(artifact_files) + 1,
            package_size_bytes=len(package_bytes),
            provenance_record_count=provenance_count,
            quality_issue_count=quality.metrics.issue_count,
            csv_parquet_consistency=consistency,
            notebook_validation_passed=request.policy.include_notebook,
        )
        warnings = () if formal is not None else ("quality_gate_failed:tabular_exports_withheld",)
        input_hash = calculate_delivery_input_hash(request)
        payload = DeliveryCompletedPayload(
            status=status,
            contract_id=knowledge.contract_id,
            upstream_knowledge_output_hash=knowledge.output_hash,
            manifest_hash=manifest.manifest_hash,
            package_sha256=package.sha256,
            artifact_count=metrics.artifact_count,
            formal_gold_record_count=formal_count,
            input_hash=input_hash,
            output_hash="0" * 64,
            idempotency_key=key,
        )
        event = EventEnvelope[DeliveryCompletedPayload](
            event_id=calculate_delivery_event_id(key),
            event_type=EventType.DELIVERY_COMPLETED,
            task_id=knowledge.task_id,
            run_id=knowledge.run_id,
            occurred_at=request.requested_at,
            producer=ProducerRef(component="delivery-orchestrator", version=self._producer_version),
            payload=payload,
            correlation_id=knowledge.event.correlation_id,
            causation_event_id=knowledge.event.event_id,
        )
        draft = DeliveryResult(
            task_id=knowledge.task_id,
            run_id=knowledge.run_id,
            contract_id=knowledge.contract_id,
            contract_version=knowledge.contract_version,
            created_at=request.requested_at,
            producer_version=self._producer_version,
            status=status,
            policy=request.policy,
            policy_hash=calculate_delivery_policy_hash(request),
            runtime=request.runtime,
            input_hash=input_hash,
            output_hash="0" * 64,
            idempotency_key=key,
            manifest=manifest,
            package=package,
            warnings=warnings,
            metrics=metrics,
            event=event,
        )
        output_hash = calculate_delivery_output_hash(draft)
        return draft.model_copy(
            update={
                "output_hash": output_hash,
                "event": event.model_copy(
                    update={"payload": payload.model_copy(update={"output_hash": output_hash})}
                ),
            }
        )

    def _artifact(
        self, request: DeliveryRequest, filename: str, payload: bytes
    ) -> DeliveryArtifact:
        sha256 = self._delivery_store.put(payload)
        return DeliveryArtifact(
            artifact_id=f"dlf_{sha256[:32]}",
            task_id=request.knowledge_result.task_id,
            run_id=request.knowledge_result.run_id,
            contract_version=request.knowledge_result.contract_version,
            created_at=request.requested_at,
            producer_version=self._producer_version,
            filename=filename,
            kind=_kind_for(filename),
            media_type=_media_type_for(filename),
            sha256=sha256,
            size_bytes=len(payload),
        )


def _kind_for(filename: str) -> DeliveryArtifactKind:
    if filename == "gold.csv":
        return DeliveryArtifactKind.CSV
    if filename == "gold.parquet":
        return DeliveryArtifactKind.PARQUET
    if filename == "data_dictionary.json":
        return DeliveryArtifactKind.DATA_DICTIONARY
    if filename == "provenance.jsonl":
        return DeliveryArtifactKind.PROVENANCE
    if filename == "quality_report.json":
        return DeliveryArtifactKind.QUALITY_REPORT
    if filename == "evidence_graph.json":
        return DeliveryArtifactKind.EVIDENCE_GRAPH
    if filename.endswith(".ipynb"):
        return DeliveryArtifactKind.NOTEBOOK
    if filename == "run_metrics.json":
        return DeliveryArtifactKind.RUN_METRICS
    if filename == "reproduction.json":
        return DeliveryArtifactKind.REPRODUCTION
    if filename == "scidatafusion-reproduction.zip":
        return DeliveryArtifactKind.REPRODUCTION_PACKAGE
    raise AppError(ErrorCode.VALIDATION_FAILED, f"unsupported M20 artifact filename: {filename}")


def _media_type_for(filename: str) -> str:
    if filename.endswith(".csv"):
        return "text/csv"
    if filename.endswith(".parquet"):
        return "application/vnd.apache.parquet"
    if filename.endswith(".jsonl"):
        return "application/x-ndjson"
    if filename.endswith((".json", ".ipynb")):
        return "application/json"
    if filename.endswith(".zip"):
        return "application/zip"
    return "application/octet-stream"

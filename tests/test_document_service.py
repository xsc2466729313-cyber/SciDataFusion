from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest

from scidatafusion.artifacts.fixtures import build_offline_ia_artifact_bundle
from scidatafusion.artifacts.service import ArtifactDownloadService
from scidatafusion.artifacts.storage import MemoryBronzeStore
from scidatafusion.cli import _build_search_planning, _execute_offline_connectors
from scidatafusion.contracts.artifacts import ArtifactDownloadRequest
from scidatafusion.contracts.documents import (
    DocumentAttemptStatus,
    DocumentCoordinatePrecision,
    DocumentGapCode,
    DocumentIR,
    DocumentPageKind,
    DocumentParserRuntimeDescriptor,
    DocumentParsingRequest,
    DocumentParsingResult,
    DocumentParsingStatus,
    DocumentRouteStatus,
)
from scidatafusion.contracts.parsing import (
    ParsePlanningRequest,
    ParsePlanningResult,
    ParserRoute,
    QualityCheckKind,
)
from scidatafusion.contracts.selection import SourceSelectionRequest
from scidatafusion.document_fixtures import build_ia_document_pdf
from scidatafusion.documents import default_document_adapter_registry
from scidatafusion.documents.adapters import (
    DocumentAdapterError,
    DocumentAdapterErrorCode,
    DocumentAdapterLimits,
    DocumentAdapterRegistry,
    DocumentParserAdapter,
    RawDocument,
)
from scidatafusion.documents.checkpoints import (
    FileSystemDocumentCheckpointStore,
    MemoryDocumentCheckpointStore,
)
from scidatafusion.documents.fixtures import build_offline_document_parsing_bundle
from scidatafusion.documents.integrity import (
    calculate_document_block_hash,
    calculate_document_hash,
    calculate_document_page_hash,
    calculate_document_runtime_hash,
    verify_document_parsing_result_integrity,
)
from scidatafusion.documents.normalizer import normalize_document_ir
from scidatafusion.documents.quality import _bounded_score, evaluate_document_quality
from scidatafusion.documents.service import (
    DocumentParsingService,
    _adapter_limits,
    _pre_execution_attempt_id,
)
from scidatafusion.documents.storage import (
    DocumentIRWriteReceipt,
    FileSystemDocumentIRStore,
    MemoryDocumentIRStore,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.parsing.fixtures import build_offline_parse_planning_bundle
from scidatafusion.parsing.service import ParsePlanningService
from scidatafusion.selection import SourceSelectionService


@dataclass(frozen=True)
class _DocumentChain:
    bronze_store: MemoryBronzeStore
    parse_request: ParsePlanningRequest
    parse_result: ParsePlanningResult
    document_request: DocumentParsingRequest


def _build_chain(*, valid_pdf: bool = False) -> _DocumentChain:
    phase1, planning = _build_search_planning(
        "Study Type Ia supernova light curves using multi-source integration into CSV.",
        "m09-service-tests",
    )
    assert planning is not None
    assert phase1.confirmation is not None
    contract = phase1.confirmation.contract
    connector_result = asyncio.run(_execute_offline_connectors(planning))
    selection_time = max(
        contract.created_at,
        planning.created_at,
        connector_result.created_at,
    ) + timedelta(seconds=1)
    selected = (
        SourceSelectionService(clock=lambda: selection_time)
        .select(
            SourceSelectionRequest(
                contract=contract,
                search_plan=planning.plan,
                connector_result=connector_result,
            )
        )
        .selected_source_set
    )
    artifact_time = selected.created_at + timedelta(seconds=1)
    artifact_bundle = build_offline_ia_artifact_bundle(selected, clock=lambda: artifact_time)
    transport = artifact_bundle.transport
    if valid_pdf:
        fixture_pdf = build_ia_document_pdf()

        async def replace_pdf(request: httpx.Request) -> httpx.Response:
            response = await artifact_bundle.transport.handle_async_request(request)
            media_type = response.headers.get("content-type", "").split(";", 1)[0].casefold()
            if media_type != "application/pdf":
                return response
            headers = {
                key: value
                for key, value in response.headers.items()
                if key.casefold() != "content-length"
            }
            return httpx.Response(
                response.status_code,
                content=fixture_pdf,
                headers=headers,
                request=request,
            )

        transport = httpx.MockTransport(replace_pdf)
    download_request = ArtifactDownloadRequest(
        selected_source_set=selected,
        policy=artifact_bundle.policy,
        runtime=artifact_bundle.runtime,
        approvals=artifact_bundle.approvals,
        requested_at=artifact_time,
    )
    bronze_store = MemoryBronzeStore()
    download_service = ArtifactDownloadService(
        store=bronze_store,
        transport=transport,
        clock=lambda: artifact_time,
    )

    async def download_and_close() -> object:
        try:
            return await download_service.execute(download_request)
        finally:
            await download_service.aclose()

    download_result = asyncio.run(download_and_close())
    parse_time = download_result.created_at + timedelta(seconds=1)  # type: ignore[attr-defined]
    parse_bundle = build_offline_parse_planning_bundle(clock=lambda: parse_time)
    parse_request = ParsePlanningRequest(
        contract=contract,
        download_request=download_request,
        download_result=download_result,  # type: ignore[arg-type]
        capability_registry=parse_bundle.registry,
        policy=parse_bundle.policy,
        runtime=parse_bundle.runtime,
        requested_at=parse_time,
    )
    parse_result = asyncio.run(ParsePlanningService(store=bronze_store).execute(parse_request))
    document_time = parse_result.created_at + timedelta(seconds=1)
    document_bundle = build_offline_document_parsing_bundle(
        parse_result.plan.capability_registry,
        parse_result.plan.runtime,
        clock=lambda: document_time,
    )
    document_request = DocumentParsingRequest(
        parse_planning_request=parse_request,
        parse_planning_result=parse_result,
        policy=document_bundle.policy,
        runtime=document_bundle.runtime,
        requested_at=document_time,
    )
    return _DocumentChain(
        bronze_store=bronze_store,
        parse_request=parse_request,
        parse_result=parse_result,
        document_request=document_request,
    )


@pytest.fixture(scope="module")
def ia_chain() -> _DocumentChain:
    return _build_chain()


@pytest.fixture(scope="module")
def valid_pdf_chain() -> _DocumentChain:
    return _build_chain(valid_pdf=True)


class _CountingAdapter:
    def __init__(self, delegate: DocumentParserAdapter) -> None:
        self._delegate = delegate
        self.calls = 0

    @property
    def parser_id(self) -> str:
        return self._delegate.parser_id

    @property
    def parser_version(self) -> str:
        return self._delegate.parser_version

    @property
    def engine_name(self) -> str:
        return self._delegate.engine_name

    @property
    def engine_version(self) -> str:
        return self._delegate.engine_version

    async def parse(self, content: bytes, *, limits: DocumentAdapterLimits) -> RawDocument:
        self.calls += 1
        return await self._delegate.parse(content, limits=limits)


class _FailingAdapter(_CountingAdapter):
    async def parse(self, content: bytes, *, limits: DocumentAdapterLimits) -> RawDocument:
        del content, limits
        self.calls += 1
        raise AssertionError("checkpoint replay must not invoke a document adapter")


def _counting_registry(
    *,
    failing: bool = False,
) -> tuple[DocumentAdapterRegistry, dict[str, _CountingAdapter]]:
    default = default_document_adapter_registry()
    wrappers: dict[str, _CountingAdapter] = {}
    for parser_id in default.parser_ids:
        delegate = default.require(parser_id)
        wrapper = _FailingAdapter(delegate) if failing else _CountingAdapter(delegate)
        wrappers[parser_id] = wrapper
    return DocumentAdapterRegistry(tuple(wrappers.values())), wrappers


def _execute(
    chain: _DocumentChain,
    *,
    adapters: DocumentAdapterRegistry | None = None,
    document_store: MemoryDocumentIRStore | None = None,
    checkpoints: MemoryDocumentCheckpointStore | None = None,
    request: DocumentParsingRequest | None = None,
) -> DocumentParsingResult:
    service = DocumentParsingService(
        bronze_store=chain.bronze_store,
        adapters=adapters,
        document_store=document_store,
        checkpoints=checkpoints,
    )
    return asyncio.run(service.execute(request or chain.document_request))


def test_ia_chain_retains_html_and_text_and_reviews_empty_pdf(
    ia_chain: _DocumentChain,
) -> None:
    adapters, counters = _counting_registry()
    document_store = MemoryDocumentIRStore()
    result = _execute(ia_chain, adapters=adapters, document_store=document_store)

    assert result.status is DocumentParsingStatus.PARTIAL
    assert result.metrics.eligible_route_count == 3
    assert result.metrics.succeeded_route_count == 2
    assert result.metrics.review_route_count == 1
    assert result.metrics.document_ir_count == 2
    assert result.metrics.attempt_count == 4
    assert result.metrics.fallback_attempt_count == 1
    assert result.metrics.gap_count == 2
    assert result.event.event_type.value == "document.parsed"
    assert {parser_id: item.calls for parser_id, item in counters.items()} == {
        "m09.pdf_text": 1,
        "m09.html": 1,
        "m09.text": 1,
    }

    pdf_route = next(
        item
        for item in result.route_results
        if result.attempts[result.route_results.index(item)].parser_id == "m09.pdf_text"
    )
    pdf_attempts = tuple(item for item in result.attempts if item.route_id == pdf_route.route_id)
    assert pdf_route.status is DocumentRouteStatus.NEEDS_REVIEW
    assert tuple(item.status for item in pdf_attempts) == (
        DocumentAttemptStatus.FAILED,
        DocumentAttemptStatus.BLOCKED,
    )
    assert pdf_attempts[1].parser_id == "m09.pdf_ocr"
    assert pdf_attempts[1].engine_name is None
    assert pdf_attempts[1].engine_version is None
    assert pdf_route.candidate_ids == ()
    assert pdf_route.comparison_id is None
    pdf_gaps = tuple(item for item in result.gaps if item.route_id == pdf_route.route_id)
    assert tuple(item.code for item in pdf_gaps) == (
        DocumentGapCode.ADAPTER_ERROR,
        DocumentGapCode.PARSER_UNAVAILABLE,
    )
    verify_document_parsing_result_integrity(
        result,
        ia_chain.document_request,
        ia_chain.bronze_store,
        document_store,
    )


def test_valid_pdf_fixture_succeeds_with_approximate_page_anchors(
    valid_pdf_chain: _DocumentChain,
) -> None:
    document_store = MemoryDocumentIRStore()
    result = _execute(valid_pdf_chain, document_store=document_store)

    assert result.status is DocumentParsingStatus.SUCCEEDED
    assert result.metrics.succeeded_route_count == 3
    assert result.metrics.document_ir_count == 3
    assert result.metrics.gap_count == 0
    pdf_candidate = next(item for item in result.candidates if item.parser_id == "m09.pdf_text")
    document = document_store.read(pdf_candidate.ir_ref.artifact_sha256)
    assert document.page_count == 2
    assert document.block_count > 0
    assert all(
        anchor.coordinate_precision is DocumentCoordinatePrecision.APPROXIMATE
        for page in document.pages
        for block in page.blocks
        for anchor in block.anchors
        if hasattr(anchor, "coordinate_precision")
    )


def test_repeated_concurrent_and_force_execution_are_idempotent(
    ia_chain: _DocumentChain,
) -> None:
    async def scenario() -> tuple[DocumentParsingResult, dict[str, int]]:
        adapters, counters = _counting_registry()
        service = DocumentParsingService(
            bronze_store=ia_chain.bronze_store,
            adapters=adapters,
        )
        first, follower = await asyncio.gather(
            service.execute(ia_chain.document_request),
            service.execute(ia_chain.document_request),
        )
        replay = await service.execute(ia_chain.document_request)
        forced = await service.execute(
            ia_chain.document_request.model_copy(update={"force_recompute": True})
        )
        assert first is follower is replay is forced
        return first, {parser_id: item.calls for parser_id, item in counters.items()}

    result, calls = asyncio.run(scenario())
    assert result.event.event_id.startswith("evt_")
    assert calls == {"m09.pdf_text": 2, "m09.html": 2, "m09.text": 2}


def test_memory_checkpoint_replays_without_invoking_adapters(
    ia_chain: _DocumentChain,
) -> None:
    checkpoints = MemoryDocumentCheckpointStore()
    document_store = MemoryDocumentIRStore()
    first_adapters, first_counts = _counting_registry()
    first = _execute(
        ia_chain,
        adapters=first_adapters,
        document_store=document_store,
        checkpoints=checkpoints,
    )
    failing_adapters, failing_counts = _counting_registry(failing=True)
    replay = _execute(
        ia_chain,
        adapters=failing_adapters,
        document_store=document_store,
        checkpoints=checkpoints,
    )

    assert replay == first
    assert sum(item.calls for item in first_counts.values()) == 3
    assert sum(item.calls for item in failing_counts.values()) == 0


def test_durable_checkpoint_and_ir_store_replay_and_reject_tampering(
    ia_chain: _DocumentChain,
    tmp_path: Path,
) -> None:
    checkpoints = FileSystemDocumentCheckpointStore(tmp_path / "checkpoints")
    document_store = FileSystemDocumentIRStore(tmp_path / "documents")
    first = asyncio.run(
        DocumentParsingService(
            bronze_store=ia_chain.bronze_store,
            document_store=document_store,
            checkpoints=checkpoints,
        ).execute(ia_chain.document_request)
    )
    failing_adapters, counts = _counting_registry(failing=True)
    replay = asyncio.run(
        DocumentParsingService(
            bronze_store=ia_chain.bronze_store,
            adapters=failing_adapters,
            document_store=document_store,
            checkpoints=checkpoints,
        ).execute(ia_chain.document_request)
    )
    assert replay == first
    assert sum(item.calls for item in counts.values()) == 0

    checkpoint = next((tmp_path / "checkpoints").rglob("*.json"))
    checkpoint.write_text("{}", encoding="utf-8")
    with pytest.raises(AppError) as caught:
        asyncio.run(
            DocumentParsingService(
                bronze_store=ia_chain.bronze_store,
                document_store=document_store,
                checkpoints=checkpoints,
            ).execute(ia_chain.document_request)
        )
    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


class _BlockingDocumentService(DocumentParsingService):
    def __init__(self, *, chain: _DocumentChain, adapters: DocumentAdapterRegistry) -> None:
        super().__init__(bronze_store=chain.bronze_store, adapters=adapters)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def _execute_once(
        self,
        request: DocumentParsingRequest,
        *,
        input_hash: str,
        idempotency_key: str,
    ) -> DocumentParsingResult:
        self.entered.set()
        await self.release.wait()
        return await super()._execute_once(
            request,
            input_hash=input_hash,
            idempotency_key=idempotency_key,
        )


def test_caller_cancellation_does_not_cancel_shared_execution(
    ia_chain: _DocumentChain,
) -> None:
    async def scenario() -> tuple[DocumentParsingResult, int]:
        adapters, counters = _counting_registry()
        service = _BlockingDocumentService(chain=ia_chain, adapters=adapters)
        cancelled_caller = asyncio.create_task(service.execute(ia_chain.document_request))
        await service.entered.wait()
        cancelled_caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_caller
        follower = asyncio.create_task(service.execute(ia_chain.document_request))
        service.release.set()
        result = await follower
        return result, sum(item.calls for item in counters.values())

    result, calls = asyncio.run(scenario())
    assert result.status is DocumentParsingStatus.PARTIAL
    assert calls == 3


class _TamperingAdapter(_CountingAdapter):
    async def parse(self, content: bytes, *, limits: DocumentAdapterLimits) -> RawDocument:
        raw = await super().parse(content, limits=limits)
        return raw.model_copy(update={"content_sha256": "f" * 64})


def test_malicious_adapter_output_becomes_failed_attempt_without_candidate(
    ia_chain: _DocumentChain,
) -> None:
    default = default_document_adapter_registry()
    adapters = tuple(
        _TamperingAdapter(default.require(parser_id))
        if parser_id == "m09.html"
        else _CountingAdapter(default.require(parser_id))
        for parser_id in default.parser_ids
    )
    result = _execute(ia_chain, adapters=DocumentAdapterRegistry(adapters))
    html_attempt = next(item for item in result.attempts if item.parser_id == "m09.html")

    assert html_attempt.status is DocumentAttemptStatus.FAILED
    assert html_attempt.failure_code is DocumentGapCode.INVALID_OUTPUT
    assert html_attempt.candidate_id is None
    assert all(item.parser_id != "m09.html" for item in result.candidates)


class _CorruptingBronzeStore:
    def __init__(self, delegate: MemoryBronzeStore) -> None:
        self._delegate = delegate

    def put(self, content: bytes) -> object:
        return self._delegate.put(content)

    def read(self, byte_sha256: str) -> bytes:
        return self._delegate.read(byte_sha256) + b"tampered"

    def contains(self, byte_sha256: str) -> bool:
        return self._delegate.contains(byte_sha256)


def test_request_or_bronze_tampering_fails_before_adapter_calls(
    ia_chain: _DocumentChain,
) -> None:
    adapters, counters = _counting_registry()
    tampered_runtime = ia_chain.document_request.runtime.model_copy(
        update={"runtime_hash": "f" * 64}
    )
    request = ia_chain.document_request.model_copy(update={"runtime": tampered_runtime})
    with pytest.raises(AppError) as caught:
        asyncio.run(
            DocumentParsingService(
                bronze_store=ia_chain.bronze_store,
                adapters=adapters,
            ).execute(request)
        )
    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR
    assert sum(item.calls for item in counters.values()) == 0

    with pytest.raises(AppError) as caught:
        asyncio.run(
            DocumentParsingService(
                bronze_store=_CorruptingBronzeStore(ia_chain.bronze_store),  # type: ignore[arg-type]
                adapters=adapters,
            ).execute(ia_chain.document_request)
        )
    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR
    assert sum(item.calls for item in counters.values()) == 0


class _FaultingAdapter(_CountingAdapter):
    def __init__(self, delegate: DocumentParserAdapter, mode: str) -> None:
        super().__init__(delegate)
        self._mode = mode

    async def parse(self, content: bytes, *, limits: DocumentAdapterLimits) -> RawDocument:
        del content, limits
        self.calls += 1
        if self._mode == "structured":
            raise DocumentAdapterError(
                DocumentAdapterErrorCode.INVALID_OUTPUT,
                "bounded adapter failure",
            )
        if self._mode == "value":
            raise ValueError("invalid adapter value")
        if self._mode == "runtime":
            raise RuntimeError("unexpected adapter crash")
        return cast(RawDocument, {"unexpected": True})


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("structured", DocumentGapCode.INVALID_OUTPUT),
        ("value", DocumentGapCode.INVALID_OUTPUT),
        ("runtime", DocumentGapCode.ADAPTER_ERROR),
        ("mapping", DocumentGapCode.INVALID_OUTPUT),
    ],
)
def test_adapter_faults_are_bounded_failed_attempts(
    ia_chain: _DocumentChain,
    mode: str,
    expected: DocumentGapCode,
) -> None:
    default = default_document_adapter_registry()
    adapters = tuple(
        _FaultingAdapter(default.require(parser_id), mode)
        if parser_id == "m09.html"
        else _CountingAdapter(default.require(parser_id))
        for parser_id in default.parser_ids
    )

    result = _execute(ia_chain, adapters=DocumentAdapterRegistry(adapters))
    attempt = next(item for item in result.attempts if item.parser_id == "m09.html")

    assert attempt.status is DocumentAttemptStatus.FAILED
    assert attempt.failure_code is expected
    assert attempt.candidate_id is None


class _VersionDriftAdapter(_CountingAdapter):
    @property
    def engine_version(self) -> str:
        return "9.9.9"


def test_policy_document_limit_and_adapter_version_drift_fail_closed(
    ia_chain: _DocumentChain,
) -> None:
    limited_request = DocumentParsingRequest(
        parse_planning_request=ia_chain.parse_request,
        parse_planning_result=ia_chain.parse_result,
        policy=ia_chain.document_request.policy.model_copy(update={"max_documents": 1}),
        runtime=ia_chain.document_request.runtime,
        requested_at=ia_chain.document_request.requested_at,
    )
    with pytest.raises(AppError) as limited:
        asyncio.run(
            DocumentParsingService(bronze_store=ia_chain.bronze_store).execute(limited_request)
        )
    assert limited.value.code is ErrorCode.BUDGET_EXCEEDED

    default = default_document_adapter_registry()
    drifted = tuple(
        _VersionDriftAdapter(default.require(parser_id))
        if parser_id == "m09.html"
        else _CountingAdapter(default.require(parser_id))
        for parser_id in default.parser_ids
    )
    with pytest.raises(AppError) as mismatch:
        asyncio.run(
            DocumentParsingService(
                bronze_store=ia_chain.bronze_store,
                adapters=DocumentAdapterRegistry(drifted),
            ).execute(ia_chain.document_request)
        )
    assert mismatch.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_tiny_output_policy_retains_failures_without_publishing_ir(
    ia_chain: _DocumentChain,
) -> None:
    request = DocumentParsingRequest(
        parse_planning_request=ia_chain.parse_request,
        parse_planning_result=ia_chain.parse_result,
        policy=ia_chain.document_request.policy.model_copy(update={"max_output_bytes": 1}),
        runtime=ia_chain.document_request.runtime,
        requested_at=ia_chain.document_request.requested_at,
    )
    result = _execute(ia_chain, request=request)

    assert result.candidates == ()
    assert any(item.failure_code is DocumentGapCode.LIMIT_EXCEEDED for item in result.attempts)


class _MismatchedReceiptStore(MemoryDocumentIRStore):
    def put(self, document: DocumentIR) -> DocumentIRWriteReceipt:
        receipt = super().put(document)
        wrong_ref = receipt.ir_ref.model_copy(update={"document_hash": "f" * 64})
        return DocumentIRWriteReceipt(ir_ref=wrong_ref, newly_stored=receipt.newly_stored)


def test_document_store_mismatched_receipt_aborts_publication(
    valid_pdf_chain: _DocumentChain,
) -> None:
    with pytest.raises(AppError) as caught:
        asyncio.run(
            DocumentParsingService(
                bronze_store=valid_pdf_chain.bronze_store,
                document_store=_MismatchedReceiptStore(),
            ).execute(valid_pdf_chain.document_request)
        )
    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_primary_parser_removed_from_runtime_is_recorded_blocked(
    ia_chain: _DocumentChain,
) -> None:
    runtime = ia_chain.document_request.runtime
    kept = tuple(item for item in runtime.parser_descriptors if item.parser_id != "m09.text")
    draft = runtime.model_copy(
        update={
            "available_parser_ids": tuple(item.parser_id for item in kept),
            "parser_descriptors": kept,
            "runtime_hash": "0" * 64,
        }
    )
    shrunk = draft.model_copy(update={"runtime_hash": calculate_document_runtime_hash(draft)})
    request = DocumentParsingRequest(
        parse_planning_request=ia_chain.parse_request,
        parse_planning_result=ia_chain.parse_result,
        policy=ia_chain.document_request.policy,
        runtime=shrunk,
        requested_at=ia_chain.document_request.requested_at,
    )

    result = _execute(ia_chain, request=request)
    text_attempt = next(item for item in result.attempts if item.parser_id == "m09.text")
    assert text_attempt.status is DocumentAttemptStatus.BLOCKED
    assert text_attempt.failure_code is DocumentGapCode.PARSER_UNAVAILABLE


def _raw_context(
    chain: _DocumentChain,
    parser_id: str,
) -> tuple[RawDocument, bytes, ParserRoute, DocumentParserRuntimeDescriptor, str]:
    request = chain.document_request
    plan = request.parse_planning_result.plan
    route = next(item for item in plan.routes if item.primary_parser_id == parser_id)
    capability = next(
        item for item in plan.capability_registry.parsers if item.parser_id == parser_id
    )
    descriptor = next(
        item for item in request.runtime.parser_descriptors if item.parser_id == parser_id
    )
    source = next(item for item in plan.source_objects if item.object_id == route.object_id)
    content = chain.bronze_store.read(source.byte_sha256)
    adapter = default_document_adapter_registry().require(parser_id)
    raw = asyncio.run(adapter.parse(content, limits=_adapter_limits(request, route, capability)))
    attempt_id = _pre_execution_attempt_id(
        route,
        capability,
        descriptor=descriptor,
        attempt_number=1,
    )
    return raw, content, route, descriptor, attempt_id


def test_normalizer_rejects_parser_media_policy_span_and_shape_drift(
    ia_chain: _DocumentChain,
) -> None:
    raw, content, route, descriptor, attempt_id = _raw_context(ia_chain, "m09.html")

    document = normalize_document_ir(
        raw,
        content=content,
        request=ia_chain.document_request,
        route=route,
        descriptor=descriptor,
        attempt_id=attempt_id,
        producer_version="1.0.0",
    )
    assert document.block_count == raw.block_count

    parser_drift = raw.model_copy(update={"parser_id": "m09.text"})
    media_drift = raw.model_copy(update={"media_type": "text/plain"})
    fixed_page = raw.pages[0].model_copy(update={"page_kind": DocumentPageKind.FIXED})
    shape_drift = raw.model_copy(update={"pages": (fixed_page,)})

    first_block = raw.pages[0].blocks[0]
    first_span = first_block.byte_spans[0].model_copy(update={"transform_id": "unknown-transform"})
    span_block = first_block.model_copy(
        update={"byte_spans": (first_span, *first_block.byte_spans[1:])}
    )
    span_page = raw.pages[0].model_copy(update={"blocks": (span_block,)})
    span_drift = raw.model_copy(update={"pages": (span_page,)})

    text_block = first_block.model_copy(update={"verbatim_text": "different observation"})
    text_page = raw.pages[0].model_copy(update={"blocks": (text_block,)})
    text_drift = raw.model_copy(update={"pages": (text_page,)})

    for invalid_raw in (parser_drift, media_drift, shape_drift, span_drift, text_drift):
        with pytest.raises(AppError):
            normalize_document_ir(
                invalid_raw,
                content=content,
                request=ia_chain.document_request,
                route=route,
                descriptor=descriptor,
                attempt_id=attempt_id,
                producer_version="1.0.0",
            )

    limited_request = DocumentParsingRequest(
        parse_planning_request=ia_chain.parse_request,
        parse_planning_result=ia_chain.parse_result,
        policy=ia_chain.document_request.policy.model_copy(
            update={"max_text_characters_per_block": 1}
        ),
        runtime=ia_chain.document_request.runtime,
        requested_at=ia_chain.document_request.requested_at,
    )
    with pytest.raises(AppError) as limited:
        normalize_document_ir(
            raw,
            content=content,
            request=limited_request,
            route=route,
            descriptor=descriptor,
            attempt_id=attempt_id,
            producer_version="1.0.0",
        )
    assert limited.value.code is ErrorCode.BUDGET_EXCEEDED


def _document_with_blocks(document: DocumentIR, block_count: int) -> DocumentIR:
    source_block = document.pages[0].blocks[0]
    blocks = []
    for index in range(block_count):
        block_draft = source_block.model_copy(
            update={
                "block_id": f"dbk_{'0' * 32}",
                "block_hash": "0" * 64,
                "reading_order_index": index,
            }
        )
        block_hash = calculate_document_block_hash(block_draft)
        blocks.append(
            block_draft.model_copy(
                update={
                    "block_id": f"dbk_{block_hash[:32]}",
                    "block_hash": block_hash,
                }
            )
        )
    page_draft = document.pages[0].model_copy(
        update={
            "page_id": f"dpg_{'0' * 32}",
            "page_hash": "0" * 64,
            "blocks": tuple(blocks),
        }
    )
    page_hash = calculate_document_page_hash(page_draft)
    page = page_draft.model_copy(
        update={"page_id": f"dpg_{page_hash[:32]}", "page_hash": page_hash}
    )
    document_draft = document.model_copy(
        update={
            "document_id": f"dir_{'0' * 32}",
            "document_hash": "0" * 64,
            "pages": (page,),
            "block_count": len(blocks),
            "text_character_count": sum(len(item.verbatim_text) for item in blocks),
        }
    )
    document_hash = calculate_document_hash(document_draft)
    return DocumentIR.model_validate(
        document_draft.model_copy(
            update={
                "document_id": f"dir_{document_hash[:32]}",
                "document_hash": document_hash,
            }
        ).model_dump()
    )


def test_quality_gates_cover_empty_duplicate_and_foreign_route_cases(
    ia_chain: _DocumentChain,
) -> None:
    store = MemoryDocumentIRStore()
    result = _execute(ia_chain, document_store=store)
    candidate = next(item for item in result.candidates if item.parser_id == "m09.html")
    document = store.read(candidate.ir_ref.artifact_sha256)
    route = next(
        item for item in ia_chain.parse_result.plan.routes if item.route_id == candidate.route_id
    )

    empty = _document_with_blocks(document, 0)
    empty_results = evaluate_document_quality(
        empty,
        route,
        candidate_id=candidate.candidate_id,
    )
    assert (
        next(
            item for item in empty_results if item.kind is QualityCheckKind.TEXT_COVERAGE
        ).observed_score
        == 0.0
    )
    assert (
        next(
            item for item in empty_results if item.kind is QualityCheckKind.READING_ORDER
        ).observed_score
        == 0.0
    )

    duplicate_source = _document_with_blocks(document, 2)
    duplicate_results = evaluate_document_quality(
        duplicate_source,
        route,
        candidate_id=candidate.candidate_id,
    )
    assert (
        next(
            item for item in duplicate_results if item.kind is QualityCheckKind.READING_ORDER
        ).observed_score
        == 0.0
    )

    foreign = route.model_copy(update={"object_id": f"brz_{'0' * 32}"})
    with pytest.raises(AppError, match="exact document route"):
        evaluate_document_quality(
            document,
            foreign,
            candidate_id=candidate.candidate_id,
        )

    unsupported_check = route.quality_checks[0].model_copy(
        update={"kind": QualityCheckKind.TABLE_STRUCTURE}
    )
    unsupported = route.model_copy(update={"quality_checks": (unsupported_check,)})
    with pytest.raises(AppError, match="owned by another"):
        evaluate_document_quality(
            document,
            unsupported,
            candidate_id=candidate.candidate_id,
        )

    with pytest.raises(AppError, match="out-of-range"):
        _bounded_score(2.0)

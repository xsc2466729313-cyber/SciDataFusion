from __future__ import annotations

import asyncio
import copy
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from scidatafusion.artifacts.fixtures import build_offline_ia_artifact_bundle
from scidatafusion.artifacts.service import ArtifactDownloadService
from scidatafusion.artifacts.storage import MemoryBronzeStore
from scidatafusion.cli import _build_search_planning, _execute_offline_connectors
from scidatafusion.contracts.artifacts import ArtifactDownloadRequest
from scidatafusion.contracts.documents import (
    BlockIR,
    ByteSpanSourceAnchor,
    CandidateSelectionStatus,
    DocumentAttemptStatus,
    DocumentBlockKind,
    DocumentCandidateComparison,
    DocumentCoordinatePrecision,
    DocumentCoordinateUnit,
    DocumentExecutionMode,
    DocumentGapCode,
    DocumentIR,
    DocumentIRCandidate,
    DocumentIRRef,
    DocumentPageKind,
    DocumentParseAttempt,
    DocumentParsedPayload,
    DocumentParserRuntimeDescriptor,
    DocumentParsingGap,
    DocumentParsingMetrics,
    DocumentParsingPolicy,
    DocumentParsingRequest,
    DocumentParsingResult,
    DocumentParsingRuntimeSnapshot,
    DocumentParsingStatus,
    DocumentQualityCheckResult,
    DocumentRouteResult,
    DocumentRouteStatus,
    DocumentTextOrigin,
    NormalizedBBox,
    PageIR,
    PageRegionSourceAnchor,
)
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.parsing import (
    ParsePlanningRequest,
    ParsePlanningResult,
    ParserTargetModule,
    ParseScope,
    ParseScopeKind,
    QualityCheckKind,
)
from scidatafusion.contracts.selection import SourceSelectionRequest
from scidatafusion.parsing.fixtures import build_offline_parse_planning_bundle
from scidatafusion.parsing.service import ParsePlanningService
from scidatafusion.selection import SourceSelectionService

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
TASK_ID = f"tsk_{'1' * 32}"
RUN_ID = f"run_{'2' * 32}"
UPSTREAM_EVENT_ID = f"evt_{'3' * 32}"
OBJECT_ID = f"brz_{'4' * 32}"
ROUTE_ID = f"prt_{'5' * 32}"
ATTEMPT_ID = f"dpa_{'6' * 32}"
CANDIDATE_ID = f"dcd_{'7' * 32}"
DOCUMENT_ID = f"dir_{'8' * 32}"
PAGE_ID = f"dpg_{'9' * 32}"
BLOCK_ID = f"dbk_{'a' * 32}"
COMPARISON_ID = f"dcp_{'b' * 32}"
ROUTE_RESULT_ID = f"dre_{'c' * 32}"
PLAN_ID = f"ppl_{'d' * 32}"


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _dump(model: BaseModel) -> dict[str, Any]:
    return copy.deepcopy(model.model_dump(mode="python"))


def _descriptor(
    *,
    parser_id: str = "m09.text",
    parser_version: str = "1.0.0",
    capability_hash: str | None = None,
    engine_name: str = "python.text",
    engine_version: str = "3.11.0",
) -> DocumentParserRuntimeDescriptor:
    return DocumentParserRuntimeDescriptor(
        parser_id=parser_id,
        parser_version=parser_version,
        capability_hash=capability_hash or _hash(f"capability:{parser_id}"),
        engine_name=engine_name,
        engine_version=engine_version,
        descriptor_hash=_hash(
            f"descriptor:{parser_id}:{parser_version}:{engine_name}:{engine_version}"
        ),
    )


@dataclass(frozen=True)
class _M08Snapshot:
    request: ParsePlanningRequest
    result: ParsePlanningResult


@pytest.fixture(scope="module")
def m08_snapshot() -> _M08Snapshot:
    phase1, planning = _build_search_planning(
        "Study Type Ia supernova light curves using multi-source integration into CSV.",
        "m09-contract-tests",
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
    download_request = ArtifactDownloadRequest(
        selected_source_set=selected,
        policy=artifact_bundle.policy,
        runtime=artifact_bundle.runtime,
        approvals=artifact_bundle.approvals,
        requested_at=artifact_time,
    )
    store = MemoryBronzeStore()
    download_service = ArtifactDownloadService(
        store=store,
        transport=artifact_bundle.transport,
        clock=lambda: artifact_time,
    )

    async def download_and_close() -> Any:
        try:
            return await download_service.execute(download_request)
        finally:
            await download_service.aclose()

    download_result = asyncio.run(download_and_close())
    parse_time = download_result.created_at + timedelta(seconds=1)
    parse_bundle = build_offline_parse_planning_bundle(clock=lambda: parse_time)
    request = ParsePlanningRequest(
        contract=contract,
        download_request=download_request,
        download_result=download_result,
        capability_registry=parse_bundle.registry,
        policy=parse_bundle.policy,
        runtime=parse_bundle.runtime,
        requested_at=parse_time,
    )
    result = asyncio.run(ParsePlanningService(store=store).execute(request))
    return _M08Snapshot(request=request, result=result)


def _request_runtime(snapshot: _M08Snapshot) -> DocumentParsingRuntimeSnapshot:
    engine_by_parser = {
        "m09.pdf_text": ("pypdf", "6.14.0"),
        "m09.html": ("html.parser", "3.11.0"),
        "m09.text": ("python.text", "3.11.0"),
    }
    capabilities = {
        item.parser_id: item
        for item in snapshot.result.plan.capability_registry.parsers
        if ParserTargetModule.DOCUMENT in item.target_modules
        and item.parser_id in snapshot.result.plan.runtime.available_parser_ids
        and item.parser_id in engine_by_parser
    }
    descriptors = tuple(
        _descriptor(
            parser_id=parser_id,
            parser_version=capability.parser_version,
            capability_hash=capability.capability_hash,
            engine_name=engine_by_parser[parser_id][0],
            engine_version=engine_by_parser[parser_id][1],
        )
        for parser_id, capability in capabilities.items()
    )
    return DocumentParsingRuntimeSnapshot(
        execution_mode=DocumentExecutionMode.OFFLINE,
        available_parser_ids=tuple(item.parser_id for item in descriptors),
        parser_descriptors=descriptors,
        remaining_cost_micro_usd=snapshot.result.plan.runtime.remaining_cost_micro_usd,
        checked_at=snapshot.result.created_at + timedelta(seconds=1),
        runtime_hash=_hash("m09-runtime"),
    )


def test_request_binds_the_exact_m08_snapshot_and_runtime_descriptors(
    m08_snapshot: _M08Snapshot,
) -> None:
    runtime = _request_runtime(m08_snapshot)
    request = DocumentParsingRequest(
        parse_planning_request=m08_snapshot.request,
        parse_planning_result=m08_snapshot.result,
        runtime=runtime,
        requested_at=runtime.checked_at,
    )

    assert request.parse_planning_result.plan.plan_hash == m08_snapshot.result.plan.plan_hash
    assert set(runtime.available_parser_ids).issubset(
        m08_snapshot.result.plan.runtime.available_parser_ids
    )
    assert tuple(item.parser_id for item in runtime.parser_descriptors) == (
        runtime.available_parser_ids
    )


def test_runtime_rejects_missing_descriptor_and_request_rejects_version_drift(
    m08_snapshot: _M08Snapshot,
) -> None:
    runtime = _request_runtime(m08_snapshot)
    missing_descriptor = _dump(runtime)
    missing_descriptor["parser_descriptors"] = missing_descriptor["parser_descriptors"][:-1]
    with pytest.raises(ValidationError, match="descriptors"):
        DocumentParsingRuntimeSnapshot.model_validate(missing_descriptor)

    drifted_runtime = _dump(runtime)
    drifted_runtime["parser_descriptors"][0]["parser_version"] = "9.9.9"
    drifted_runtime["parser_descriptors"][0]["descriptor_hash"] = _hash("drifted")
    drifted = DocumentParsingRuntimeSnapshot.model_validate(drifted_runtime)
    with pytest.raises(ValidationError, match="adapter versions"):
        DocumentParsingRequest(
            parse_planning_request=m08_snapshot.request,
            parse_planning_result=m08_snapshot.result,
            runtime=drifted,
            requested_at=drifted.checked_at,
        )


def test_request_cannot_reenable_a_parser_unavailable_to_m08(
    m08_snapshot: _M08Snapshot,
) -> None:
    unavailable = next(
        item
        for item in m08_snapshot.result.plan.capability_registry.parsers
        if ParserTargetModule.DOCUMENT in item.target_modules
        and item.parser_id not in m08_snapshot.result.plan.runtime.available_parser_ids
    )
    descriptor = _descriptor(
        parser_id=unavailable.parser_id,
        parser_version=unavailable.parser_version,
        capability_hash=unavailable.capability_hash,
        engine_name="guarded.model",
        engine_version="1.0.0",
    )
    checked_at = m08_snapshot.result.created_at + timedelta(seconds=1)
    runtime = DocumentParsingRuntimeSnapshot(
        execution_mode=DocumentExecutionMode.MOCK,
        available_parser_ids=(descriptor.parser_id,),
        parser_descriptors=(descriptor,),
        model_execution_enabled=True,
        remaining_cost_micro_usd=100_000,
        checked_at=checked_at,
        runtime_hash=_hash("unavailable-runtime"),
    )
    with pytest.raises(ValidationError, match="subset of M08"):
        DocumentParsingRequest(
            parse_planning_request=m08_snapshot.request,
            parse_planning_result=m08_snapshot.result,
            policy=DocumentParsingPolicy(allow_model_execution=True),
            runtime=runtime,
            requested_at=checked_at,
        )


def test_request_rejects_tampered_m08_event_and_extra_fields(
    m08_snapshot: _M08Snapshot,
) -> None:
    runtime = _request_runtime(m08_snapshot)
    payload = m08_snapshot.result.event.payload.model_copy(
        update={"plan_hash": _hash("tampered-plan")}
    )
    event = m08_snapshot.result.event.model_copy(update={"payload": payload})
    result = m08_snapshot.result.model_copy(update={"event": event})
    with pytest.raises(ValidationError, match="event must refer to this M08 result"):
        DocumentParsingRequest(
            parse_planning_request=m08_snapshot.request,
            parse_planning_result=result,
            runtime=runtime,
            requested_at=runtime.checked_at,
        )

    valid = DocumentParsingRequest(
        parse_planning_request=m08_snapshot.request,
        parse_planning_result=m08_snapshot.result,
        runtime=runtime,
        requested_at=runtime.checked_at,
    )
    data = _dump(valid)
    data["scientific_value"] = 12.3
    with pytest.raises(ValidationError, match="Extra inputs"):
        DocumentParsingRequest.model_validate(data)


def _byte_anchor() -> ByteSpanSourceAnchor:
    text = "Observed magnitude 12.3"
    return ByteSpanSourceAnchor(
        object_id=OBJECT_ID,
        byte_sha256=_hash("bronze-bytes"),
        route_id=ROUTE_ID,
        route_hash=_hash("route"),
        parser_attempt_id=ATTEMPT_ID,
        parser_id="m09.text",
        parser_version="1.0.0",
        capability_hash=_hash("capability:m09.text"),
        engine_name="python.text",
        engine_version="3.11.0",
        start_byte=0,
        end_byte=len(text.encode("utf-8")),
        source_slice_sha256=_hash(text),
        encoding="utf-8",
        transform_id="utf8.identity",
        transform_version="1.0.0",
    )


def _block() -> BlockIR:
    text = "Observed magnitude 12.3"
    anchor = _byte_anchor()
    return BlockIR(
        block_id=BLOCK_ID,
        object_id=anchor.object_id,
        byte_sha256=anchor.byte_sha256,
        route_id=anchor.route_id,
        route_hash=anchor.route_hash,
        parser_attempt_id=anchor.parser_attempt_id,
        parser_id=anchor.parser_id,
        parser_version=anchor.parser_version,
        capability_hash=anchor.capability_hash,
        engine_name=anchor.engine_name,
        engine_version=anchor.engine_version,
        page_number=1,
        kind=DocumentBlockKind.PARAGRAPH,
        reading_order_index=0,
        verbatim_text=text,
        verbatim_text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        text_origin=DocumentTextOrigin.DECODED_BYTES,
        confidence=1.0,
        anchors=(anchor,),
        block_hash=_hash("block"),
    )


def _page() -> PageIR:
    block = _block()
    return PageIR(
        page_id=PAGE_ID,
        object_id=block.object_id,
        byte_sha256=block.byte_sha256,
        route_id=block.route_id,
        route_hash=block.route_hash,
        parser_attempt_id=block.parser_attempt_id,
        parser_id=block.parser_id,
        parser_version=block.parser_version,
        capability_hash=block.capability_hash,
        engine_name=block.engine_name,
        engine_version=block.engine_version,
        page_number=1,
        page_kind=DocumentPageKind.REFLOW,
        blocks=(block,),
        page_hash=_hash("page"),
    )


def _document() -> DocumentIR:
    page = _page()
    return DocumentIR(
        task_id=TASK_ID,
        run_id=RUN_ID,
        contract_version="1.0.0",
        created_at=NOW,
        producer_version="1.0.0",
        document_id=DOCUMENT_ID,
        object_id=page.object_id,
        byte_sha256=page.byte_sha256,
        object_metadata_hash=_hash("metadata"),
        acquisition_ids=(f"acq_{'d' * 16}",),
        classification_id=f"cls_{'e' * 32}",
        classification_hash=_hash("classification"),
        upstream_plan_id=PLAN_ID,
        upstream_plan_hash=_hash("upstream-plan"),
        upstream_parse_output_hash=_hash("m08-output"),
        upstream_parse_event_id=UPSTREAM_EVENT_ID,
        route_id=page.route_id,
        route_hash=page.route_hash,
        scope=ParseScope(kind=ParseScopeKind.ARTIFACT),
        parser_attempt_id=page.parser_attempt_id,
        parser_id=page.parser_id,
        parser_version=page.parser_version,
        capability_hash=page.capability_hash,
        engine_name=page.engine_name,
        engine_version=page.engine_version,
        pages=(page,),
        page_count=1,
        block_count=1,
        text_character_count=len(page.blocks[0].verbatim_text),
        document_hash=_hash("document"),
    )


def test_block_ir_retains_verbatim_source_and_forbids_scientific_fields() -> None:
    block = _block()
    assert block.verbatim_text == "Observed magnitude 12.3"

    tampered = _dump(block)
    tampered["verbatim_text"] = "Observed magnitude 99.9"
    with pytest.raises(ValidationError, match="exact UTF-8 hash"):
        BlockIR.model_validate(tampered)

    invented = _dump(block)
    invented["normalized_value"] = 12.3
    invented["gold_value"] = 12.3
    with pytest.raises(ValidationError, match="Extra inputs"):
        BlockIR.model_validate(invented)

    whitespace = _dump(block)
    whitespace_text = "  Observed magnitude 12.3\n"
    whitespace["verbatim_text"] = whitespace_text
    whitespace["verbatim_text_sha256"] = hashlib.sha256(whitespace_text.encode("utf-8")).hexdigest()
    preserved = BlockIR.model_validate(whitespace)
    assert preserved.verbatim_text == whitespace_text


def test_ir_rejects_anchor_engine_drift_and_derived_count_tampering() -> None:
    block_data = _dump(_block())
    block_data["anchors"][0]["engine_version"] = "3.12.0"
    with pytest.raises(ValidationError, match="exact source, route, and parser"):
        BlockIR.model_validate(block_data)

    document_data = _dump(_document())
    document_data["block_count"] = 2
    with pytest.raises(ValidationError, match="counts must be derived"):
        DocumentIR.model_validate(document_data)

    document_data = _dump(_document())
    document_data["pages"][0]["engine_version"] = "3.12.0"
    with pytest.raises(ValidationError, match="exact page, source, route, and parser"):
        DocumentIR.model_validate(document_data)


def test_page_region_requires_precision_and_pypdf_is_always_approximate() -> None:
    region = PageRegionSourceAnchor(
        object_id=OBJECT_ID,
        byte_sha256=_hash("pdf-bytes"),
        route_id=ROUTE_ID,
        route_hash=_hash("pdf-route"),
        parser_attempt_id=ATTEMPT_ID,
        parser_id="m09.pdf_text",
        parser_version="1.0.0",
        capability_hash=_hash("pdf-capability"),
        engine_name="pypdf",
        engine_version="6.14.0",
        page_number=1,
        bbox=NormalizedBBox(left=10, top=20, right=30, bottom=40),
        coordinate_precision=DocumentCoordinatePrecision.APPROXIMATE,
        native_ref_hash=_hash("pdf-native-ref"),
    )
    missing = _dump(region)
    del missing["coordinate_precision"]
    with pytest.raises(ValidationError, match="Field required"):
        PageRegionSourceAnchor.model_validate(missing)

    exact = _dump(region)
    exact["coordinate_precision"] = DocumentCoordinatePrecision.EXACT
    with pytest.raises(ValidationError, match=r"pypdf.*approximate"):
        PageRegionSourceAnchor.model_validate(exact)


def test_coordinates_are_strict_bounded_integer_millionths() -> None:
    with pytest.raises(ValidationError):
        NormalizedBBox.model_validate(
            {"left": 0.0, "top": 0, "right": 1_000_000, "bottom": 1_000_000}
        )
    with pytest.raises(ValidationError, match="positive width"):
        NormalizedBBox(left=50, top=0, right=50, bottom=1)


def _ir_ref() -> DocumentIRRef:
    document = _document()
    artifact_hash = _hash("ir-artifact")
    return DocumentIRRef(
        document_id=document.document_id,
        document_hash=document.document_hash,
        object_id=document.object_id,
        route_id=document.route_id,
        route_hash=document.route_hash,
        parser_id=document.parser_id,
        parser_version=document.parser_version,
        capability_hash=document.capability_hash,
        engine_name=document.engine_name,
        engine_version=document.engine_version,
        artifact_sha256=artifact_hash,
        uri=f"silver://document-ir/sha256/{artifact_hash}",
        size_bytes=2_048,
        page_count=document.page_count,
        block_count=document.block_count,
        text_character_count=document.text_character_count,
    )


def _result() -> DocumentParsingResult:
    ir_ref = _ir_ref()
    candidate_hash = _hash("candidate")
    candidate = DocumentIRCandidate(
        candidate_id=CANDIDATE_ID,
        object_id=OBJECT_ID,
        route_id=ROUTE_ID,
        route_hash=_hash("route"),
        parser_attempt_id=ATTEMPT_ID,
        parser_id="m09.text",
        parser_version="1.0.0",
        capability_hash=_hash("capability:m09.text"),
        engine_name="python.text",
        engine_version="3.11.0",
        ir_ref=ir_ref,
        candidate_hash=candidate_hash,
    )
    quality = DocumentQualityCheckResult(
        quality_result_id=f"dqr_{'1' * 16}",
        route_id=ROUTE_ID,
        parser_attempt_id=ATTEMPT_ID,
        candidate_id=CANDIDATE_ID,
        check_id=f"pqc_{'2' * 16}",
        kind=QualityCheckKind.OUTPUT_SCHEMA,
        minimum_score=1.0,
        observed_score=1.0,
        passed=True,
        algorithm_id="document.schema",
        algorithm_version="1.0.0",
        algorithm_hash=_hash("quality-algorithm"),
        input_document_hash=ir_ref.document_hash,
        measured_page_count=1,
        measured_block_count=1,
        result_hash=_hash("quality-result"),
    )
    attempt_hash = _hash("attempt")
    attempt = DocumentParseAttempt(
        attempt_id=ATTEMPT_ID,
        object_id=OBJECT_ID,
        route_id=ROUTE_ID,
        route_hash=_hash("route"),
        parser_id="m09.text",
        parser_version="1.0.0",
        capability_hash=_hash("capability:m09.text"),
        engine_name="python.text",
        engine_version="3.11.0",
        attempt_number=1,
        status=DocumentAttemptStatus.SUCCEEDED,
        candidate_id=CANDIDATE_ID,
        candidate_hash=candidate_hash,
        quality_results=(quality,),
        actual_cost_micro_usd=0,
        attempt_hash=attempt_hash,
    )
    comparison_hash = _hash("comparison")
    comparison = DocumentCandidateComparison(
        comparison_id=COMPARISON_ID,
        object_id=OBJECT_ID,
        route_id=ROUTE_ID,
        route_hash=_hash("route"),
        candidate_ids=(CANDIDATE_ID,),
        candidate_hashes=(candidate_hash,),
        ranked_candidate_ids=(CANDIDATE_ID,),
        selected_candidate_id=CANDIDATE_ID,
        status=CandidateSelectionStatus.SELECTED,
        comparison_hash=comparison_hash,
    )
    route_result = DocumentRouteResult(
        route_result_id=ROUTE_RESULT_ID,
        object_id=OBJECT_ID,
        route_id=ROUTE_ID,
        route_hash=_hash("route"),
        scope=ParseScope(kind=ParseScopeKind.ARTIFACT),
        status=DocumentRouteStatus.SUCCEEDED,
        attempt_ids=(ATTEMPT_ID,),
        attempt_hashes=(attempt_hash,),
        candidate_ids=(CANDIDATE_ID,),
        candidate_hashes=(candidate_hash,),
        comparison_id=COMPARISON_ID,
        comparison_hash=comparison_hash,
        selected_candidate_id=CANDIDATE_ID,
        gap_ids=(),
        actual_cost_micro_usd=0,
        route_result_hash=_hash("route-result"),
    )
    descriptor = _descriptor()
    runtime = DocumentParsingRuntimeSnapshot(
        execution_mode=DocumentExecutionMode.OFFLINE,
        available_parser_ids=(descriptor.parser_id,),
        parser_descriptors=(descriptor,),
        remaining_cost_micro_usd=100_000,
        checked_at=NOW,
        runtime_hash=_hash("runtime"),
    )
    policy = DocumentParsingPolicy()
    input_hash = _hash("input")
    output_hash = _hash("output")
    idempotency_key = _hash("idempotency")
    route_set_hash = _hash("route-set")
    ir_set_hash = _hash("ir-set")
    policy_hash = _hash("policy")
    metrics = DocumentParsingMetrics(
        eligible_route_count=1,
        succeeded_route_count=1,
        partial_route_count=0,
        review_route_count=0,
        unsupported_route_count=0,
        failed_route_count=0,
        attempt_count=1,
        fallback_attempt_count=0,
        candidate_count=1,
        document_ir_count=1,
        page_count=1,
        block_count=1,
        text_character_count=ir_ref.text_character_count,
        gap_count=0,
        model_attempt_count=0,
        network_attempt_count=0,
        actual_cost_micro_usd=0,
    )
    payload = DocumentParsedPayload(
        status=DocumentParsingStatus.SUCCEEDED,
        upstream_plan_id=PLAN_ID,
        upstream_plan_hash=_hash("upstream-plan"),
        upstream_parse_output_hash=_hash("m08-output"),
        upstream_parse_event_id=UPSTREAM_EVENT_ID,
        policy_hash=policy_hash,
        runtime_hash=runtime.runtime_hash,
        route_result_set_hash=route_set_hash,
        ir_set_hash=ir_set_hash,
        route_count=1,
        document_ir_count=1,
        attempt_count=1,
        gap_count=0,
        input_hash=input_hash,
        output_hash=output_hash,
        idempotency_key=idempotency_key,
    )
    event = EventEnvelope[DocumentParsedPayload](
        event_id=f"evt_{'f' * 32}",
        event_type=EventType.DOCUMENT_PARSED,
        task_id=TASK_ID,
        run_id=RUN_ID,
        occurred_at=NOW,
        schema_version="1.0.0",
        producer=ProducerRef(component="document_parsing_service", version="1.0.0"),
        payload=payload,
        correlation_id=input_hash,
        causation_event_id=UPSTREAM_EVENT_ID,
    )
    return DocumentParsingResult(
        task_id=TASK_ID,
        run_id=RUN_ID,
        contract_version="1.0.0",
        created_at=NOW,
        producer_version="1.0.0",
        status=DocumentParsingStatus.SUCCEEDED,
        upstream_parse_input_hash=_hash("m08-input"),
        upstream_parse_output_hash=_hash("m08-output"),
        upstream_plan_id=PLAN_ID,
        upstream_plan_hash=_hash("upstream-plan"),
        upstream_parse_event_id=UPSTREAM_EVENT_ID,
        policy=policy,
        policy_hash=policy_hash,
        runtime=runtime,
        input_hash=input_hash,
        output_hash=output_hash,
        idempotency_key=idempotency_key,
        route_result_set_hash=route_set_hash,
        ir_set_hash=ir_set_hash,
        route_results=(route_result,),
        attempts=(attempt,),
        candidates=(candidate,),
        comparisons=(comparison,),
        gaps=(),
        warnings=(),
        metrics=metrics,
        event=event,
    )


def test_result_closes_candidate_attempt_runtime_and_event_lineage() -> None:
    result = _result()
    assert result.status is DocumentParsingStatus.SUCCEEDED
    assert result.event.event_type is EventType.DOCUMENT_PARSED
    assert result.metrics.text_character_count == len("Observed magnitude 12.3")


def test_quality_pass_and_result_metrics_are_derived() -> None:
    result = _result()
    quality = _dump(result.attempts[0].quality_results[0])
    quality["passed"] = False
    with pytest.raises(ValidationError, match="pass state must be derived"):
        DocumentQualityCheckResult.model_validate(quality)

    data = _dump(result)
    data["metrics"]["block_count"] = 2
    with pytest.raises(ValidationError, match="result-derived"):
        DocumentParsingResult.model_validate(data)


def test_result_rejects_engine_version_drift_across_candidate_and_runtime() -> None:
    result = _result()
    data = _dump(result)
    data["candidates"][0]["engine_version"] = "3.12.0"
    data["candidates"][0]["ir_ref"]["engine_version"] = "3.12.0"
    with pytest.raises(ValidationError, match="resolve their parser attempts"):
        DocumentParsingResult.model_validate(data)

    data = _dump(result)
    data["attempts"][0]["engine_version"] = "3.12.0"
    data["candidates"][0]["engine_version"] = "3.12.0"
    data["candidates"][0]["ir_ref"]["engine_version"] = "3.12.0"
    with pytest.raises(ValidationError, match="runtime adapter and engine"):
        DocumentParsingResult.model_validate(data)


def test_result_rejects_event_and_selection_tampering() -> None:
    result = _result()
    data = _dump(result)
    data["event"]["payload"]["output_hash"] = _hash("different-output")
    with pytest.raises(ValidationError, match="event must exactly reference"):
        DocumentParsingResult.model_validate(data)

    comparison = _dump(result.comparisons[0])
    comparison["ranked_candidate_ids"] = (f"dcd_{'0' * 32}",)
    with pytest.raises(ValidationError, match="permutation"):
        DocumentCandidateComparison.model_validate(comparison)


def test_document_ir_reference_uri_is_content_addressed() -> None:
    data = _dump(_ir_ref())
    data["uri"] = f"silver://document-ir/sha256/{_hash('wrong-artifact')}"
    with pytest.raises(ValidationError, match="URI must match"):
        DocumentIRRef.model_validate(data)


def test_blocked_attempt_forbids_fabricated_engine_identity() -> None:
    data = _dump(_result().attempts[0])
    data.update(
        {
            "status": DocumentAttemptStatus.BLOCKED,
            "candidate_id": None,
            "candidate_hash": None,
            "quality_results": (),
            "failure_code": DocumentGapCode.PARSER_UNAVAILABLE,
            "failure_detail": "declared fallback is unavailable",
            "engine_name": None,
            "engine_version": None,
            "attempt_hash": _hash("blocked-attempt"),
        }
    )
    blocked = DocumentParseAttempt.model_validate(data)
    assert blocked.engine_name is None
    assert blocked.engine_version is None

    fabricated = copy.deepcopy(data)
    fabricated["engine_name"] = "unavailable.engine"
    fabricated["engine_version"] = "0.0.0"
    with pytest.raises(ValidationError, match="blocked attempts forbid"):
        DocumentParseAttempt.model_validate(fabricated)

    executed = _dump(_result().attempts[0])
    executed["engine_name"] = None
    executed["engine_version"] = None
    with pytest.raises(ValidationError, match="executed M09 attempts require"):
        DocumentParseAttempt.model_validate(executed)


@pytest.mark.parametrize(
    "values",
    [
        {"max_blocks_per_page": 2, "max_total_blocks": 1},
        {"max_text_characters_per_block": 2, "max_total_text_characters": 1},
        {"allow_ocr": True},
        {"allow_vlm": True},
        {"allow_external_network": True},
    ],
)
def test_document_policy_rejects_incoherent_limits_and_permissions(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        DocumentParsingPolicy(**values)  # type: ignore[arg-type]


def test_runtime_and_request_timestamp_and_permission_guards(
    m08_snapshot: _M08Snapshot,
) -> None:
    runtime = _request_runtime(m08_snapshot)
    runtime_data = _dump(runtime)
    runtime_data["checked_at"] = NOW.replace(tzinfo=None)
    with pytest.raises(ValidationError, match="timezone"):
        DocumentParsingRuntimeSnapshot.model_validate(runtime_data)

    duplicate_ids = _dump(runtime)
    duplicate_ids["available_parser_ids"] = (
        runtime.available_parser_ids[0],
        runtime.available_parser_ids[0],
    )
    duplicate_ids["parser_descriptors"] = (
        duplicate_ids["parser_descriptors"][0],
        duplicate_ids["parser_descriptors"][0],
    )
    with pytest.raises(ValidationError, match="unique"):
        DocumentParsingRuntimeSnapshot.model_validate(duplicate_ids)

    duplicate_hashes = _dump(runtime)
    second = copy.deepcopy(duplicate_hashes["parser_descriptors"][0])
    second["parser_id"] = "m09.html"
    duplicate_hashes["available_parser_ids"] = (
        duplicate_hashes["available_parser_ids"][0],
        "m09.html",
    )
    duplicate_hashes["parser_descriptors"] = (
        duplicate_hashes["parser_descriptors"][0],
        second,
    )
    with pytest.raises(ValidationError, match="descriptor hashes"):
        DocumentParsingRuntimeSnapshot.model_validate(duplicate_hashes)

    for mode, model_enabled, network_enabled in (
        (DocumentExecutionMode.MOCK, False, True),
        (DocumentExecutionMode.OFFLINE, True, False),
    ):
        invalid = _dump(runtime)
        invalid.update(
            {
                "execution_mode": mode,
                "model_execution_enabled": model_enabled,
                "external_network_enabled": network_enabled,
            }
        )
        with pytest.raises(ValidationError):
            DocumentParsingRuntimeSnapshot.model_validate(invalid)

    request = DocumentParsingRequest(
        parse_planning_request=m08_snapshot.request,
        parse_planning_result=m08_snapshot.result,
        runtime=runtime,
        requested_at=runtime.checked_at,
    )
    request_data = _dump(request)
    request_data["requested_at"] = NOW.replace(tzinfo=None)
    with pytest.raises(ValidationError, match="timezone"):
        DocumentParsingRequest.model_validate(request_data)

    predating = _dump(request)
    predating["requested_at"] = m08_snapshot.result.created_at
    with pytest.raises(ValidationError, match="predate"):
        DocumentParsingRequest.model_validate(predating)

    blocked_model = _dump(request)
    blocked_model["runtime"]["execution_mode"] = DocumentExecutionMode.MOCK
    blocked_model["runtime"]["model_execution_enabled"] = True
    with pytest.raises(ValidationError, match="model execution is blocked"):
        DocumentParsingRequest.model_validate(blocked_model)


def _page_anchor_for_block(*, rendered: bool = False) -> PageRegionSourceAnchor:
    block = _block()
    return PageRegionSourceAnchor(
        object_id=block.object_id,
        byte_sha256=block.byte_sha256,
        route_id=block.route_id,
        route_hash=block.route_hash,
        parser_attempt_id=block.parser_attempt_id,
        parser_id=block.parser_id,
        parser_version=block.parser_version,
        capability_hash=block.capability_hash,
        engine_name=block.engine_name,
        engine_version=block.engine_version,
        page_number=1,
        bbox=NormalizedBBox(left=1, top=1, right=2, bottom=2),
        coordinate_precision=DocumentCoordinatePrecision.EXACT,
        rendered_page_sha256=_hash("rendered") if rendered else None,
        native_ref_hash=None if rendered else _hash("native"),
    )


def test_anchor_block_page_and_document_negative_invariants() -> None:
    span = _dump(_byte_anchor())
    span["start_byte"] = span["end_byte"]
    with pytest.raises(ValidationError, match="end must be greater"):
        ByteSpanSourceAnchor.model_validate(span)

    region = _dump(_page_anchor_for_block())
    region["native_ref_hash"] = None
    with pytest.raises(ValidationError, match="require a render or native"):
        PageRegionSourceAnchor.model_validate(region)

    block = _dump(_block())
    block["anchors"] = (_dump(_page_anchor_for_block()),)
    with pytest.raises(ValidationError, match="decoded byte text"):
        BlockIR.model_validate(block)

    pdf_block = _dump(_block())
    pdf_block["text_origin"] = DocumentTextOrigin.PDF_TEXT_LAYER
    with pytest.raises(ValidationError, match="PDF text-layer"):
        BlockIR.model_validate(pdf_block)

    ocr_block = _dump(_block())
    ocr_block["text_origin"] = DocumentTextOrigin.OCR_OBSERVATION
    ocr_block["anchors"] = (_dump(_page_anchor_for_block()),)
    with pytest.raises(ValidationError, match="OCR observations"):
        BlockIR.model_validate(ocr_block)

    structural = _dump(_block())
    structural.update(
        {
            "kind": DocumentBlockKind.TABLE_REGION,
            "verbatim_text": "",
            "verbatim_text_sha256": hashlib.sha256(b"").hexdigest(),
            "text_origin": DocumentTextOrigin.NONE,
        }
    )
    with pytest.raises(ValidationError, match="structural regions"):
        BlockIR.model_validate(structural)

    page = _dump(_page())
    page["page_kind"] = DocumentPageKind.FIXED
    with pytest.raises(ValidationError, match="fixed pages require geometry"):
        PageIR.model_validate(page)

    page = _dump(_page())
    page["page_number"] = 2
    page["blocks"][0]["page_number"] = 2
    with pytest.raises(ValidationError, match="synthetic page numbered one"):
        PageIR.model_validate(page)

    page = _dump(_page())
    page["blocks"] = (page["blocks"][0], page["blocks"][0])
    with pytest.raises(ValidationError, match="unique"):
        PageIR.model_validate(page)

    page = _dump(_page())
    page["blocks"][0]["reading_order_index"] = 1
    with pytest.raises(ValidationError, match="contiguous"):
        PageIR.model_validate(page)

    document = _dump(_document())
    document["acquisition_ids"] = (
        document["acquisition_ids"][0],
        document["acquisition_ids"][0],
    )
    with pytest.raises(ValidationError, match="acquisition ids must be unique"):
        DocumentIR.model_validate(document)

    fixed = _dump(_document())
    fixed["scope"] = {"kind": ParseScopeKind.PAGE_RANGE, "start_page": 2, "end_page": 2}
    fixed["pages"][0]["page_kind"] = DocumentPageKind.FIXED
    fixed["pages"][0]["geometry"] = {
        "width": 612.0,
        "height": 792.0,
        "unit": DocumentCoordinateUnit.PDF_POINT,
        "rotation_degrees": 0,
    }
    with pytest.raises(ValidationError, match="cover route scope"):
        DocumentIR.model_validate(fixed)


def test_attempt_comparison_gap_and_route_reference_guards() -> None:
    base_attempt = _dump(_result().attempts[0])
    cases: list[dict[str, Any]] = []

    partial_engine = copy.deepcopy(base_attempt)
    partial_engine["engine_version"] = None
    cases.append(partial_engine)

    missing_candidate = copy.deepcopy(base_attempt)
    missing_candidate["candidate_hash"] = None
    cases.append(missing_candidate)

    failed_without_detail = copy.deepcopy(base_attempt)
    failed_without_detail.update(
        {
            "status": DocumentAttemptStatus.FAILED,
            "candidate_id": None,
            "candidate_hash": None,
            "quality_results": (),
            "failure_code": None,
            "failure_detail": None,
        }
    )
    cases.append(failed_without_detail)

    candidate_with_failure = copy.deepcopy(base_attempt)
    candidate_with_failure.update(
        {
            "failure_code": DocumentGapCode.ADAPTER_ERROR,
            "failure_detail": "cannot accompany a candidate",
        }
    )
    cases.append(candidate_with_failure)

    model_mismatch = copy.deepcopy(base_attempt)
    model_mismatch["model_performed"] = True
    cases.append(model_mismatch)

    quality_drift = copy.deepcopy(base_attempt)
    quality_drift["quality_results"][0]["route_id"] = f"prt_{'0' * 32}"
    cases.append(quality_drift)

    for data in cases:
        with pytest.raises(ValidationError):
            DocumentParseAttempt.model_validate(data)

    comparison = _dump(_result().comparisons[0])
    comparison["candidate_hashes"] = (_hash("first"), _hash("second"))
    with pytest.raises(ValidationError, match="equal length"):
        DocumentCandidateComparison.model_validate(comparison)

    comparison = _dump(_result().comparisons[0])
    comparison["selected_candidate_id"] = f"dcd_{'0' * 32}"
    with pytest.raises(ValidationError, match="first"):
        DocumentCandidateComparison.model_validate(comparison)

    gap_data = {
        "gap_id": f"dgp_{'1' * 16}",
        "code": DocumentGapCode.ADAPTER_ERROR,
        "object_id": OBJECT_ID,
        "route_id": ROUTE_ID,
        "detail": "bounded gap",
    }
    half_scope = {**gap_data, "start_page": 1}
    with pytest.raises(ValidationError, match="bounds must appear together"):
        DocumentParsingGap.model_validate(half_scope)
    reverse_scope = {**gap_data, "start_page": 2, "end_page": 1}
    with pytest.raises(ValidationError, match="cannot precede"):
        DocumentParsingGap.model_validate(reverse_scope)

    route_result = _dump(_result().route_results[0])
    route_result["attempt_hashes"] = (_hash("first"), _hash("second"))
    with pytest.raises(ValidationError, match="equal length"):
        DocumentRouteResult.model_validate(route_result)

    route_result = _dump(_result().route_results[0])
    route_result["comparison_hash"] = None
    with pytest.raises(ValidationError, match="appear together"):
        DocumentRouteResult.model_validate(route_result)

    route_result = _dump(_result().route_results[0])
    route_result["selected_candidate_id"] = f"dcd_{'0' * 32}"
    with pytest.raises(ValidationError, match="must belong"):
        DocumentRouteResult.model_validate(route_result)


def test_aggregate_result_rejects_cross_reference_and_runtime_drift() -> None:
    cases: list[dict[str, Any]] = []

    timestamp = _dump(_result())
    timestamp["created_at"] = NOW + timedelta(seconds=1)
    cases.append(timestamp)

    duplicate_route = _dump(_result())
    duplicate_route["route_results"] = (
        duplicate_route["route_results"][0],
        duplicate_route["route_results"][0],
    )
    cases.append(duplicate_route)

    unknown_attempt = _dump(_result())
    unknown_attempt["route_results"][0]["attempt_ids"] = (f"dpa_{'0' * 32}",)
    cases.append(unknown_attempt)

    wrong_attempt_hash = _dump(_result())
    wrong_attempt_hash["route_results"][0]["attempt_hashes"] = (_hash("wrong"),)
    cases.append(wrong_attempt_hash)

    attempt_number = _dump(_result())
    attempt_number["attempts"][0]["attempt_number"] = 2
    cases.append(attempt_number)

    route_cost = _dump(_result())
    route_cost["route_results"][0]["actual_cost_micro_usd"] = 1
    cases.append(route_cost)

    missing_comparison = _dump(_result())
    missing_comparison["comparisons"] = ()
    cases.append(missing_comparison)

    unavailable = _dump(_result())
    unavailable["runtime"]["available_parser_ids"] = ()
    unavailable["runtime"]["parser_descriptors"] = ()
    cases.append(unavailable)

    model_attempt = _dump(_result())
    model_attempt["attempts"][0].update(
        {
            "model_performed": True,
            "model_invocation_id": f"mdl_{'1' * 32}",
            "model_response_hash": _hash("model-response"),
        }
    )
    model_attempt["metrics"]["model_attempt_count"] = 1
    cases.append(model_attempt)

    network_attempt = _dump(_result())
    network_attempt["attempts"][0]["network_performed"] = True
    network_attempt["metrics"]["network_attempt_count"] = 1
    cases.append(network_attempt)

    for data in cases:
        with pytest.raises(ValidationError):
            DocumentParsingResult.model_validate(data)

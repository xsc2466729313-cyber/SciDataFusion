from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.documents import (
    BlockIR,
    ByteSpanSourceAnchor,
    DocumentAttemptStatus,
    DocumentBlockKind,
    DocumentExecutionMode,
    DocumentGapCode,
    DocumentIR,
    DocumentPageKind,
    DocumentParseAttempt,
    DocumentParsedPayload,
    DocumentParserRuntimeDescriptor,
    DocumentParsingMetrics,
    DocumentParsingPolicy,
    DocumentParsingRequest,
    DocumentParsingResult,
    DocumentParsingRuntimeSnapshot,
    DocumentParsingStatus,
    DocumentTextOrigin,
    PageIR,
)
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.parsing import (
    ParserRoute,
    ParserTargetModule,
    ParseScope,
    ParseScopeKind,
    QualityCheckKind,
    QualityCheckSpec,
    RouteDisposition,
)
from scidatafusion.documents import integrity as integrity_module
from scidatafusion.documents.checkpoints import (
    FileSystemDocumentCheckpointStore,
    MemoryDocumentCheckpointStore,
)
from scidatafusion.documents.integrity import (
    build_document_ir_ref,
    calculate_document_attempt_id,
    calculate_document_block_hash,
    calculate_document_hash,
    calculate_document_id,
    calculate_document_ir_artifact_sha256,
    calculate_document_ir_set_hash,
    calculate_document_page_hash,
    calculate_document_parsed_event_id,
    calculate_document_parser_descriptor_hash,
    calculate_document_parsing_output_hash,
    calculate_document_policy_hash,
    calculate_document_quality_result_hash,
    calculate_document_quality_result_id,
    calculate_document_route_result_set_hash,
    calculate_document_runtime_hash,
    serialize_document_ir,
    verify_document_ir_integrity,
    verify_document_parsing_result_hashes,
    verify_document_parsing_result_integrity,
)
from scidatafusion.documents.quality import evaluate_document_quality
from scidatafusion.documents.storage import (
    DocumentIRStore,
    FileSystemDocumentIRStore,
    MemoryDocumentIRStore,
)
from scidatafusion.errors import AppError, ErrorCode

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
TASK_ID = f"tsk_{'1' * 32}"
RUN_ID = f"run_{'2' * 32}"
OBJECT_ID = f"brz_{'3' * 32}"
ROUTE_ID = f"prt_{'4' * 32}"
PLAN_ID = f"ppl_{'5' * 32}"
UPSTREAM_EVENT_ID = f"evt_{'6' * 32}"
PLACEHOLDER_HASH = "0" * 64


def _hash(value: str | bytes) -> str:
    content = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(content).hexdigest()


def _attempt_id() -> str:
    draft = DocumentParseAttempt(
        attempt_id=f"dpa_{'0' * 32}",
        object_id=OBJECT_ID,
        route_id=ROUTE_ID,
        route_hash=_hash("route"),
        parser_id="m09.text",
        parser_version="1.0.0",
        capability_hash=_hash("text-capability"),
        attempt_number=1,
        status=DocumentAttemptStatus.BLOCKED,
        quality_results=(),
        failure_code=DocumentGapCode.PARSER_UNAVAILABLE,
        failure_detail="Identity-only draft was not executed.",
        actual_cost_micro_usd=0,
        attempt_hash=PLACEHOLDER_HASH,
    )
    return calculate_document_attempt_id(draft)


def _document(*, created_at: datetime = NOW, raw: bytes | None = None) -> DocumentIR:
    source = raw or b"Observed magnitude 12.3"
    text = source.decode("utf-8")
    attempt_id = _attempt_id()
    anchor = ByteSpanSourceAnchor(
        object_id=OBJECT_ID,
        byte_sha256=_hash(source),
        route_id=ROUTE_ID,
        route_hash=_hash("route"),
        parser_attempt_id=attempt_id,
        parser_id="m09.text",
        parser_version="1.0.0",
        capability_hash=_hash("text-capability"),
        engine_name="python.text",
        engine_version="3.11.0",
        start_byte=0,
        end_byte=len(source),
        source_slice_sha256=_hash(source),
        encoding="utf-8",
        transform_id="utf8.identity",
        transform_version="1.0.0",
    )
    block_draft = BlockIR(
        block_id=f"dbk_{'0' * 32}",
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
        verbatim_text_sha256=_hash(text),
        text_origin=DocumentTextOrigin.DECODED_BYTES,
        confidence=1.0,
        anchors=(anchor,),
        block_hash=PLACEHOLDER_HASH,
    )
    block_hash = calculate_document_block_hash(block_draft)
    block = BlockIR.model_validate(
        block_draft.model_copy(
            update={"block_id": f"dbk_{block_hash[:32]}", "block_hash": block_hash}
        ).model_dump(mode="python")
    )
    page_draft = PageIR(
        page_id=f"dpg_{'0' * 32}",
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
        page_hash=PLACEHOLDER_HASH,
    )
    page_hash = calculate_document_page_hash(page_draft)
    page = PageIR.model_validate(
        page_draft.model_copy(
            update={"page_id": f"dpg_{page_hash[:32]}", "page_hash": page_hash}
        ).model_dump(mode="python")
    )
    document_draft = DocumentIR(
        task_id=TASK_ID,
        run_id=RUN_ID,
        contract_version="1.0.0",
        created_at=created_at,
        producer_version="1.0.0",
        document_id=f"dir_{'0' * 32}",
        object_id=OBJECT_ID,
        byte_sha256=_hash(source),
        object_metadata_hash=_hash("object-metadata"),
        acquisition_ids=(f"acq_{'7' * 16}",),
        classification_id=f"cls_{'8' * 32}",
        classification_hash=_hash("classification"),
        upstream_plan_id=PLAN_ID,
        upstream_plan_hash=_hash("plan"),
        upstream_parse_output_hash=_hash("parse-output"),
        upstream_parse_event_id=UPSTREAM_EVENT_ID,
        route_id=ROUTE_ID,
        route_hash=_hash("route"),
        scope=ParseScope(kind=ParseScopeKind.ARTIFACT),
        parser_attempt_id=attempt_id,
        parser_id="m09.text",
        parser_version="1.0.0",
        capability_hash=_hash("text-capability"),
        engine_name="python.text",
        engine_version="3.11.0",
        pages=(page,),
        page_count=1,
        block_count=1,
        text_character_count=len(text),
        document_hash=PLACEHOLDER_HASH,
    )
    document_hash = calculate_document_hash(document_draft)
    return DocumentIR.model_validate(
        document_draft.model_copy(
            update={
                "document_id": f"dir_{document_hash[:32]}",
                "document_hash": document_hash,
            }
        ).model_dump(mode="python")
    )


def _runtime(*, checked_at: datetime) -> DocumentParsingRuntimeSnapshot:
    runtime_draft = DocumentParsingRuntimeSnapshot(
        execution_mode=DocumentExecutionMode.OFFLINE,
        available_parser_ids=(),
        parser_descriptors=(),
        remaining_cost_micro_usd=0,
        checked_at=checked_at,
        runtime_hash=PLACEHOLDER_HASH,
    )
    return runtime_draft.model_copy(
        update={"runtime_hash": calculate_document_runtime_hash(runtime_draft)}
    )


def _metrics() -> DocumentParsingMetrics:
    return DocumentParsingMetrics(
        eligible_route_count=0,
        succeeded_route_count=0,
        partial_route_count=0,
        review_route_count=0,
        unsupported_route_count=0,
        failed_route_count=0,
        attempt_count=0,
        fallback_attempt_count=0,
        candidate_count=0,
        document_ir_count=0,
        page_count=0,
        block_count=0,
        text_character_count=0,
        gap_count=0,
        model_attempt_count=0,
        network_attempt_count=0,
        actual_cost_micro_usd=0,
    )


def _checkpoint_result(
    *,
    created_at: datetime = NOW,
    idempotency_key: str | None = None,
) -> DocumentParsingResult:
    policy = DocumentParsingPolicy()
    policy_hash = calculate_document_policy_hash(policy)
    runtime = _runtime(checked_at=created_at)
    input_hash = _hash("checkpoint-input")
    key = idempotency_key or _hash("checkpoint-idempotency")
    route_set_hash = calculate_document_route_result_set_hash(())
    ir_set_hash = calculate_document_ir_set_hash(())
    payload = DocumentParsedPayload(
        status=DocumentParsingStatus.UNSUPPORTED,
        upstream_plan_id=PLAN_ID,
        upstream_plan_hash=_hash("plan"),
        upstream_parse_output_hash=_hash("parse-output"),
        upstream_parse_event_id=UPSTREAM_EVENT_ID,
        policy_hash=policy_hash,
        runtime_hash=runtime.runtime_hash,
        route_result_set_hash=route_set_hash,
        ir_set_hash=ir_set_hash,
        route_count=0,
        document_ir_count=0,
        attempt_count=0,
        gap_count=0,
        input_hash=input_hash,
        output_hash=PLACEHOLDER_HASH,
        idempotency_key=key,
    )
    event = EventEnvelope[DocumentParsedPayload](
        event_id=calculate_document_parsed_event_id(key),
        event_type=EventType.DOCUMENT_PARSED,
        task_id=TASK_ID,
        run_id=RUN_ID,
        occurred_at=created_at,
        schema_version="1.0.0",
        producer=ProducerRef(component="document_parsing_service", version="1.0.0"),
        payload=payload,
        correlation_id=input_hash,
        causation_event_id=UPSTREAM_EVENT_ID,
    )
    draft = DocumentParsingResult(
        task_id=TASK_ID,
        run_id=RUN_ID,
        contract_version="1.0.0",
        created_at=created_at,
        producer_version="1.0.0",
        status=DocumentParsingStatus.UNSUPPORTED,
        upstream_parse_input_hash=_hash("parse-input"),
        upstream_parse_output_hash=_hash("parse-output"),
        upstream_plan_id=PLAN_ID,
        upstream_plan_hash=_hash("plan"),
        upstream_parse_event_id=UPSTREAM_EVENT_ID,
        policy=policy,
        policy_hash=policy_hash,
        runtime=runtime,
        input_hash=input_hash,
        output_hash=PLACEHOLDER_HASH,
        idempotency_key=key,
        route_result_set_hash=route_set_hash,
        ir_set_hash=ir_set_hash,
        route_results=(),
        attempts=(),
        candidates=(),
        comparisons=(),
        gaps=(),
        warnings=(),
        metrics=_metrics(),
        event=event,
    )
    output_hash = calculate_document_parsing_output_hash(draft)
    final_event = event.model_copy(
        update={"payload": payload.model_copy(update={"output_hash": output_hash})}
    )
    return DocumentParsingResult.model_validate(
        draft.model_copy(update={"output_hash": output_hash, "event": final_event}).model_dump(
            mode="python"
        )
    )


def test_document_ir_hashes_ids_and_canonical_reference_are_stable() -> None:
    document = _document()

    verify_document_ir_integrity(document)
    first = serialize_document_ir(document)
    second = serialize_document_ir(document)
    reference = build_document_ir_ref(document)

    assert first == second
    assert document.document_id == calculate_document_id(document)
    assert reference.document_hash == document.document_hash
    assert reference.artifact_sha256 == calculate_document_ir_artifact_sha256(document)
    assert reference.size_bytes == len(first)
    assert reference.uri.endswith(reference.artifact_sha256)

    tampered = document.model_copy(update={"document_hash": "f" * 64})
    with pytest.raises(AppError) as caught:
        verify_document_ir_integrity(tampered)
    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR

    invalid_counts = document.model_copy(update={"block_count": 2})
    invalid_hash = calculate_document_hash(invalid_counts)
    rehashed_invalid = invalid_counts.model_copy(
        update={
            "document_hash": invalid_hash,
            "document_id": f"dir_{invalid_hash[:32]}",
        }
    )
    with pytest.raises(AppError, match="strict contract revalidation") as caught:
        verify_document_ir_integrity(rehashed_invalid)
    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_memory_document_store_is_content_addressed_bounded_and_concurrent() -> None:
    document = _document()
    store = MemoryDocumentIRStore()
    first = store.put(document)
    second = store.put(document)

    assert first.newly_stored
    assert not second.newly_stored
    assert first.ir_ref == second.ir_ref
    assert store.read(first.artifact_sha256) == document
    assert store.contains(first.artifact_sha256)
    assert not store.contains("f" * 64)

    concurrent = MemoryDocumentIRStore()
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = tuple(pool.map(concurrent.put, (document,) * 16))
    assert sum(item.newly_stored for item in receipts) == 1
    assert {item.artifact_sha256 for item in receipts} == {first.artifact_sha256}

    size_bytes = len(serialize_document_ir(document))
    bounded = MemoryDocumentIRStore(
        max_object_bytes=size_bytes + 1,
        max_total_bytes=size_bytes + 1,
    )
    bounded.put(document)
    distinct_artifact = _document(created_at=NOW + timedelta(seconds=1))
    with pytest.raises(AppError) as capacity:
        bounded.put(distinct_artifact)
    assert capacity.value.code is ErrorCode.BUDGET_EXCEEDED

    too_small = MemoryDocumentIRStore(max_object_bytes=1, max_total_bytes=1)
    with pytest.raises(AppError) as oversized:
        too_small.put(document)
    assert oversized.value.code is ErrorCode.VALIDATION_FAILED


def test_filesystem_document_store_roundtrip_tampering_and_guards(tmp_path: Path) -> None:
    root = tmp_path / "silver"
    store = FileSystemDocumentIRStore(root)
    document = _document()
    receipt = store.put(document)

    assert store.read(receipt.artifact_sha256) == document
    assert not store.put(document).newly_stored
    with pytest.raises(AppError) as invalid:
        store.read("NOT-A-HASH")
    assert invalid.value.code is ErrorCode.INVALID_REQUEST

    target = root / "sha256" / receipt.artifact_sha256[:2] / f"{receipt.artifact_sha256}.json"
    target.write_bytes(b"tampered")
    with pytest.raises(AppError) as tampered:
        store.read(receipt.artifact_sha256)
    assert tampered.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR

    invalid_root = tmp_path / "not-a-directory"
    invalid_root.write_text("blocked", encoding="utf-8")
    with pytest.raises(AppError) as root_error:
        FileSystemDocumentIRStore(invalid_root)
    assert root_error.value.code is ErrorCode.CONFIGURATION_ERROR


def test_filesystem_document_store_rejects_symlink_and_publish_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "symlink-store"
    store = FileSystemDocumentIRStore(root)
    document = _document()
    receipt = store.put(document)
    target = next((root / "sha256").rglob("*.json"))
    outside = tmp_path / "outside.json"
    outside.write_bytes(target.read_bytes())
    target.unlink()
    try:
        target.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable in this environment")
    with pytest.raises(AppError) as symlinked:
        store.read(receipt.artifact_sha256)
    assert symlinked.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR

    target.unlink()

    def fail_link(
        source: str | bytes | os.PathLike[str], destination: str | bytes | os.PathLike[str]
    ) -> None:
        del source, destination
        raise OSError("simulated atomic publication failure")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(AppError) as persistence:
        store.put(document)
    assert persistence.value.code is ErrorCode.INTERNAL_ERROR


def test_memory_checkpoint_is_canonical_idempotent_concurrent_and_conflict_safe() -> None:
    result = _checkpoint_result()
    verify_document_parsing_result_hashes(result)
    store = MemoryDocumentCheckpointStore()

    assert store.save(result) == result
    assert store.save(result) == result
    assert store.load(result.idempotency_key) == result
    assert store.load("f" * 64) is None
    with pytest.raises(AppError) as invalid:
        store.load("NOT-A-HASH")
    assert invalid.value.code is ErrorCode.INVALID_REQUEST

    concurrent = MemoryDocumentCheckpointStore()
    with ThreadPoolExecutor(max_workers=8) as pool:
        saved = tuple(pool.map(concurrent.save, (result,) * 16))
    assert all(item == result for item in saved)

    conflicting = _checkpoint_result(
        created_at=NOW + timedelta(seconds=1),
        idempotency_key=result.idempotency_key,
    )
    with pytest.raises(AppError) as conflict:
        store.save(conflicting)
    assert conflict.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR

    forged = result.model_copy(update={"output_hash": "f" * 64})
    with pytest.raises(AppError) as forged_error:
        MemoryDocumentCheckpointStore().save(forged)
    assert forged_error.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_filesystem_checkpoint_roundtrip_tampering_wrong_key_and_bounds(tmp_path: Path) -> None:
    result = _checkpoint_result()
    root = tmp_path / "checkpoints"
    store = FileSystemDocumentCheckpointStore(root)
    assert store.save(result) == result
    assert store.save(result) == result
    assert store.load(result.idempotency_key) == result

    original = next(root.rglob("*.json"))
    wrong_key = "e" * 64
    wrong_target = root / wrong_key[:2] / f"{wrong_key}.json"
    wrong_target.parent.mkdir(parents=True)
    wrong_target.write_bytes(original.read_bytes())
    with pytest.raises(AppError) as wrong:
        store.load(wrong_key)
    assert wrong.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR

    original.write_text("{}", encoding="utf-8")
    with pytest.raises(AppError) as tampered:
        store.load(result.idempotency_key)
    assert tampered.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR

    bounded = FileSystemDocumentCheckpointStore(
        tmp_path / "bounded",
        max_checkpoint_bytes=1,
    )
    with pytest.raises(AppError) as oversized:
        bounded.save(result)
    assert oversized.value.code is ErrorCode.VALIDATION_FAILED


def test_filesystem_checkpoint_rejects_symlink_and_handles_publish_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _checkpoint_result()
    root = tmp_path / "checkpoint-symlink"
    store = FileSystemDocumentCheckpointStore(root)
    store.save(result)
    target = next(root.rglob("*.json"))
    outside = tmp_path / "checkpoint-outside.json"
    outside.write_bytes(target.read_bytes())
    target.unlink()
    try:
        target.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable in this environment")
    with pytest.raises(AppError) as symlinked:
        store.load(result.idempotency_key)
    assert symlinked.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR

    race_store = FileSystemDocumentCheckpointStore(tmp_path / "race")

    def race_link(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
    ) -> None:
        Path(destination).write_bytes(Path(source).read_bytes())
        raise FileExistsError("simulated concurrent publication")

    monkeypatch.setattr(os, "link", race_link)
    assert race_store.save(result) == result


def test_descriptor_and_runtime_hashes_reject_rehashed_configuration_drift() -> None:
    descriptor_draft = DocumentParserRuntimeDescriptor(
        parser_id="m09.text",
        parser_version="1.0.0",
        capability_hash=_hash("text-capability"),
        engine_name="python.text",
        engine_version="3.11.0",
        descriptor_hash=PLACEHOLDER_HASH,
    )
    descriptor_hash = calculate_document_parser_descriptor_hash(descriptor_draft)
    descriptor = descriptor_draft.model_copy(update={"descriptor_hash": descriptor_hash})
    runtime_draft = DocumentParsingRuntimeSnapshot(
        execution_mode=DocumentExecutionMode.OFFLINE,
        available_parser_ids=(descriptor.parser_id,),
        parser_descriptors=(descriptor,),
        remaining_cost_micro_usd=0,
        checked_at=NOW,
        runtime_hash=PLACEHOLDER_HASH,
    )
    runtime = runtime_draft.model_copy(
        update={"runtime_hash": calculate_document_runtime_hash(runtime_draft)}
    )

    assert descriptor.descriptor_hash == calculate_document_parser_descriptor_hash(descriptor)
    assert runtime.runtime_hash == calculate_document_runtime_hash(runtime)
    drifted = descriptor.model_copy(update={"engine_version": "3.12.0"})
    assert descriptor.descriptor_hash != calculate_document_parser_descriptor_hash(drifted)


def _disable_outer_hash_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate full-verifier semantic checks after the independently tested hash closure."""

    monkeypatch.setattr(
        integrity_module,
        "verify_document_parsing_request_integrity",
        lambda _request, _store: None,
    )
    monkeypatch.setattr(
        integrity_module,
        "verify_document_parsing_result_hashes",
        lambda _result: None,
    )
    monkeypatch.setattr(
        integrity_module,
        "calculate_document_parsing_input_hash",
        lambda _request: _hash("semantic-input"),
    )
    monkeypatch.setattr(
        integrity_module,
        "calculate_document_parsing_idempotency_key",
        lambda _request, _producer_version: _hash("semantic-idempotency"),
    )


class _SingleDocumentReader:
    def __init__(self, document: DocumentIR) -> None:
        self.document = document

    def read(self, artifact_sha256: str) -> DocumentIR:
        assert artifact_sha256 == build_document_ir_ref(self.document).artifact_sha256
        return self.document


class _SingleBronzeReader:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def read(self, byte_sha256: str) -> bytes:
        assert byte_sha256 == _hash(self.content)
        return self.content


def _quality_semantic_context(
    *,
    rehash_tampered_quality: bool,
) -> tuple[
    DocumentParsingRequest,
    DocumentParsingResult,
    BronzeByteStore,
    DocumentIRStore,
]:
    document = _document()
    candidate_id = f"dcd_{'a' * 32}"
    check = QualityCheckSpec(
        check_id=f"pqc_{'b' * 16}",
        kind=QualityCheckKind.OUTPUT_SCHEMA,
        minimum_score=1.0,
    )
    route = SimpleNamespace(
        route_id=document.route_id,
        object_id=document.object_id,
        classification_id=document.classification_id,
        classification_hash=document.classification_hash,
        scope=document.scope,
        disposition=RouteDisposition.PARSE,
        target_module=ParserTargetModule.DOCUMENT,
        primary_parser_id=document.parser_id,
        fallback_parser_ids=(),
        quality_checks=(check,),
        escalation_rules=(),
        route_hash=document.route_hash,
    )
    quality = evaluate_document_quality(
        document,
        cast(ParserRoute, route),
        candidate_id=candidate_id,
    )
    if rehash_tampered_quality:
        quality_draft = quality[0].model_copy(
            update={
                "algorithm_hash": _hash("forged-quality-algorithm"),
                "quality_result_id": f"dqr_{'0' * 16}",
                "result_hash": PLACEHOLDER_HASH,
            }
        )
        quality_hash = calculate_document_quality_result_hash(quality_draft)
        quality = (
            quality_draft.model_copy(
                update={
                    "quality_result_id": calculate_document_quality_result_id(quality_draft),
                    "result_hash": quality_hash,
                }
            ),
        )
    reference = build_document_ir_ref(document)
    candidate_hash = _hash("semantic-candidate")
    candidate = SimpleNamespace(
        candidate_id=candidate_id,
        candidate_hash=candidate_hash,
        object_id=document.object_id,
        route_id=document.route_id,
        route_hash=document.route_hash,
        parser_attempt_id=document.parser_attempt_id,
        parser_id=document.parser_id,
        parser_version=document.parser_version,
        capability_hash=document.capability_hash,
        engine_name=document.engine_name,
        engine_version=document.engine_version,
        ir_ref=reference,
    )
    attempt = SimpleNamespace(
        attempt_id=document.parser_attempt_id,
        parser_id=document.parser_id,
        parser_version=document.parser_version,
        capability_hash=document.capability_hash,
        candidate_id=candidate_id,
        quality_results=quality,
        status=DocumentAttemptStatus.SUCCEEDED,
    )
    route_result = SimpleNamespace(
        route_id=document.route_id,
        route_hash=document.route_hash,
        route_result_id=f"dre_{'c' * 32}",
        object_id=document.object_id,
        scope=document.scope,
        attempt_ids=(document.parser_attempt_id,),
    )
    capability = SimpleNamespace(
        parser_id=document.parser_id,
        parser_version=document.parser_version,
        capability_hash=document.capability_hash,
    )
    source = SimpleNamespace(
        object_id=document.object_id,
        byte_sha256=document.byte_sha256,
        object_metadata_hash=document.object_metadata_hash,
        acquisition_ids=document.acquisition_ids,
    )
    classification = SimpleNamespace(
        classification_id=document.classification_id,
        classification_hash=document.classification_hash,
    )
    plan = SimpleNamespace(
        plan_id=document.upstream_plan_id,
        plan_hash=document.upstream_plan_hash,
        routes=(route,),
        source_objects=(source,),
        classifications=(classification,),
        capability_registry=SimpleNamespace(parsers=(capability,)),
    )
    upstream = SimpleNamespace(
        task_id=document.task_id,
        run_id=document.run_id,
        contract_version=document.contract_version,
        input_hash=_hash("parse-input"),
        output_hash=document.upstream_parse_output_hash,
        plan=plan,
        event=SimpleNamespace(event_id=document.upstream_parse_event_id),
    )
    policy = object()
    runtime = SimpleNamespace(checked_at=document.created_at)
    request = cast(
        DocumentParsingRequest,
        SimpleNamespace(
            parse_planning_request=SimpleNamespace(),
            parse_planning_result=upstream,
            policy=policy,
            runtime=runtime,
        ),
    )
    result = cast(
        DocumentParsingResult,
        SimpleNamespace(
            task_id=document.task_id,
            run_id=document.run_id,
            contract_version=document.contract_version,
            created_at=document.created_at,
            producer_version=document.producer_version,
            input_hash=_hash("semantic-input"),
            idempotency_key=_hash("semantic-idempotency"),
            upstream_parse_input_hash=upstream.input_hash,
            upstream_parse_output_hash=upstream.output_hash,
            upstream_plan_id=plan.plan_id,
            upstream_plan_hash=plan.plan_hash,
            upstream_parse_event_id=upstream.event.event_id,
            policy=policy,
            runtime=runtime,
            route_results=(route_result,),
            attempts=(attempt,),
            candidates=(candidate,),
        ),
    )
    return (
        request,
        result,
        cast(BronzeByteStore, _SingleBronzeReader(b"Observed magnitude 12.3")),
        cast(DocumentIRStore, _SingleDocumentReader(document)),
    )


def test_full_integrity_recomputes_quality_after_attacker_rehashes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_outer_hash_checks(monkeypatch)
    request, result, bronze, documents = _quality_semantic_context(rehash_tampered_quality=False)
    verify_document_parsing_result_integrity(
        result,
        request,
        bronze,
        documents,
    )

    request, forged, bronze, documents = _quality_semantic_context(rehash_tampered_quality=True)
    with pytest.raises(AppError, match="do not reproduce") as caught:
        verify_document_parsing_result_integrity(
            forged,
            request,
            bronze,
            documents,
        )
    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def _fallback_semantic_context(
    *,
    second_trigger_failed: bool,
    first_trigger_failed: bool = True,
    primary_structural_failure: bool = False,
) -> tuple[DocumentParsingRequest, DocumentParsingResult]:
    check_one = QualityCheckSpec(
        check_id=f"pqc_{'1' * 16}",
        kind=QualityCheckKind.TEXT_COVERAGE,
        minimum_score=0.8,
    )
    check_two = QualityCheckSpec(
        check_id=f"pqc_{'2' * 16}",
        kind=QualityCheckKind.READING_ORDER,
        minimum_score=0.8,
    )
    primary_id = f"dpa_{'1' * 32}"
    blocked_id = f"dpa_{'2' * 32}"
    executed_id = f"dpa_{'3' * 32}"
    primary = SimpleNamespace(
        attempt_id=primary_id,
        parser_id="m09.primary",
        parser_version="1.0.0",
        capability_hash=_hash("primary-capability"),
        candidate_id=None,
        quality_results=(
            ()
            if primary_structural_failure
            else (
                SimpleNamespace(check_id=check_one.check_id, passed=not first_trigger_failed),
                SimpleNamespace(check_id=check_two.check_id, passed=not second_trigger_failed),
            )
        ),
        status=(
            DocumentAttemptStatus.FAILED
            if primary_structural_failure
            else DocumentAttemptStatus.QUALITY_FAILED
        ),
    )
    blocked = SimpleNamespace(
        attempt_id=blocked_id,
        parser_id="m09.blocked",
        parser_version="1.0.0",
        capability_hash=_hash("blocked-capability"),
        candidate_id=None,
        quality_results=(),
        status=DocumentAttemptStatus.BLOCKED,
    )
    executed = SimpleNamespace(
        attempt_id=executed_id,
        parser_id="m09.executed",
        parser_version="1.0.0",
        capability_hash=_hash("executed-capability"),
        candidate_id=None,
        quality_results=(),
        status=(
            DocumentAttemptStatus.BLOCKED
            if primary_structural_failure
            else DocumentAttemptStatus.FAILED
        ),
    )
    route = SimpleNamespace(
        route_id=ROUTE_ID,
        object_id=OBJECT_ID,
        classification_id=f"cls_{'8' * 32}",
        scope=ParseScope(kind=ParseScopeKind.ARTIFACT),
        disposition=RouteDisposition.PARSE,
        target_module=ParserTargetModule.DOCUMENT,
        primary_parser_id=primary.parser_id,
        fallback_parser_ids=(blocked.parser_id, executed.parser_id),
        quality_checks=(check_one, check_two),
        escalation_rules=(
            SimpleNamespace(
                fallback_parser_id=blocked.parser_id,
                trigger_check_id=check_one.check_id,
            ),
            SimpleNamespace(
                fallback_parser_id=executed.parser_id,
                trigger_check_id=check_two.check_id,
            ),
        ),
        route_hash=_hash("fallback-route"),
    )
    capabilities = tuple(
        SimpleNamespace(
            parser_id=item.parser_id,
            parser_version=item.parser_version,
            capability_hash=item.capability_hash,
        )
        for item in (primary, blocked, executed)
    )
    plan = SimpleNamespace(
        plan_id=PLAN_ID,
        plan_hash=_hash("fallback-plan"),
        routes=(route,),
        source_objects=(),
        classifications=(),
        capability_registry=SimpleNamespace(parsers=capabilities),
    )
    upstream = SimpleNamespace(
        task_id=TASK_ID,
        run_id=RUN_ID,
        contract_version="1.0.0",
        input_hash=_hash("fallback-parse-input"),
        output_hash=_hash("fallback-parse-output"),
        plan=plan,
        event=SimpleNamespace(event_id=UPSTREAM_EVENT_ID),
    )
    policy = object()
    runtime = SimpleNamespace(checked_at=NOW)
    request = cast(
        DocumentParsingRequest,
        SimpleNamespace(
            parse_planning_request=SimpleNamespace(),
            parse_planning_result=upstream,
            policy=policy,
            runtime=runtime,
        ),
    )
    route_result = SimpleNamespace(
        route_id=route.route_id,
        route_hash=route.route_hash,
        object_id=route.object_id,
        scope=route.scope,
        attempt_ids=(primary_id, blocked_id, executed_id),
    )
    result = cast(
        DocumentParsingResult,
        SimpleNamespace(
            task_id=TASK_ID,
            run_id=RUN_ID,
            contract_version="1.0.0",
            created_at=NOW,
            producer_version="1.0.0",
            input_hash=_hash("semantic-input"),
            idempotency_key=_hash("semantic-idempotency"),
            upstream_parse_input_hash=upstream.input_hash,
            upstream_parse_output_hash=upstream.output_hash,
            upstream_plan_id=plan.plan_id,
            upstream_plan_hash=plan.plan_hash,
            upstream_parse_event_id=upstream.event.event_id,
            policy=policy,
            runtime=runtime,
            route_results=(route_result,),
            attempts=(primary, blocked, executed),
            candidates=(),
        ),
    )
    return request, result


def test_full_integrity_allows_blocked_middle_fallback_but_requires_prior_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_outer_hash_checks(monkeypatch)
    request, result = _fallback_semantic_context(second_trigger_failed=True)
    verify_document_parsing_result_integrity(
        result,
        request,
        cast(BronzeByteStore, object()),
        cast(DocumentIRStore, object()),
    )

    request, structural_failure = _fallback_semantic_context(
        primary_structural_failure=True,
        first_trigger_failed=False,
        second_trigger_failed=False,
    )
    verify_document_parsing_result_integrity(
        structural_failure,
        request,
        cast(BronzeByteStore, object()),
        cast(DocumentIRStore, object()),
    )

    request, untriggered = _fallback_semantic_context(second_trigger_failed=False)
    with pytest.raises(AppError, match="failed quality-check trigger") as caught:
        verify_document_parsing_result_integrity(
            untriggered,
            request,
            cast(BronzeByteStore, object()),
            cast(DocumentIRStore, object()),
        )
    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR

    request, unnecessary_blocked = _fallback_semantic_context(
        first_trigger_failed=False,
        second_trigger_failed=True,
    )
    with pytest.raises(AppError, match="blocked fallback requires") as caught:
        verify_document_parsing_result_integrity(
            unnecessary_blocked,
            request,
            cast(BronzeByteStore, object()),
            cast(DocumentIRStore, object()),
        )
    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_document_store_receipts_configuration_and_decode_edges(tmp_path: Path) -> None:
    document = _document()
    memory = MemoryDocumentIRStore()
    receipt = memory.put(document)

    assert receipt.size_bytes == receipt.ir_ref.size_bytes
    assert receipt.storage_uri == receipt.ir_ref.uri
    assert memory.contains(receipt.artifact_sha256)

    for object_limit, total_limit in ((0, 1), (2, 1), (1, 1_000_000_001)):
        with pytest.raises(AppError) as invalid:
            MemoryDocumentIRStore(
                max_object_bytes=object_limit,
                max_total_bytes=total_limit,
            )
        assert invalid.value.code is ErrorCode.CONFIGURATION_ERROR

    tiny_fs = FileSystemDocumentIRStore(tmp_path / "tiny-fs", max_object_bytes=1)
    with pytest.raises(AppError) as oversized:
        tiny_fs.put(document)
    assert oversized.value.code is ErrorCode.VALIDATION_FAILED

    object_root_file = tmp_path / "object-root-file"
    object_root_file.mkdir()
    (object_root_file / "sha256").write_text("not a directory", encoding="utf-8")
    with pytest.raises(AppError) as object_root:
        FileSystemDocumentIRStore(object_root_file)
    assert object_root.value.code is ErrorCode.CONFIGURATION_ERROR

    invalid_json_root = tmp_path / "invalid-json"
    invalid_json_store = FileSystemDocumentIRStore(invalid_json_root)
    invalid_payload = b"{}"
    invalid_hash = hashlib.sha256(invalid_payload).hexdigest()
    invalid_target = invalid_json_root / "sha256" / invalid_hash[:2] / f"{invalid_hash}.json"
    invalid_target.parent.mkdir(parents=True)
    invalid_target.write_bytes(invalid_payload)
    with pytest.raises(AppError, match="strict contract") as invalid_json:
        invalid_json_store.read(invalid_hash)
    assert invalid_json.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR

    noncanonical_root = tmp_path / "noncanonical"
    noncanonical_store = FileSystemDocumentIRStore(noncanonical_root)
    noncanonical_payload = document.model_dump_json().encode("utf-8")
    assert noncanonical_payload != serialize_document_ir(document)
    noncanonical_hash = hashlib.sha256(noncanonical_payload).hexdigest()
    noncanonical_target = (
        noncanonical_root / "sha256" / noncanonical_hash[:2] / f"{noncanonical_hash}.json"
    )
    noncanonical_target.parent.mkdir(parents=True)
    noncanonical_target.write_bytes(noncanonical_payload)
    with pytest.raises(AppError, match="canonical JSON") as noncanonical:
        noncanonical_store.read(noncanonical_hash)
    assert noncanonical.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_checkpoint_configuration_and_missing_file_edges(tmp_path: Path) -> None:
    for limit in (0, 1_000_000_001):
        with pytest.raises(AppError) as invalid:
            MemoryDocumentCheckpointStore(max_checkpoint_bytes=limit)
        assert invalid.value.code is ErrorCode.CONFIGURATION_ERROR

    missing = FileSystemDocumentCheckpointStore(tmp_path / "missing-checkpoint")
    assert missing.load("f" * 64) is None

    invalid_root = tmp_path / "checkpoint-root-file"
    invalid_root.write_text("not a directory", encoding="utf-8")
    with pytest.raises(AppError) as root_error:
        FileSystemDocumentCheckpointStore(invalid_root)
    assert root_error.value.code is ErrorCode.CONFIGURATION_ERROR

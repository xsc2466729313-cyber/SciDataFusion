"""Canonical M09 hashes and end-to-end document integrity verification."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import TYPE_CHECKING, NoReturn

from pydantic import ValidationError

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.documents import (
    BlockIR,
    ByteSpanSourceAnchor,
    DocumentAttemptStatus,
    DocumentCandidateComparison,
    DocumentGapCode,
    DocumentIR,
    DocumentIRCandidate,
    DocumentIRRef,
    DocumentParseAttempt,
    DocumentParserRuntimeDescriptor,
    DocumentParsingGap,
    DocumentParsingPolicy,
    DocumentParsingRequest,
    DocumentParsingResult,
    DocumentParsingRuntimeSnapshot,
    DocumentQualityCheckResult,
    DocumentRouteResult,
    PageIR,
    SourceAnchor,
)
from scidatafusion.contracts.events import EventType
from scidatafusion.contracts.parsing import ParserTargetModule, RouteDisposition
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.parsing.integrity import verify_parse_planning_integrity

if TYPE_CHECKING:
    from scidatafusion.documents.storage import DocumentIRStore


def serialize_document_ir(document: DocumentIR) -> bytes:
    """Serialize one strict DocumentIR to stable canonical UTF-8 JSON bytes."""

    return _canonical_json_bytes(document.model_dump(mode="json"))


def calculate_document_policy_hash(policy: DocumentParsingPolicy) -> str:
    """Hash every bounded M09 parser, output, escalation, and cost control."""

    return canonical_hash(policy.model_dump(mode="json"))


def calculate_document_parser_descriptor_hash(
    descriptor: DocumentParserRuntimeDescriptor,
) -> str:
    """Hash an exact M09 adapter and engine identity without its self-reference."""

    return canonical_hash(descriptor.model_dump(mode="json", exclude={"descriptor_hash"}))


def calculate_document_runtime_hash(runtime: DocumentParsingRuntimeSnapshot) -> str:
    """Hash the immutable M09 availability, permission, budget, and version snapshot."""

    return canonical_hash(runtime.model_dump(mode="json", exclude={"runtime_hash"}))


def calculate_source_anchor_hash(anchor: SourceAnchor) -> str:
    """Hash one exact byte-span or page-region source observation."""

    return canonical_hash(anchor.model_dump(mode="json"))


def calculate_document_block_hash(block: BlockIR) -> str:
    """Hash one semantic M09 block without its derived hash and identifier."""

    return canonical_hash(block.model_dump(mode="json", exclude={"block_hash", "block_id"}))


def calculate_document_block_id(block: BlockIR) -> str:
    """Derive a stable block identifier from its semantic hash."""

    return f"dbk_{calculate_document_block_hash(block)[:32]}"


def calculate_document_page_hash(page: PageIR) -> str:
    """Hash one ordered page without its derived hash and identifier."""

    return canonical_hash(page.model_dump(mode="json", exclude={"page_hash", "page_id"}))


def calculate_document_page_id(page: PageIR) -> str:
    """Derive a stable page identifier from its semantic hash."""

    return f"dpg_{calculate_document_page_hash(page)[:32]}"


def calculate_document_hash(document: DocumentIR) -> str:
    """Hash semantic DocumentIR content apart from its identity and production timestamp."""

    return canonical_hash(
        document.model_dump(
            mode="json",
            exclude={"created_at", "document_hash", "document_id"},
        )
    )


def calculate_document_id(document: DocumentIR) -> str:
    """Derive a stable DocumentIR identifier from its semantic hash."""

    return f"dir_{calculate_document_hash(document)[:32]}"


def calculate_document_ir_artifact_sha256(document: DocumentIR) -> str:
    """Hash the complete canonical serialized DocumentIR artifact."""

    return hashlib.sha256(serialize_document_ir(document)).hexdigest()


def build_document_ir_ref(document: DocumentIR) -> DocumentIRRef:
    """Build the complete immutable reference for a verified canonical DocumentIR."""

    artifact_sha256 = calculate_document_ir_artifact_sha256(document)
    size_bytes = len(serialize_document_ir(document))
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
        artifact_sha256=artifact_sha256,
        uri=f"silver://document-ir/sha256/{artifact_sha256}",
        size_bytes=size_bytes,
        page_count=document.page_count,
        block_count=document.block_count,
        text_character_count=document.text_character_count,
    )


def calculate_document_ir_ref_hash(reference: DocumentIRRef) -> str:
    """Hash every field in one immutable DocumentIR reference."""

    return canonical_hash(reference.model_dump(mode="json"))


def calculate_document_quality_result_hash(result: DocumentQualityCheckResult) -> str:
    """Hash one deterministic quality observation without its derived identity."""

    return canonical_hash(
        result.model_dump(mode="json", exclude={"quality_result_id", "result_hash"})
    )


def calculate_document_quality_result_id(result: DocumentQualityCheckResult) -> str:
    """Derive a stable quality-result identifier."""

    return f"dqr_{calculate_document_quality_result_hash(result)[:16]}"


def calculate_document_candidate_hash(candidate: DocumentIRCandidate) -> str:
    """Hash one retained parser candidate without its derived identity."""

    return canonical_hash(
        candidate.model_dump(mode="json", exclude={"candidate_hash", "candidate_id"})
    )


def calculate_document_candidate_id(candidate: DocumentIRCandidate) -> str:
    """Derive a stable candidate identifier after its parser-attempt ID is known."""

    return f"dcd_{calculate_document_candidate_hash(candidate)[:32]}"


def calculate_document_attempt_id(attempt: DocumentParseAttempt) -> str:
    """Derive a pre-execution attempt ID without the candidate/attempt hash cycle."""

    value = canonical_hash(
        {
            "attempt_number": attempt.attempt_number,
            "capability_hash": attempt.capability_hash,
            "engine_name": attempt.engine_name,
            "engine_version": attempt.engine_version,
            "object_id": attempt.object_id,
            "parser_id": attempt.parser_id,
            "parser_version": attempt.parser_version,
            "route_hash": attempt.route_hash,
            "route_id": attempt.route_id,
        }
    )
    return f"dpa_{value[:32]}"


def calculate_document_attempt_hash(attempt: DocumentParseAttempt) -> str:
    """Hash the complete append-only attempt record apart from its own hash."""

    return canonical_hash(attempt.model_dump(mode="json", exclude={"attempt_hash"}))


def calculate_document_comparison_hash(comparison: DocumentCandidateComparison) -> str:
    """Hash deterministic candidate ranking without its derived identity."""

    return canonical_hash(
        comparison.model_dump(mode="json", exclude={"comparison_hash", "comparison_id"})
    )


def calculate_document_comparison_id(comparison: DocumentCandidateComparison) -> str:
    """Derive a stable candidate-comparison identifier."""

    return f"dcp_{calculate_document_comparison_hash(comparison)[:32]}"


def calculate_document_gap_id(gap: DocumentParsingGap) -> str:
    """Derive a stable identifier that binds every structured gap field."""

    value = canonical_hash(gap.model_dump(mode="json", exclude={"gap_id"}))
    return f"dgp_{value[:16]}"


def calculate_document_route_result_hash(route_result: DocumentRouteResult) -> str:
    """Hash all attempt, candidate, comparison, gap, and cost references for one route."""

    return canonical_hash(
        route_result.model_dump(
            mode="json",
            exclude={"route_result_hash", "route_result_id"},
        )
    )


def calculate_document_route_result_id(route_result: DocumentRouteResult) -> str:
    """Derive a stable route-result identifier."""

    return f"dre_{calculate_document_route_result_hash(route_result)[:32]}"


def calculate_document_route_result_set_hash(
    route_results: tuple[DocumentRouteResult, ...],
) -> str:
    """Hash the ordered complete set of M09 route-result identities."""

    return canonical_hash([item.route_result_hash for item in route_results])


def calculate_document_ir_set_hash(candidates: tuple[DocumentIRCandidate, ...]) -> str:
    """Hash ordered immutable references for every retained DocumentIR candidate."""

    return canonical_hash([calculate_document_ir_ref_hash(item.ir_ref) for item in candidates])


def calculate_document_parsing_input_hash(request: DocumentParsingRequest) -> str:
    """Bind the exact M08 output, Bronze snapshot, M09 policy, and execution runtime."""

    upstream = request.parse_planning_result
    upstream_request = request.parse_planning_request
    return canonical_hash(
        {
            "artifact_set_hash": upstream.plan.artifact_set_hash,
            "manifest_hash": upstream.plan.manifest_hash,
            "parse_event_id": upstream.event.event_id,
            "parse_input_hash": upstream.input_hash,
            "parse_output_hash": upstream.output_hash,
            "plan_hash": upstream.plan.plan_hash,
            "policy_hash": calculate_document_policy_hash(request.policy),
            "registry_hash": upstream_request.capability_registry.registry_hash,
            "runtime_hash": request.runtime.runtime_hash,
        }
    )


def calculate_document_parsing_idempotency_key(
    request: DocumentParsingRequest,
    producer_version: str,
) -> str:
    """Bind M09 replay identity to task, contract, input, and producer version."""

    return canonical_hash(
        {
            "contract_version": request.parse_planning_result.contract_version,
            "input_hash": calculate_document_parsing_input_hash(request),
            "module_id": "M09",
            "producer_version": producer_version,
            "task_id": request.parse_planning_result.task_id,
        }
    )


def calculate_document_parsing_output_hash(result: DocumentParsingResult) -> str:
    """Hash the complete semantic M09 result while breaking the event output-hash cycle."""

    return canonical_hash(
        {
            "contract_version": result.contract_version,
            "created_at": result.created_at.isoformat(),
            "event": result.event.model_dump(
                mode="json",
                exclude={"payload": {"output_hash"}},
            ),
            "idempotency_key": result.idempotency_key,
            "input_hash": result.input_hash,
            "ir_set_hash": result.ir_set_hash,
            "metrics": result.metrics.model_dump(mode="json"),
            "policy_hash": result.policy_hash,
            "producer_version": result.producer_version,
            "route_result_set_hash": result.route_result_set_hash,
            "run_id": result.run_id,
            "runtime_hash": result.runtime.runtime_hash,
            "status": result.status.value,
            "task_id": result.task_id,
            "upstream_parse_input_hash": result.upstream_parse_input_hash,
            "upstream_parse_output_hash": result.upstream_parse_output_hash,
            "upstream_plan_hash": result.upstream_plan_hash,
            "upstream_plan_id": result.upstream_plan_id,
            "upstream_parse_event_id": result.upstream_parse_event_id,
            "warnings": list(result.warnings),
        }
    )


def calculate_document_parsed_event_id(idempotency_key: str) -> str:
    """Return the deterministic aggregate document.parsed event identifier."""

    return f"evt_{canonical_hash((idempotency_key, 'document-parsed'))[:32]}"


def verify_document_ir_integrity(document: DocumentIR) -> None:
    """Recalculate every nested DocumentIR hash and content-derived identifier."""

    try:
        validated = DocumentIR.model_validate(document.model_dump(mode="python"))
    except ValidationError as exc:
        raise AppError(
            ErrorCode.ARTIFACT_INTEGRITY_ERROR,
            "M09 DocumentIR failed strict contract revalidation",
        ) from exc
    if validated != document:
        _fail("M09 DocumentIR changed during strict contract revalidation")
    for page in document.pages:
        for block in page.blocks:
            block_hash = calculate_document_block_hash(block)
            if not (
                hmac.compare_digest(block.block_hash, block_hash)
                and hmac.compare_digest(block.block_id, f"dbk_{block_hash[:32]}")
            ):
                _fail("M09 block content does not match its immutable hash")
        page_hash = calculate_document_page_hash(page)
        if not (
            hmac.compare_digest(page.page_hash, page_hash)
            and hmac.compare_digest(page.page_id, f"dpg_{page_hash[:32]}")
        ):
            _fail("M09 page content does not match its immutable hash")
    document_hash = calculate_document_hash(document)
    if not (
        hmac.compare_digest(document.document_hash, document_hash)
        and hmac.compare_digest(document.document_id, f"dir_{document_hash[:32]}")
    ):
        _fail("M09 DocumentIR content does not match its immutable hash")


def verify_document_parsing_request_integrity(
    request: DocumentParsingRequest,
    bronze_store: BronzeByteStore,
) -> None:
    """Reject a tampered M08 chain, Bronze byte, policy, or M09 runtime snapshot."""

    try:
        validated = DocumentParsingRequest.model_validate(request.model_dump(mode="python"))
    except ValidationError as exc:
        raise AppError(
            ErrorCode.ARTIFACT_INTEGRITY_ERROR,
            "M09 request failed strict contract revalidation",
        ) from exc
    if validated != request:
        _fail("M09 request changed during strict contract revalidation")
    verify_parse_planning_integrity(
        request.parse_planning_result,
        request.parse_planning_request,
        bronze_store,
    )
    for descriptor in request.runtime.parser_descriptors:
        expected = calculate_document_parser_descriptor_hash(descriptor)
        if not hmac.compare_digest(descriptor.descriptor_hash, expected):
            _fail("M09 parser descriptor does not match its immutable hash")
    if not hmac.compare_digest(
        request.runtime.runtime_hash,
        calculate_document_runtime_hash(request.runtime),
    ):
        _fail("M09 runtime snapshot does not match its immutable hash")


def verify_document_parsing_result_hashes(result: DocumentParsingResult) -> None:
    """Verify the self-contained M09 result hash closure used by checkpoints."""

    try:
        validated = DocumentParsingResult.model_validate(result.model_dump(mode="python"))
    except ValidationError as exc:
        raise AppError(
            ErrorCode.ARTIFACT_INTEGRITY_ERROR,
            "M09 result failed strict contract revalidation",
        ) from exc
    if validated != result:
        _fail("M09 result changed during strict contract revalidation")
    for descriptor in result.runtime.parser_descriptors:
        expected = calculate_document_parser_descriptor_hash(descriptor)
        if not hmac.compare_digest(descriptor.descriptor_hash, expected):
            _fail("M09 result parser descriptor hash is invalid")
    if not (
        hmac.compare_digest(result.policy_hash, calculate_document_policy_hash(result.policy))
        and hmac.compare_digest(
            result.runtime.runtime_hash,
            calculate_document_runtime_hash(result.runtime),
        )
    ):
        _fail("M09 result policy or runtime hash is invalid")

    for candidate in result.candidates:
        candidate_hash = calculate_document_candidate_hash(candidate)
        if not (
            hmac.compare_digest(candidate.candidate_hash, candidate_hash)
            and hmac.compare_digest(candidate.candidate_id, f"dcd_{candidate_hash[:32]}")
        ):
            _fail("M09 candidate content does not match its immutable hash")
    for attempt in result.attempts:
        for quality in attempt.quality_results:
            quality_hash = calculate_document_quality_result_hash(quality)
            if not (
                hmac.compare_digest(quality.result_hash, quality_hash)
                and hmac.compare_digest(
                    quality.quality_result_id,
                    f"dqr_{quality_hash[:16]}",
                )
            ):
                _fail("M09 quality result does not match its immutable hash")
        attempt_hash = calculate_document_attempt_hash(attempt)
        if not (
            hmac.compare_digest(attempt.attempt_hash, attempt_hash)
            and hmac.compare_digest(
                attempt.attempt_id,
                calculate_document_attempt_id(attempt),
            )
        ):
            _fail("M09 parser attempt does not match its immutable identity")
    for comparison in result.comparisons:
        comparison_hash = calculate_document_comparison_hash(comparison)
        if not (
            hmac.compare_digest(comparison.comparison_hash, comparison_hash)
            and hmac.compare_digest(
                comparison.comparison_id,
                f"dcp_{comparison_hash[:32]}",
            )
        ):
            _fail("M09 candidate comparison does not match its immutable hash")
    if any(gap.gap_id != calculate_document_gap_id(gap) for gap in result.gaps):
        _fail("M09 parsing gap does not match its content-derived identifier")
    for route_result in result.route_results:
        route_hash = calculate_document_route_result_hash(route_result)
        if not (
            hmac.compare_digest(route_result.route_result_hash, route_hash)
            and hmac.compare_digest(
                route_result.route_result_id,
                f"dre_{route_hash[:32]}",
            )
        ):
            _fail("M09 route result does not match its immutable hash")

    expected_route_set_hash = calculate_document_route_result_set_hash(result.route_results)
    expected_ir_set_hash = calculate_document_ir_set_hash(result.candidates)
    expected_output_hash = calculate_document_parsing_output_hash(result)
    if not (
        hmac.compare_digest(result.route_result_set_hash, expected_route_set_hash)
        and hmac.compare_digest(result.ir_set_hash, expected_ir_set_hash)
        and hmac.compare_digest(result.output_hash, expected_output_hash)
        and hmac.compare_digest(
            result.event.event_id,
            calculate_document_parsed_event_id(result.idempotency_key),
        )
        and result.event.event_type is EventType.DOCUMENT_PARSED
    ):
        _fail("M09 aggregate result or event does not match its immutable hashes")


def verify_document_parsing_result_integrity(
    result: DocumentParsingResult,
    request: DocumentParsingRequest,
    bronze_store: BronzeByteStore,
    document_store: DocumentIRStore,
) -> None:
    """Verify exact M08/Bronze lineage, persisted IR, result hashes, and event causality."""

    verify_document_parsing_request_integrity(request, bronze_store)
    verify_document_parsing_result_hashes(result)
    upstream = request.parse_planning_result
    plan = upstream.plan
    expected_input_hash = calculate_document_parsing_input_hash(request)
    expected_idempotency_key = calculate_document_parsing_idempotency_key(
        request,
        result.producer_version,
    )
    if not (
        hmac.compare_digest(result.input_hash, expected_input_hash)
        and hmac.compare_digest(result.idempotency_key, expected_idempotency_key)
        and result.task_id == upstream.task_id
        and result.run_id == upstream.run_id
        and result.contract_version == upstream.contract_version
        and result.created_at == request.runtime.checked_at
        and result.upstream_parse_input_hash == upstream.input_hash
        and result.upstream_parse_output_hash == upstream.output_hash
        and result.upstream_plan_id == plan.plan_id
        and result.upstream_plan_hash == plan.plan_hash
        and result.upstream_parse_event_id == upstream.event.event_id
        and result.policy == request.policy
        and result.runtime == request.runtime
    ):
        _fail("M09 result does not match its immutable request and M08 snapshot")

    eligible_routes = tuple(
        route
        for route in plan.routes
        if route.disposition is RouteDisposition.PARSE
        and route.target_module is ParserTargetModule.DOCUMENT
    )
    if tuple(item.route_id for item in result.route_results) != tuple(
        item.route_id for item in eligible_routes
    ):
        _fail("M09 result must account for every and only executable M09 route")
    route_by_id = {item.route_id: item for item in eligible_routes}
    source_by_id = {item.object_id: item for item in plan.source_objects}
    classification_by_id = {item.classification_id: item for item in plan.classifications}
    capability_by_id = {item.parser_id: item for item in plan.capability_registry.parsers}
    attempt_by_id = {item.attempt_id: item for item in result.attempts}
    candidate_by_id = {item.candidate_id: item for item in result.candidates}

    for route_result in result.route_results:
        route = route_by_id[route_result.route_id]
        if not (
            route_result.object_id == route.object_id
            and route_result.route_hash == route.route_hash
            and route_result.scope == route.scope
        ):
            _fail("M09 route result is not an exact M08 route projection")
        if route.primary_parser_id is None:
            _fail("M09 executable route must retain its M08 primary parser")
        parser_sequence = (route.primary_parser_id, *route.fallback_parser_ids)
        route_attempts = tuple(attempt_by_id[item] for item in route_result.attempt_ids)
        if (
            tuple(item.parser_id for item in route_attempts)
            != parser_sequence[: len(route_attempts)]
        ):
            _fail("M09 attempts must follow the declared primary and fallback order")
        for attempt in route_attempts:
            capability = capability_by_id.get(attempt.parser_id)
            if capability is None or not (
                attempt.parser_version == capability.parser_version
                and attempt.capability_hash == capability.capability_hash
            ):
                _fail("M09 attempt does not match its exact M08 parser capability")
            if attempt.candidate_id is not None:
                candidate = candidate_by_id[attempt.candidate_id]
                observed_checks = tuple(
                    (item.check_id, item.kind, item.minimum_score)
                    for item in attempt.quality_results
                )
                planned_checks = tuple(
                    (item.check_id, item.kind, item.minimum_score) for item in route.quality_checks
                )
                if observed_checks != planned_checks or any(
                    item.input_document_hash != candidate.ir_ref.document_hash
                    or item.measured_page_count != candidate.ir_ref.page_count
                    or item.measured_block_count != candidate.ir_ref.block_count
                    for item in attempt.quality_results
                ):
                    _fail("M09 quality results must exactly execute the M08 route checks")
        rule_by_parser = {item.fallback_parser_id: item for item in route.escalation_rules}
        for index, fallback in enumerate(route_attempts[1:], start=1):
            rule = rule_by_parser.get(fallback.parser_id)
            if rule is None:
                _fail("M09 fallback requires its declared escalation rule")
            prior_attempts = route_attempts[:index]
            trigger_failed = any(
                item.check_id == rule.trigger_check_id and not item.passed
                for previous in prior_attempts
                for item in previous.quality_results
            )
            structural_failure = any(
                item.status is DocumentAttemptStatus.FAILED for item in prior_attempts
            )
            skipped_unmatched_trigger = (
                getattr(fallback, "failure_code", None) is DocumentGapCode.QUALITY_UNSATISFIED
                and not trigger_failed
                and any(
                    item.status is DocumentAttemptStatus.QUALITY_FAILED for item in prior_attempts
                )
            )
            if fallback.status is DocumentAttemptStatus.BLOCKED and not (
                trigger_failed or structural_failure or skipped_unmatched_trigger
            ):
                _fail("M09 blocked fallback requires a prior parser failure or declared trigger")
            if fallback.status is not DocumentAttemptStatus.BLOCKED and not trigger_failed:
                _fail("M09 fallback requires its declared failed quality-check trigger")

    for candidate in result.candidates:
        document = document_store.read(candidate.ir_ref.artifact_sha256)
        verify_document_ir_integrity(document)
        if build_document_ir_ref(document) != candidate.ir_ref:
            _fail("M09 candidate reference does not match its persisted DocumentIR")
        candidate_route = route_by_id.get(candidate.route_id)
        candidate_source = source_by_id.get(candidate.object_id)
        if candidate_route is None or candidate_source is None:
            _fail("M09 candidate refers outside the exact M08 plan")
        classification = classification_by_id.get(candidate_route.classification_id)
        attempt = attempt_by_id[candidate.parser_attempt_id]
        if classification is None or not (
            document.task_id == result.task_id
            and document.run_id == result.run_id
            and document.contract_version == result.contract_version
            and document.producer_version == result.producer_version
            and document.object_id == candidate_source.object_id
            and document.byte_sha256 == candidate_source.byte_sha256
            and document.object_metadata_hash == candidate_source.object_metadata_hash
            and document.acquisition_ids == candidate_source.acquisition_ids
            and document.classification_id == classification.classification_id
            and document.classification_hash == classification.classification_hash
            and document.upstream_plan_id == plan.plan_id
            and document.upstream_plan_hash == plan.plan_hash
            and document.upstream_parse_output_hash == upstream.output_hash
            and document.upstream_parse_event_id == upstream.event.event_id
            and document.route_id == candidate_route.route_id
            and document.route_hash == candidate_route.route_hash
            and document.scope == candidate_route.scope
            and document.parser_attempt_id == attempt.attempt_id
            and document.parser_id == candidate.parser_id
            and document.parser_version == candidate.parser_version
            and document.capability_hash == candidate.capability_hash
            and document.engine_name == candidate.engine_name
            and document.engine_version == candidate.engine_version
        ):
            _fail("M09 DocumentIR is not an exact source, route, and parser projection")
        # Local import avoids the quality -> integrity module dependency at import time.
        from scidatafusion.documents.quality import evaluate_document_quality

        expected_quality = evaluate_document_quality(
            document,
            candidate_route,
            candidate_id=candidate.candidate_id,
        )
        if attempt.quality_results != expected_quality:
            _fail("M09 quality results do not reproduce from the persisted DocumentIR")
        source_bytes = bronze_store.read(document.byte_sha256)
        for page in document.pages:
            for block in page.blocks:
                for anchor in block.anchors:
                    if isinstance(anchor, ByteSpanSourceAnchor):
                        if anchor.end_byte > len(source_bytes) or not hmac.compare_digest(
                            anchor.source_slice_sha256,
                            hashlib.sha256(
                                source_bytes[anchor.start_byte : anchor.end_byte]
                            ).hexdigest(),
                        ):
                            _fail("M09 byte-span anchor does not match immutable Bronze bytes")


def _canonical_json_bytes(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "M09 content could not be serialized as canonical JSON",
        ) from exc
    return encoded.encode("utf-8")


def _fail(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)

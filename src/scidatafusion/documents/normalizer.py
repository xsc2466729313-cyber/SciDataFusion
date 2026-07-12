"""Fail-closed normalization of untrusted adapter observations into M09 DocumentIR."""

from __future__ import annotations

import hashlib
import hmac
import html
from typing import NoReturn

from scidatafusion.contracts.documents import (
    BlockIR,
    ByteSpanSourceAnchor,
    DocumentIR,
    DocumentPageKind,
    DocumentParserRuntimeDescriptor,
    DocumentParsingRequest,
    DocumentTextOrigin,
    NormalizedBBox,
    PageGeometry,
    PageIR,
    PageRegionSourceAnchor,
    SourceAnchor,
)
from scidatafusion.contracts.parsing import (
    ParserRoute,
    ParserTargetModule,
    ParseScopeKind,
    RouteDisposition,
)
from scidatafusion.documents.adapters import (
    RawBlock,
    RawDocument,
    RawPage,
)
from scidatafusion.documents.integrity import (
    calculate_document_block_hash,
    calculate_document_hash,
    calculate_document_page_hash,
    calculate_document_parser_descriptor_hash,
    calculate_document_runtime_hash,
    serialize_document_ir,
    verify_document_ir_integrity,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.parsing.integrity import (
    calculate_classification_hash,
    calculate_parse_plan_hash,
    calculate_parse_planning_output_hash,
    calculate_parser_route_hash,
)
from scidatafusion.parsing.registry import (
    calculate_parser_capability_hash,
    calculate_parser_registry_hash,
)

_ZERO_HASH = "0" * 64
_ZERO_BLOCK_ID = "dbk_" + "0" * 32
_ZERO_PAGE_ID = "dpg_" + "0" * 32
_ZERO_DOCUMENT_ID = "dir_" + "0" * 32
_SPAN_TRANSFORMS_BY_MEDIA_TYPE = {
    "text/plain": frozenset({"utf8-decode", "utf8-bom-strip"}),
    "text/html": frozenset({"html-text-decode", "html-entity-decode"}),
}


def normalize_document_ir(
    raw: RawDocument,
    *,
    content: bytes,
    request: DocumentParsingRequest,
    route: ParserRoute,
    descriptor: DocumentParserRuntimeDescriptor,
    attempt_id: str,
    producer_version: str,
) -> DocumentIR:
    """Build one immutable DocumentIR only after revalidating all untrusted output lineage."""

    plan = request.parse_planning_result.plan
    source = next((item for item in plan.source_objects if item.object_id == route.object_id), None)
    classification = next(
        (
            item
            for item in plan.classifications
            if item.classification_id == route.classification_id
        ),
        None,
    )
    registered_route = next(
        (item for item in plan.routes if item.route_id == route.route_id),
        None,
    )
    if source is None or classification is None or registered_route != route:
        _invalid("M09 normalizer requires an exact M08 source, classification, and route")
    if (
        classification.object_id != source.object_id
        or classification.byte_sha256 != source.byte_sha256
        or classification.object_metadata_hash != source.object_metadata_hash
        or classification.acquisition_ids != source.acquisition_ids
        or route.classification_hash != classification.classification_hash
        or route.object_id != classification.object_id
        or route.capability_registry_hash != plan.capability_registry.registry_hash
        or not hmac.compare_digest(
            classification.classification_hash,
            calculate_classification_hash(classification),
        )
        or not hmac.compare_digest(route.route_hash, calculate_parser_route_hash(route))
        or not hmac.compare_digest(plan.plan_hash, calculate_parse_plan_hash(plan))
        or not hmac.compare_digest(
            request.parse_planning_result.output_hash,
            calculate_parse_planning_output_hash(request.parse_planning_result),
        )
        or not hmac.compare_digest(
            plan.capability_registry.registry_hash,
            calculate_parser_registry_hash(plan.capability_registry),
        )
    ):
        _invalid("M09 normalizer requires exact M08 source and classification lineage")
    if route.disposition is not RouteDisposition.PARSE or (
        route.target_module is not ParserTargetModule.DOCUMENT
    ):
        _invalid("M09 normalizer accepts only executable M09 routes")
    parser_sequence = (route.primary_parser_id, *route.fallback_parser_ids)
    if raw.parser_id not in parser_sequence or descriptor.parser_id != raw.parser_id:
        _invalid("M09 adapter output is not a parser declared by its route")
    runtime_descriptor = next(
        (
            item
            for item in request.runtime.parser_descriptors
            if item.parser_id == descriptor.parser_id
        ),
        None,
    )
    capability = next(
        (
            item
            for item in plan.capability_registry.parsers
            if item.parser_id == descriptor.parser_id
        ),
        None,
    )
    if (
        runtime_descriptor != descriptor
        or descriptor.parser_id not in request.runtime.available_parser_ids
        or capability is None
        or descriptor.parser_version != capability.parser_version
        or descriptor.capability_hash != capability.capability_hash
        or not hmac.compare_digest(
            capability.capability_hash,
            calculate_parser_capability_hash(capability),
        )
        or not hmac.compare_digest(
            descriptor.descriptor_hash,
            calculate_document_parser_descriptor_hash(descriptor),
        )
        or not hmac.compare_digest(
            request.runtime.runtime_hash,
            calculate_document_runtime_hash(request.runtime),
        )
    ):
        _invalid("M09 adapter descriptor does not match the immutable M08 and M09 runtimes")
    if (
        raw.parser_version != descriptor.parser_version
        or raw.engine_name != descriptor.engine_name
        or raw.engine_version != descriptor.engine_version
    ):
        _invalid("M09 adapter output does not match its immutable runtime descriptor")

    content_hash = hashlib.sha256(content).hexdigest()
    if not (
        hmac.compare_digest(content_hash, source.byte_sha256)
        and hmac.compare_digest(raw.content_sha256, source.byte_sha256)
    ):
        _invalid("M09 adapter output does not match the immutable Bronze bytes")
    if raw.media_type != classification.classified_media_type:
        _invalid("M09 adapter media type does not match the exact M08 classification")
    _validate_media_shape(raw)
    if (
        raw.page_count > request.policy.max_pages_per_document
        or raw.block_count > request.policy.max_total_blocks
        or raw.text_character_count > request.policy.max_total_text_characters
        or any(len(page.blocks) > request.policy.max_blocks_per_page for page in raw.pages)
        or any(
            len(block.verbatim_text) > request.policy.max_text_characters_per_block
            for page in raw.pages
            for block in page.blocks
        )
    ):
        raise AppError(
            ErrorCode.BUDGET_EXCEEDED,
            "M09 adapter output exceeds the configured document policy",
        )
    _validate_scope(raw, route)

    pages = tuple(
        _normalize_page(
            page,
            content=content,
            source_object_id=source.object_id,
            source_byte_sha256=source.byte_sha256,
            route=route,
            descriptor=descriptor,
            attempt_id=attempt_id,
            media_type=raw.media_type,
        )
        for page in raw.pages
    )
    draft = DocumentIR(
        task_id=request.parse_planning_result.task_id,
        run_id=request.parse_planning_result.run_id,
        contract_version=request.parse_planning_result.contract_version,
        created_at=request.runtime.checked_at,
        producer_version=producer_version,
        document_id=_ZERO_DOCUMENT_ID,
        object_id=source.object_id,
        byte_sha256=source.byte_sha256,
        object_metadata_hash=source.object_metadata_hash,
        acquisition_ids=source.acquisition_ids,
        classification_id=classification.classification_id,
        classification_hash=classification.classification_hash,
        upstream_plan_id=plan.plan_id,
        upstream_plan_hash=plan.plan_hash,
        upstream_parse_output_hash=request.parse_planning_result.output_hash,
        upstream_parse_event_id=request.parse_planning_result.event.event_id,
        route_id=route.route_id,
        route_hash=route.route_hash,
        scope=route.scope,
        parser_attempt_id=attempt_id,
        parser_id=descriptor.parser_id,
        parser_version=descriptor.parser_version,
        capability_hash=descriptor.capability_hash,
        engine_name=descriptor.engine_name,
        engine_version=descriptor.engine_version,
        pages=pages,
        page_count=len(pages),
        block_count=sum(len(page.blocks) for page in pages),
        text_character_count=sum(
            len(block.verbatim_text) for page in pages for block in page.blocks
        ),
        document_hash=_ZERO_HASH,
    )
    document_hash = calculate_document_hash(draft)
    document = DocumentIR.model_validate(
        draft.model_copy(
            update={
                "document_id": f"dir_{document_hash[:32]}",
                "document_hash": document_hash,
            }
        ).model_dump()
    )
    if len(serialize_document_ir(document)) > request.policy.max_output_bytes:
        raise AppError(
            ErrorCode.BUDGET_EXCEEDED,
            "M09 canonical DocumentIR exceeds the configured byte limit",
        )
    verify_document_ir_integrity(document)
    return document


def _normalize_page(
    raw: RawPage,
    *,
    content: bytes,
    source_object_id: str,
    source_byte_sha256: str,
    route: ParserRoute,
    descriptor: DocumentParserRuntimeDescriptor,
    attempt_id: str,
    media_type: str,
) -> PageIR:
    blocks = tuple(
        _normalize_block(
            block,
            page=raw,
            content=content,
            source_object_id=source_object_id,
            source_byte_sha256=source_byte_sha256,
            route=route,
            descriptor=descriptor,
            attempt_id=attempt_id,
            media_type=media_type,
        )
        for block in raw.blocks
    )
    geometry = (
        PageGeometry(
            width=raw.geometry.width,
            height=raw.geometry.height,
            unit=raw.geometry.unit,
            rotation_degrees=raw.geometry.rotation_degrees,
        )
        if raw.geometry is not None
        else None
    )
    draft = PageIR(
        page_id=_ZERO_PAGE_ID,
        object_id=source_object_id,
        byte_sha256=source_byte_sha256,
        route_id=route.route_id,
        route_hash=route.route_hash,
        parser_attempt_id=attempt_id,
        parser_id=descriptor.parser_id,
        parser_version=descriptor.parser_version,
        capability_hash=descriptor.capability_hash,
        engine_name=descriptor.engine_name,
        engine_version=descriptor.engine_version,
        page_number=raw.page_number,
        page_kind=raw.page_kind,
        geometry=geometry,
        blocks=blocks,
        page_hash=_ZERO_HASH,
    )
    page_hash = calculate_document_page_hash(draft)
    return PageIR.model_validate(
        draft.model_copy(
            update={
                "page_id": f"dpg_{page_hash[:32]}",
                "page_hash": page_hash,
            }
        ).model_dump()
    )


def _normalize_block(
    raw: RawBlock,
    *,
    page: RawPage,
    content: bytes,
    source_object_id: str,
    source_byte_sha256: str,
    route: ParserRoute,
    descriptor: DocumentParserRuntimeDescriptor,
    attempt_id: str,
    media_type: str,
) -> BlockIR:
    anchors = _normalize_anchors(
        raw,
        page=page,
        content=content,
        source_object_id=source_object_id,
        source_byte_sha256=source_byte_sha256,
        route=route,
        descriptor=descriptor,
        attempt_id=attempt_id,
        media_type=media_type,
    )
    draft = BlockIR(
        block_id=_ZERO_BLOCK_ID,
        object_id=source_object_id,
        byte_sha256=source_byte_sha256,
        route_id=route.route_id,
        route_hash=route.route_hash,
        parser_attempt_id=attempt_id,
        parser_id=descriptor.parser_id,
        parser_version=descriptor.parser_version,
        capability_hash=descriptor.capability_hash,
        engine_name=descriptor.engine_name,
        engine_version=descriptor.engine_version,
        page_number=page.page_number,
        kind=raw.kind,
        reading_order_index=raw.reading_order_index,
        verbatim_text=raw.verbatim_text,
        verbatim_text_sha256=raw.verbatim_text_sha256,
        text_origin=raw.text_origin,
        confidence=raw.confidence,
        anchors=anchors,
        block_hash=_ZERO_HASH,
    )
    block_hash = calculate_document_block_hash(draft)
    return BlockIR.model_validate(
        draft.model_copy(
            update={
                "block_id": f"dbk_{block_hash[:32]}",
                "block_hash": block_hash,
            }
        ).model_dump()
    )


def _normalize_anchors(
    raw: RawBlock,
    *,
    page: RawPage,
    content: bytes,
    source_object_id: str,
    source_byte_sha256: str,
    route: ParserRoute,
    descriptor: DocumentParserRuntimeDescriptor,
    attempt_id: str,
    media_type: str,
) -> tuple[SourceAnchor, ...]:
    if raw.text_origin is DocumentTextOrigin.DECODED_BYTES:
        anchors: list[SourceAnchor] = []
        reconstructed_fragments: list[str] = []
        previous_end = -1
        allowed_transforms = _SPAN_TRANSFORMS_BY_MEDIA_TYPE.get(media_type, frozenset())
        for span in raw.byte_spans:
            if (
                span.start_byte < previous_end
                or span.end_byte > len(content)
                or span.transform_id not in allowed_transforms
                or span.transform_version != "1.0.0"
                or not hmac.compare_digest(
                    span.source_slice_sha256,
                    hashlib.sha256(content[span.start_byte : span.end_byte]).hexdigest(),
                )
            ):
                _invalid("M09 raw byte span does not match immutable Bronze bytes")
            source_slice = content[span.start_byte : span.end_byte]
            reconstructed_fragments.append(
                _apply_span_transform(source_slice, transform_id=span.transform_id)
            )
            previous_end = span.end_byte
            anchors.append(
                ByteSpanSourceAnchor(
                    object_id=source_object_id,
                    byte_sha256=source_byte_sha256,
                    route_id=route.route_id,
                    route_hash=route.route_hash,
                    parser_attempt_id=attempt_id,
                    parser_id=descriptor.parser_id,
                    parser_version=descriptor.parser_version,
                    capability_hash=descriptor.capability_hash,
                    engine_name=descriptor.engine_name,
                    engine_version=descriptor.engine_version,
                    start_byte=span.start_byte,
                    end_byte=span.end_byte,
                    source_slice_sha256=span.source_slice_sha256,
                    encoding=span.encoding,
                    transform_id=span.transform_id,
                    transform_version=span.transform_version,
                )
            )
        if "".join(reconstructed_fragments) != raw.verbatim_text:
            _invalid("M09 raw byte spans do not reconstruct the observed verbatim text")
        return tuple(anchors)
    region = raw.page_region
    if region is None or page.native_ref_hash != region.native_ref_hash:
        _invalid("M09 raw page region does not match its containing native page")
    return (
        PageRegionSourceAnchor(
            object_id=source_object_id,
            byte_sha256=source_byte_sha256,
            route_id=route.route_id,
            route_hash=route.route_hash,
            parser_attempt_id=attempt_id,
            parser_id=descriptor.parser_id,
            parser_version=descriptor.parser_version,
            capability_hash=descriptor.capability_hash,
            engine_name=descriptor.engine_name,
            engine_version=descriptor.engine_version,
            page_number=region.page_number,
            bbox=NormalizedBBox(
                left=region.bbox.left,
                top=region.bbox.top,
                right=region.bbox.right,
                bottom=region.bbox.bottom,
            ),
            coordinate_precision=region.coordinate_precision,
            native_ref_hash=region.native_ref_hash,
        ),
    )


def _validate_scope(raw: RawDocument, route: ParserRoute) -> None:
    if route.scope.kind is ParseScopeKind.PAGE_RANGE:
        if (
            route.scope.start_page is None
            or route.scope.end_page is None
            or any(page.page_kind is not DocumentPageKind.FIXED for page in raw.pages)
            or tuple(page.page_number for page in raw.pages)
            != tuple(range(route.scope.start_page, route.scope.end_page + 1))
        ):
            _invalid("M09 adapter pages do not exactly cover the planned page scope")
    elif raw.pages[0].page_kind is DocumentPageKind.FIXED and tuple(
        page.page_number for page in raw.pages
    ) != tuple(range(1, len(raw.pages) + 1)):
        _invalid("M09 fixed pages must contiguously cover an artifact route")


def _validate_media_shape(raw: RawDocument) -> None:
    if raw.media_type == "application/pdf":
        if any(
            page.page_kind is not DocumentPageKind.FIXED
            or any(
                block.text_origin is not DocumentTextOrigin.PDF_TEXT_LAYER for block in page.blocks
            )
            for page in raw.pages
        ):
            _invalid("M09 PDF observations require fixed pages and PDF text-layer blocks")
        return
    if (
        len(raw.pages) != 1
        or raw.pages[0].page_kind is not DocumentPageKind.REFLOW
        or any(
            block.text_origin is not DocumentTextOrigin.DECODED_BYTES
            for block in raw.pages[0].blocks
        )
    ):
        _invalid("M09 flow observations require one reflow page of decoded byte blocks")


def _apply_span_transform(source_slice: bytes, *, transform_id: str) -> str:
    try:
        decoded = source_slice.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _invalid("M09 raw byte span is not valid UTF-8")
    if transform_id == "utf8-bom-strip":
        return decoded.removeprefix("\ufeff")
    if transform_id == "html-entity-decode":
        return html.unescape(decoded)
    return decoded


def _invalid(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)

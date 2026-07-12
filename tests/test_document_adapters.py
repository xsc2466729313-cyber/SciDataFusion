from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
from pathlib import Path

import pytest
from pydantic import ValidationError

from scidatafusion.contracts.documents import (
    DocumentBlockKind,
    DocumentCoordinatePrecision,
    DocumentPageKind,
    DocumentTextOrigin,
)
from scidatafusion.document_fixtures import (
    IA_DOCUMENT_PDF_PAGE_TEXT,
    IA_DOCUMENT_PDF_SHA256,
    build_ia_document_pdf,
)
from scidatafusion.documents import (
    DocumentAdapterError,
    DocumentAdapterErrorCode,
    DocumentAdapterLimits,
    DocumentAdapterRegistry,
    DocumentParserAdapter,
    HtmlDocumentAdapter,
    PlainTextDocumentAdapter,
    PypdfDocumentAdapter,
    RawBlock,
    RawByteSpan,
    RawDocument,
    RawNormalizedBBox,
    RawPage,
    default_document_adapter_registry,
)
from scidatafusion.documents.adapters import enforce_output_limits

_HASH = "0" * 64


def _parse(
    adapter: DocumentParserAdapter,
    content: bytes,
    *,
    limits: DocumentAdapterLimits | None = None,
) -> RawDocument:
    return asyncio.run(adapter.parse(content, limits=limits or DocumentAdapterLimits()))


def _assert_error(
    expected: DocumentAdapterErrorCode,
    adapter: DocumentParserAdapter,
    content: bytes,
    *,
    limits: DocumentAdapterLimits | None = None,
) -> DocumentAdapterError:
    with pytest.raises(DocumentAdapterError) as captured:
        _parse(adapter, content, limits=limits)
    assert captured.value.code is expected
    assert 0 < len(captured.value.detail) <= 512
    return captured.value


def test_limits_and_raw_models_are_strict() -> None:
    with pytest.raises(ValidationError):
        DocumentAdapterLimits(start_page=1)
    with pytest.raises(ValidationError):
        DocumentAdapterLimits(start_page=2, end_page=1)
    with pytest.raises(ValidationError):
        RawByteSpan.model_validate(
            {
                "start_byte": 0,
                "end_byte": 1,
                "source_slice_sha256": _HASH,
                "transform_id": "utf8-decode",
                "unexpected": True,
            }
        )


def test_default_registry_is_static_and_exact() -> None:
    registry = default_document_adapter_registry()

    assert registry.parser_ids == ("m09.pdf_text", "m09.html", "m09.text")
    assert isinstance(registry.require("m09.pdf_text"), PypdfDocumentAdapter)
    assert isinstance(registry.require("m09.html"), HtmlDocumentAdapter)
    assert isinstance(registry.require("m09.text"), PlainTextDocumentAdapter)
    assert registry.get("M09.TEXT") is None
    assert registry.get("m09.text ") is None
    error = _assert_registry_error(registry)
    assert "unknown-parser" not in error.detail


def _assert_registry_error(registry: DocumentAdapterRegistry) -> DocumentAdapterError:
    with pytest.raises(DocumentAdapterError) as captured:
        registry.require("unknown-parser")
    assert captured.value.code is DocumentAdapterErrorCode.UNSUPPORTED_INPUT
    return captured.value


def test_plain_text_preserves_paragraph_text_and_exact_byte_spans() -> None:
    content = b"\xef\xbb\xbfTitle\r\nline \xce\xb1\r\n\r\nSecond paragraph\n"

    document = _parse(PlainTextDocumentAdapter(engine_version="3.11.0"), content)

    assert document.model_performed is False
    assert document.network_performed is False
    assert document.content_sha256 == hashlib.sha256(content).hexdigest()
    assert document.media_type == "text/plain"
    assert document.engine_version == "3.11.0"
    assert document.pages[0].page_kind is DocumentPageKind.REFLOW
    assert [block.verbatim_text for block in document.pages[0].blocks] == [
        "Title\r\nline \u03b1",
        "Second paragraph",
    ]
    for block in document.pages[0].blocks:
        assert block.kind is DocumentBlockKind.PARAGRAPH
        assert block.text_origin is DocumentTextOrigin.DECODED_BYTES
        assert len(block.byte_spans) == 1
        span = block.byte_spans[0]
        source_slice = content[span.start_byte : span.end_byte]
        assert source_slice.decode("utf-8") == block.verbatim_text
        assert span.source_slice_sha256 == hashlib.sha256(source_slice).hexdigest()
        assert span.transform_id == "utf8-bom-strip"


def test_plain_text_rejects_invalid_encoding_scope_and_limits() -> None:
    adapter = PlainTextDocumentAdapter()

    _assert_error(DocumentAdapterErrorCode.INVALID_ENCODING, adapter, b"\xff")
    _assert_error(
        DocumentAdapterErrorCode.SCOPE_UNSUPPORTED,
        adapter,
        b"text",
        limits=DocumentAdapterLimits(start_page=1, end_page=1),
    )
    _assert_error(
        DocumentAdapterErrorCode.LIMIT_EXCEEDED,
        adapter,
        b"12345",
        limits=DocumentAdapterLimits(max_input_bytes=4),
    )
    _assert_error(DocumentAdapterErrorCode.NO_TEXT, adapter, b" \r\n\t")


def test_html_extracts_source_order_spans_without_traversal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("HTML adapter attempted a network operation")

    monkeypatch.setattr(socket, "create_connection", fail_network)
    content = (
        b"<!doctype html><html><body><h1>Heading &amp; facts</h1>"
        b"<p>Before<script>secret</script>After <a href='https://evil.invalid/x'>"
        b"Link</a></p><style>hidden</style><iframe src='https://evil.invalid/y'>bad"
        b"</iframe><object>bad</object><template>bad</template></body></html>"
    )

    document = _parse(HtmlDocumentAdapter(engine_version="3.11.0"), content)

    blocks = document.pages[0].blocks
    assert document.model_performed is False
    assert document.network_performed is False
    assert document.pages[0].page_kind is DocumentPageKind.REFLOW
    assert [(block.kind, block.verbatim_text) for block in blocks] == [
        (DocumentBlockKind.HEADING, "Heading & facts"),
        (DocumentBlockKind.PARAGRAPH, "BeforeAfter Link"),
    ]
    assert all(block.text_origin is DocumentTextOrigin.DECODED_BYTES for block in blocks)
    assert all(block.page_region is None for block in blocks)
    assert "secret" not in "".join(block.verbatim_text for block in blocks)
    assert "bad" not in "".join(block.verbatim_text for block in blocks)
    assert any(span.transform_id == "html-entity-decode" for span in blocks[0].byte_spans)
    observed_source_slices: list[bytes] = []
    for block in blocks:
        for span in block.byte_spans:
            source_slice = content[span.start_byte : span.end_byte]
            observed_source_slices.append(source_slice)
            assert span.source_slice_sha256 == hashlib.sha256(source_slice).hexdigest()
    observed_source = b"".join(observed_source_slices)
    assert b"href" not in observed_source
    assert b"src" not in observed_source
    assert b"evil.invalid" not in observed_source


def test_html_rejects_fixed_page_scope_encoding_and_depth() -> None:
    adapter = HtmlDocumentAdapter()

    _assert_error(DocumentAdapterErrorCode.INVALID_ENCODING, adapter, b"<p>\xff</p>")
    _assert_error(
        DocumentAdapterErrorCode.SCOPE_UNSUPPORTED,
        adapter,
        b"<p>text</p>",
        limits=DocumentAdapterLimits(start_page=1, end_page=1),
    )
    _assert_error(
        DocumentAdapterErrorCode.LIMIT_EXCEEDED,
        adapter,
        b"<div><div>text</div></div>",
        limits=DocumentAdapterLimits(max_markup_depth=1),
    )


def test_flow_adapters_enforce_aggregate_output_limits_during_emission() -> None:
    _assert_error(
        DocumentAdapterErrorCode.LIMIT_EXCEEDED,
        HtmlDocumentAdapter(),
        b"<p>one</p><p>two</p>",
        limits=DocumentAdapterLimits(max_total_blocks=1, max_blocks_per_page=1),
    )
    _assert_error(
        DocumentAdapterErrorCode.LIMIT_EXCEEDED,
        HtmlDocumentAdapter(),
        b"<p>one</p><p>two</p>",
        limits=DocumentAdapterLimits(
            max_characters_per_block=3,
            max_total_characters=5,
        ),
    )


def test_pdf_fixture_extracts_text_geometry_and_deterministic_regions() -> None:
    content = build_ia_document_pdf()
    assert hashlib.sha256(content).hexdigest() == IA_DOCUMENT_PDF_SHA256

    document = _parse(PypdfDocumentAdapter(engine_version="6.14.2"), content)

    assert document.content_sha256 == IA_DOCUMENT_PDF_SHA256
    assert document.page_count == 2
    assert document.engine_name == "pypdf"
    assert document.engine_version == "6.14.2"
    assert document.model_performed is False
    assert document.network_performed is False
    for expected_text, page in zip(IA_DOCUMENT_PDF_PAGE_TEXT, document.pages, strict=True):
        assert page.page_kind is DocumentPageKind.FIXED
        assert page.geometry is not None
        assert (page.geometry.width, page.geometry.height) == pytest.approx((612.0, 792.0))
        assert "\n".join(block.verbatim_text for block in page.blocks) == expected_text
        assert all(block.text_origin is DocumentTextOrigin.PDF_TEXT_LAYER for block in page.blocks)
        assert all(block.byte_spans == () for block in page.blocks)
        assert all(block.page_region is not None for block in page.blocks)
        assert all(
            block.page_region is not None
            and block.page_region.coordinate_precision is DocumentCoordinatePrecision.APPROXIMATE
            and block.page_region.native_ref_hash == page.native_ref_hash
            for block in page.blocks
        )
        assert sum(block.kind is DocumentBlockKind.HEADER for block in page.blocks) == 1
        assert sum(block.kind is DocumentBlockKind.FOOTER for block in page.blocks) == 1
        assert all(
            block.page_region is not None
            and 0 <= block.page_region.bbox.left < block.page_region.bbox.right <= 1_000_000
            and 0 <= block.page_region.bbox.top < block.page_region.bbox.bottom <= 1_000_000
            for block in page.blocks
        )

    second_page = document.pages[1]
    left = next(block for block in second_page.blocks if block.verbatim_text.startswith("Left"))
    right = next(block for block in second_page.blocks if block.verbatim_text.startswith("Right"))
    assert left.page_region is not None
    assert right.page_region is not None
    assert left.page_region.bbox.left < right.page_region.bbox.left


def test_pdf_honors_exact_page_range_and_bounded_failures() -> None:
    adapter = PypdfDocumentAdapter()
    content = build_ia_document_pdf()

    page_two = _parse(
        adapter,
        content,
        limits=DocumentAdapterLimits(start_page=2, end_page=2),
    )
    assert page_two.page_count == 1
    assert tuple(page.page_number for page in page_two.pages) == (2,)
    assert (
        "\n".join(block.verbatim_text for block in page_two.pages[0].blocks)
        == (IA_DOCUMENT_PDF_PAGE_TEXT[1])
    )
    _assert_error(
        DocumentAdapterErrorCode.LIMIT_EXCEEDED,
        adapter,
        content,
        limits=DocumentAdapterLimits(max_pages=1),
    )
    _assert_error(
        DocumentAdapterErrorCode.SCOPE_UNSUPPORTED,
        adapter,
        content,
        limits=DocumentAdapterLimits(start_page=2, end_page=3),
    )
    _assert_error(
        DocumentAdapterErrorCode.LIMIT_EXCEEDED,
        adapter,
        content,
        limits=DocumentAdapterLimits(max_blocks_per_page=6, max_total_blocks=6),
    )
    _assert_error(
        DocumentAdapterErrorCode.LIMIT_EXCEEDED,
        adapter,
        content,
        limits=DocumentAdapterLimits(
            max_characters_per_block=100,
            max_total_characters=len(IA_DOCUMENT_PDF_PAGE_TEXT[0]) + 1,
        ),
    )


def test_existing_invalid_ia_pdf_is_a_structured_malformed_failure() -> None:
    fixture_path = (
        Path(__file__).parents[1]
        / "src"
        / "scidatafusion"
        / "artifact_fixtures"
        / "ia"
        / "downloads.json"
    )
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    response = next(
        item
        for item in payload["responses"]
        if item["headers"].get("Content-Type") == "application/pdf"
    )
    invalid_pdf = base64.b64decode(response["content_base64"], validate=True)

    error = _assert_error(
        DocumentAdapterErrorCode.MALFORMED_DOCUMENT,
        PypdfDocumentAdapter(),
        invalid_pdf,
    )
    assert invalid_pdf.decode("latin-1") not in error.detail


def test_adapter_boundary_models_fail_closed_on_incoherent_observations() -> None:
    assert DocumentAdapterError(DocumentAdapterErrorCode.INVALID_OUTPUT, "   ").detail == (
        "document adapter failed"
    )

    for values in (
        {"max_blocks_per_page": 2, "max_total_blocks": 1},
        {"max_characters_per_block": 2, "max_total_characters": 1},
    ):
        with pytest.raises(ValidationError):
            DocumentAdapterLimits(**values)

    with pytest.raises(ValidationError, match="end must exceed"):
        RawByteSpan(
            start_byte=1,
            end_byte=1,
            source_slice_sha256=_HASH,
            transform_id="utf8-decode",
        )
    with pytest.raises(ValidationError, match="positive dimensions"):
        RawNormalizedBBox(left=1, top=1, right=1, bottom=2)

    plain = _parse(PlainTextDocumentAdapter(), b"observed text")
    block_data = plain.pages[0].blocks[0].model_dump(mode="python")
    invalid_blocks: list[dict[str, object]] = []

    wrong_hash = dict(block_data)
    wrong_hash["verbatim_text_sha256"] = "f" * 64
    invalid_blocks.append(wrong_hash)

    whitespace = dict(block_data)
    whitespace["verbatim_text"] = "   "
    whitespace["verbatim_text_sha256"] = hashlib.sha256(b"   ").hexdigest()
    invalid_blocks.append(whitespace)

    missing_span = dict(block_data)
    missing_span["byte_spans"] = ()
    invalid_blocks.append(missing_span)

    pdf_with_span = dict(block_data)
    pdf_with_span["text_origin"] = DocumentTextOrigin.PDF_TEXT_LAYER
    invalid_blocks.append(pdf_with_span)

    unsupported_origin = dict(block_data)
    unsupported_origin["text_origin"] = DocumentTextOrigin.NONE
    invalid_blocks.append(unsupported_origin)

    for data in invalid_blocks:
        with pytest.raises(ValidationError):
            RawBlock.model_validate(data)

    page_data = plain.pages[0].model_dump(mode="python")
    fixed_without_geometry = dict(page_data)
    fixed_without_geometry["page_kind"] = DocumentPageKind.FIXED
    with pytest.raises(ValidationError, match="require geometry"):
        RawPage.model_validate(fixed_without_geometry)

    reflow_page_two = dict(page_data)
    reflow_page_two["page_number"] = 2
    with pytest.raises(ValidationError, match="synthetic page one"):
        RawPage.model_validate(reflow_page_two)

    unordered = dict(page_data)
    unordered_block = dict(block_data)
    unordered_block["reading_order_index"] = 1
    unordered["blocks"] = (unordered_block,)
    with pytest.raises(ValidationError, match="contiguous"):
        RawPage.model_validate(unordered)

    document_data = plain.model_dump(mode="python")
    document_data["block_count"] = 2
    with pytest.raises(ValidationError, match="counts must be derived"):
        RawDocument.model_validate(document_data)

    duplicate_document = plain.model_copy(
        update={
            "pages": (plain.pages[0], plain.pages[0]),
            "page_count": 2,
            "block_count": 2,
            "text_character_count": 2 * plain.text_character_count,
        }
    )
    with pytest.raises(ValidationError, match="ordered and unique"):
        RawDocument.model_validate(duplicate_document.model_dump(mode="python"))

    adapter = PlainTextDocumentAdapter()
    with pytest.raises(ValueError, match="unique"):
        DocumentAdapterRegistry((adapter, adapter))
    _assert_error(DocumentAdapterErrorCode.UNSUPPORTED_INPUT, adapter, b"")

    pdf = _parse(PypdfDocumentAdapter(), build_ia_document_pdf())
    with pytest.raises(DocumentAdapterError) as page_limit:
        enforce_output_limits(pdf.pages, DocumentAdapterLimits(max_pages=1))
    assert page_limit.value.code is DocumentAdapterErrorCode.LIMIT_EXCEEDED

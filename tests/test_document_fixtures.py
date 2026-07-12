from __future__ import annotations

import hashlib
from io import BytesIO

import pytest
from pypdf import PdfReader

from scidatafusion.document_fixtures import (
    IA_DOCUMENT_PDF_FOOTER,
    IA_DOCUMENT_PDF_HEADER,
    IA_DOCUMENT_PDF_PAGE_COUNT,
    IA_DOCUMENT_PDF_PAGE_TEXT,
    IA_DOCUMENT_PDF_SHA256,
    build_ia_document_pdf,
)

_PINNED_SHA256 = "3e96409837aa9d94996740400901fd17fb437c310990ef03a519d3339dd275e1"


def test_ia_document_pdf_is_deterministic_and_hash_pinned() -> None:
    first = build_ia_document_pdf()
    second = build_ia_document_pdf()

    assert first == second
    assert first.startswith(b"%PDF-")
    assert first.rstrip().endswith(b"%%EOF")
    assert hashlib.sha256(first).hexdigest() == _PINNED_SHA256
    assert IA_DOCUMENT_PDF_SHA256 == _PINNED_SHA256
    assert b"/CreationDate" not in first
    assert b"/ModDate" not in first


def test_ia_document_pdf_is_strictly_readable_with_fixed_text() -> None:
    reader = PdfReader(BytesIO(build_ia_document_pdf()), strict=True)

    assert len(reader.pages) == IA_DOCUMENT_PDF_PAGE_COUNT == 2
    extracted = tuple(page.extract_text().rstrip("\n") for page in reader.pages)
    assert extracted == IA_DOCUMENT_PDF_PAGE_TEXT
    assert all(text.count(IA_DOCUMENT_PDF_HEADER) == 1 for text in extracted)
    assert all(text.count(IA_DOCUMENT_PDF_FOOTER) == 1 for text in extracted)


def test_second_page_retains_distinct_column_coordinates() -> None:
    reader = PdfReader(BytesIO(build_ia_document_pdf()), strict=True)
    positions: dict[str, tuple[float, float]] = {}

    def capture_position(
        text: str,
        _current_matrix: list[float],
        text_matrix: list[float],
        _font: dict[str, object] | None,
        _font_size: float,
    ) -> None:
        stripped = text.strip()
        if stripped.startswith(("Left column:", "Right column:")):
            positions[stripped] = (text_matrix[4], text_matrix[5])

    reader.pages[1].extract_text(visitor_text=capture_position)

    left_x = {
        position[0] for text, position in positions.items() if text.startswith("Left column:")
    }
    right_x = {
        position[0] for text, position in positions.items() if text.startswith("Right column:")
    }
    assert tuple(sorted(left_x)) == pytest.approx((72.0,))
    assert tuple(sorted(right_x)) == pytest.approx((330.0,))
    assert tuple(sorted({position[1] for position in positions.values()})) == pytest.approx(
        (630.0, 650.0)
    )

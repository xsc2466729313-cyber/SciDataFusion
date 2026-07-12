"""Deterministic offline document fixtures used by M09 acceptance tests."""

from scidatafusion.document_fixtures.ia import (
    IA_DOCUMENT_PDF_FOOTER,
    IA_DOCUMENT_PDF_HEADER,
    IA_DOCUMENT_PDF_PAGE_COUNT,
    IA_DOCUMENT_PDF_PAGE_TEXT,
    IA_DOCUMENT_PDF_SHA256,
    build_ia_document_pdf,
)

__all__ = [
    "IA_DOCUMENT_PDF_FOOTER",
    "IA_DOCUMENT_PDF_HEADER",
    "IA_DOCUMENT_PDF_PAGE_COUNT",
    "IA_DOCUMENT_PDF_PAGE_TEXT",
    "IA_DOCUMENT_PDF_SHA256",
    "build_ia_document_pdf",
]

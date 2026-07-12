"""Bounded local document parser adapters for M09."""

from scidatafusion.documents.adapters import (
    DocumentAdapterError,
    DocumentAdapterErrorCode,
    DocumentAdapterLimits,
    DocumentAdapterRegistry,
    DocumentParserAdapter,
    RawBlock,
    RawByteSpan,
    RawDocument,
    RawNormalizedBBox,
    RawPage,
    RawPageGeometry,
    RawPageRegion,
)
from scidatafusion.documents.html import HtmlDocumentAdapter
from scidatafusion.documents.pdf import PypdfDocumentAdapter
from scidatafusion.documents.plain import PlainTextDocumentAdapter


def default_document_adapter_registry() -> DocumentAdapterRegistry:
    """Return the fixed offline M09 adapter registry in parser-ID order."""

    return DocumentAdapterRegistry(
        (
            PypdfDocumentAdapter(),
            HtmlDocumentAdapter(),
            PlainTextDocumentAdapter(),
        )
    )


__all__ = [
    "DocumentAdapterError",
    "DocumentAdapterErrorCode",
    "DocumentAdapterLimits",
    "DocumentAdapterRegistry",
    "DocumentParserAdapter",
    "HtmlDocumentAdapter",
    "PlainTextDocumentAdapter",
    "PypdfDocumentAdapter",
    "RawBlock",
    "RawByteSpan",
    "RawDocument",
    "RawNormalizedBBox",
    "RawPage",
    "RawPageGeometry",
    "RawPageRegion",
    "default_document_adapter_registry",
]

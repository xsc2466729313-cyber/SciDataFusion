"""Fixed in-memory PDF fixture for the M09 Type Ia vertical slice."""

from __future__ import annotations

from io import BytesIO

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

IA_DOCUMENT_PDF_HEADER = "SciDataFusion M09 Offline Fixture"
IA_DOCUMENT_PDF_FOOTER = "Deterministic document fixture footer"
IA_DOCUMENT_PDF_PAGE_COUNT = 2
IA_DOCUMENT_PDF_SHA256 = "3e96409837aa9d94996740400901fd17fb437c310990ef03a519d3339dd275e1"
IA_DOCUMENT_PDF_PAGE_TEXT = (
    "\n".join(
        (
            IA_DOCUMENT_PDF_HEADER,
            "Type Ia Supernova Light Curves",
            "This deterministic PDF validates offline document text extraction.",
            "Every extracted statement remains linked to immutable source bytes.",
            IA_DOCUMENT_PDF_FOOTER,
        )
    ),
    "\n".join(
        (
            IA_DOCUMENT_PDF_HEADER,
            "Two-column Observation Summary",
            "Left column: observation epoch and band.",
            "Left column: source identifier SN-Ia-001.",
            "Right column: magnitude and uncertainty.",
            "Right column: evidence remains unmodified.",
            IA_DOCUMENT_PDF_FOOTER,
        )
    ),
)

_PAGE_WIDTH = 612.0
_PAGE_HEIGHT = 792.0
_TextRun = tuple[float, float, float, str]
_PAGE_RUNS: tuple[tuple[_TextRun, ...], ...] = (
    (
        (72.0, 756.0, 9.0, IA_DOCUMENT_PDF_HEADER),
        (72.0, 700.0, 18.0, "Type Ia Supernova Light Curves"),
        (
            72.0,
            660.0,
            11.0,
            "This deterministic PDF validates offline document text extraction.",
        ),
        (
            72.0,
            640.0,
            11.0,
            "Every extracted statement remains linked to immutable source bytes.",
        ),
        (72.0, 36.0, 9.0, IA_DOCUMENT_PDF_FOOTER),
    ),
    (
        (72.0, 756.0, 9.0, IA_DOCUMENT_PDF_HEADER),
        (72.0, 700.0, 18.0, "Two-column Observation Summary"),
        (72.0, 650.0, 11.0, "Left column: observation epoch and band."),
        (72.0, 630.0, 11.0, "Left column: source identifier SN-Ia-001."),
        (330.0, 650.0, 11.0, "Right column: magnitude and uncertainty."),
        (330.0, 630.0, 11.0, "Right column: evidence remains unmodified."),
        (72.0, 36.0, 9.0, IA_DOCUMENT_PDF_FOOTER),
    ),
)


def build_ia_document_pdf() -> bytes:
    """Return a deterministic, valid two-page PDF without filesystem access."""

    writer = PdfWriter()
    writer.metadata = None
    for runs in _PAGE_RUNS:
        page = writer.add_blank_page(width=_PAGE_WIDTH, height=_PAGE_HEIGHT)
        page[NameObject("/Resources")] = _font_resources()
        stream = DecodedStreamObject()
        stream.set_data(_content_stream(runs))
        # PdfWriter has no public text-drawing API and PDF streams must be indirect.
        # This is the only private writer seam used around otherwise public generic objects.
        page[NameObject("/Contents")] = writer._add_object(stream)

    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _font_resources() -> DictionaryObject:
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }
    )
    return DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {
                    NameObject("/F1"): font,
                }
            )
        }
    )


def _content_stream(runs: tuple[_TextRun, ...]) -> bytes:
    commands: list[str] = []
    for x, y, font_size, text in runs:
        commands.extend(
            (
                "BT",
                f"/F1 {font_size:g} Tf",
                f"1 0 0 1 {x:g} {y:g} Tm",
                f"({_pdf_literal(text)}) Tj",
                "ET",
            )
        )
    return ("\n".join(commands) + "\n").encode("ascii")


def _pdf_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

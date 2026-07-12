"""Deterministic magic-byte and structural content inspection for M07."""

from __future__ import annotations

import csv
import io
import json
import zipfile

from scidatafusion.contracts.artifacts import (
    ArtifactKind,
    ContentDetectionBasis,
    ContentInspection,
)

_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"
_TIFF_LE = b"II*\x00"
_TIFF_BE = b"MM\x00*"
_HDF5 = b"\x89HDF\r\n\x1a\n"
_MAX_TEXT_PROBE_BYTES = 1_048_576


class ContentSniffer:
    """Classify bytes without trusting filenames or response media declarations."""

    @staticmethod
    def inspect(
        content: bytes,
        *,
        declared_media_type: str | None = None,
    ) -> ContentInspection:
        """Return one deterministic content inspection from bounded in-memory bytes."""

        normalized_declared = _normalize_media_type(declared_media_type)
        detected, basis, kind, confidence = _detect(content)
        return ContentInspection(
            detected_media_type=detected,
            declared_media_type=normalized_declared,
            basis=basis,
            artifact_kind=kind,
            media_type_mismatch=(
                normalized_declared is not None and normalized_declared != detected
            ),
            confidence=confidence,
            requires_review=(
                (normalized_declared is not None and normalized_declared != detected)
                or basis is ContentDetectionBasis.UNKNOWN
                or kind is ArtifactKind.UNKNOWN
            ),
        )


def _detect(
    content: bytes,
) -> tuple[str, ContentDetectionBasis, ArtifactKind, float]:
    if content.startswith(b"%PDF-"):
        return "application/pdf", ContentDetectionBasis.MAGIC_BYTES, ArtifactKind.DOCUMENT, 1.0
    if content.startswith(_PNG):
        return "image/png", ContentDetectionBasis.MAGIC_BYTES, ArtifactKind.IMAGE, 1.0
    if content.startswith(_JPEG):
        return "image/jpeg", ContentDetectionBasis.MAGIC_BYTES, ArtifactKind.IMAGE, 1.0
    if content.startswith((_TIFF_LE, _TIFF_BE)):
        return "image/tiff", ContentDetectionBasis.MAGIC_BYTES, ArtifactKind.IMAGE, 1.0
    if content.startswith(_HDF5):
        return (
            "application/x-hdf5",
            ContentDetectionBasis.MAGIC_BYTES,
            ArtifactKind.SCIENTIFIC_FILE,
            1.0,
        )
    if content.startswith(b"SIMPLE  ="):
        return (
            "application/fits",
            ContentDetectionBasis.STRUCTURAL_PROBE,
            ArtifactKind.SCIENTIFIC_FILE,
            0.98,
        )
    if len(content) >= 8 and content.startswith(b"PAR1") and content.endswith(b"PAR1"):
        return (
            "application/vnd.apache.parquet",
            ContentDetectionBasis.STRUCTURAL_PROBE,
            ArtifactKind.TABLE,
            1.0,
        )
    if content.startswith(b"\x1f\x8b"):
        return "application/gzip", ContentDetectionBasis.MAGIC_BYTES, ArtifactKind.ARCHIVE, 1.0
    if content.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return _detect_zip(content)
    text = _decode_text(content[:_MAX_TEXT_PROBE_BYTES])
    if text is not None:
        stripped = text.lstrip("\ufeff\t\r\n ")
        folded = stripped[:512].casefold()
        if folded.startswith(("<!doctype html", "<html", "<head", "<body")):
            return "text/html", ContentDetectionBasis.TEXT_PROBE, ArtifactKind.LANDING_PAGE, 0.98
        if len(content) > _MAX_TEXT_PROBE_BYTES and stripped.startswith(("{", "[")):
            return (
                "application/octet-stream",
                ContentDetectionBasis.UNKNOWN,
                ArtifactKind.UNKNOWN,
                0.0,
            )
        if _is_json(stripped):
            return (
                "application/json",
                ContentDetectionBasis.STRUCTURAL_PROBE,
                ArtifactKind.TABLE,
                0.95,
            )
        if _is_csv(text):
            return "text/csv", ContentDetectionBasis.STRUCTURAL_PROBE, ArtifactKind.TABLE, 0.9
        if _printable_ratio(text) >= 0.9:
            return "text/plain", ContentDetectionBasis.TEXT_PROBE, ArtifactKind.DOCUMENT, 0.75
    return (
        "application/octet-stream",
        ContentDetectionBasis.UNKNOWN,
        ArtifactKind.UNKNOWN,
        0.0,
    )


def _detect_zip(
    content: bytes,
) -> tuple[str, ContentDetectionBasis, ArtifactKind, float]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = frozenset(archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return (
            "application/zip",
            ContentDetectionBasis.MAGIC_BYTES,
            ArtifactKind.ARCHIVE,
            0.7,
        )
    if "[Content_Types].xml" in names and any(name.startswith("xl/") for name in names):
        return (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ContentDetectionBasis.STRUCTURAL_PROBE,
            ArtifactKind.TABLE,
            1.0,
        )
    if "[Content_Types].xml" in names and any(name.startswith("word/") for name in names):
        return (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ContentDetectionBasis.STRUCTURAL_PROBE,
            ArtifactKind.DOCUMENT,
            1.0,
        )
    return "application/zip", ContentDetectionBasis.MAGIC_BYTES, ArtifactKind.ARCHIVE, 1.0


def _decode_text(content: bytes) -> str | None:
    if not content:
        return None
    if b"\x00" in content[:4096]:
        return None
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _is_json(value: str) -> bool:
    if not value or value[0] not in "[{":
        return False
    try:
        parsed: object = json.loads(value)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, (dict, list))


def _is_csv(value: str) -> bool:
    lines = [line for line in value.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    sample = "\n".join(lines[:20])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        rows = list(csv.reader(io.StringIO(sample), dialect))
    except (csv.Error, UnicodeError):
        return False
    widths = {len(row) for row in rows}
    return len(widths) == 1 and next(iter(widths), 0) >= 2


def _printable_ratio(value: str) -> float:
    if not value:
        return 0.0
    printable = sum(character.isprintable() or character in "\r\n\t" for character in value)
    return printable / len(value)


def _normalize_media_type(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.split(";", maxsplit=1)[0].strip().casefold()
    return normalized or None

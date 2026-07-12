"""Bounded deterministic artifact classification for M08 parse planning."""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from typing import Protocol

from scidatafusion.artifacts.sniffer import ContentSniffer
from scidatafusion.contracts.artifacts import (
    ArtifactKind,
    BronzeObject,
    ContentDetectionBasis,
)
from scidatafusion.contracts.parsing import (
    ClassificationBasis,
    ClassificationReviewCode,
    FormatFamily,
    ParsePlanningPolicy,
    StructuralFeatures,
)

_NETCDF_SIGNATURES = (b"CDF\x01", b"CDF\x02", b"CDF\x05")
_PDF_PAGE_PATTERN = re.compile(rb"/Type\s*/Page\b")
_ZIP_LOCAL_FILE_HEADER = b"PK\x03\x04"
_ZIP_LOCAL_FILE_HEADER_SIZE = 30
_ZIP_CENTRAL_DIRECTORY_HEADER = b"PK\x01\x02"
_ZIP_CENTRAL_DIRECTORY_HEADER_SIZE = 46
_ZIP_END_OF_CENTRAL_DIRECTORY = b"PK\x05\x06"
_ZIP_END_OF_CENTRAL_DIRECTORY_SIZE = 22
_ZIP_MAX_END_SEARCH_BYTES = 65_557
_ZIP_MEMBER_NAME_LIMIT = 4_096
_ZIP_SUPPORTED_METHODS = frozenset({0, 8, 12, 14, 93})
_ZIP_ENCRYPTION_FLAGS = 0x2041
_ZIP64_EXTRA_FIELD_ID = 0x0001


@dataclass(frozen=True, slots=True)
class ClassificationDecision:
    """Pure classification values used to construct a hash-linked contract."""

    classified_media_type: str
    artifact_kind: ArtifactKind
    format_family: FormatFamily
    features: StructuralFeatures
    basis: tuple[ClassificationBasis, ...]
    confidence: float
    source_media_type_mismatch: bool
    review_codes: tuple[ClassificationReviewCode, ...]


@dataclass(frozen=True, slots=True)
class _ZipEndRecord:
    central_directory_offset: int
    central_directory_size: int
    entry_count: int
    record_offset: int


@dataclass(frozen=True, slots=True)
class _ZipCentralEntry:
    name: str
    flags: int
    method: int
    crc32: int
    compressed_size: int
    uncompressed_size: int
    local_header_offset: int


@dataclass(frozen=True, slots=True)
class _ZipLocalEntry:
    name: str
    flags: int
    method: int
    crc32: int
    compressed_size: int
    uncompressed_size: int
    payload_offset: int


class StructuralFeatureProbe(Protocol):
    """Read-only boundary for deterministic, bounded structural observations."""

    def inspect(
        self,
        sample: bytes,
        *,
        total_size: int,
        media_type: str,
        format_family: FormatFamily,
        max_pages: int,
    ) -> StructuralFeatures:
        """Return structural facts without retaining body text or scientific values."""


class ArtifactClassifier(Protocol):
    """Classify one verified Bronze object without executing its content."""

    def classify(
        self,
        obj: BronzeObject,
        content: bytes,
        policy: ParsePlanningPolicy,
    ) -> ClassificationDecision:
        """Return one deterministic decision for immutable object bytes."""


class BoundedStructuralFeatureProbe:
    """Conservative built-in probe for inert container and PDF metadata facts."""

    def inspect(
        self,
        sample: bytes,
        *,
        total_size: int,
        media_type: str,
        format_family: FormatFamily,
        max_pages: int,
    ) -> StructuralFeatures:
        """Inspect bounded bytes without parsing document bodies or table values."""

        del media_type, max_pages
        encrypted = format_family is FormatFamily.PDF and b"/Encrypt" in sample
        damaged = False
        total_pages: int | None = None
        if format_family is FormatFamily.PDF:
            if not sample.startswith(b"%PDF-"):
                damaged = True
            elif len(sample) == total_size and b"%%EOF" not in sample[-1024:]:
                damaged = True
            if len(sample) == total_size:
                page_count = len(_PDF_PAGE_PATTERN.findall(sample))
                total_pages = page_count or None
        elif format_family in {
            FormatFamily.ARCHIVE,
            FormatFamily.DOCX,
            FormatFamily.PPTX,
            FormatFamily.XLSX,
        }:
            damaged = len(sample) == total_size and not _is_valid_zip(sample)
        return StructuralFeatures(
            sampled_bytes=len(sample),
            total_pages=total_pages,
            sampled_pages=0,
            pages=(),
            text_layer_density=None,
            scanned_page_ratio=None,
            table_page_ratio=None,
            figure_page_ratio=None,
            encrypted=encrypted,
            damaged=damaged,
        )


class DeterministicArtifactClassifier:
    """Signature-first classifier that treats M07 metadata as a verified prior."""

    def __init__(self, *, feature_probe: StructuralFeatureProbe | None = None) -> None:
        self._feature_probe = feature_probe or BoundedStructuralFeatureProbe()

    def classify(
        self,
        obj: BronzeObject,
        content: bytes,
        policy: ParsePlanningPolicy,
    ) -> ClassificationDecision:
        """Classify one object from a bounded prefix and immutable M07 inspection."""

        sample, zip_tail, zip_tail_offset, sampled_bytes = _bounded_samples(
            content,
            policy.max_sample_bytes_per_artifact,
        )
        sample_media, sample_kind, sample_basis, sample_confidence = _inspect_sample(sample)
        prior = obj.media
        if sample_basis is ClassificationBasis.M07_INSPECTION:
            media_type = prior.detected_media_type
            artifact_kind = prior.artifact_kind
            confidence = prior.confidence
            basis: tuple[ClassificationBasis, ...] = (ClassificationBasis.M07_INSPECTION,)
        else:
            media_type = sample_media
            artifact_kind = sample_kind
            confidence = sample_confidence
            basis = (ClassificationBasis.M07_INSPECTION, sample_basis)

        format_family, normalized_media, normalized_kind, structural_basis = _format_family(
            media_type,
            artifact_kind,
            sample,
            zip_tail=zip_tail,
            zip_tail_offset=zip_tail_offset,
            total_size=len(content),
        )
        if structural_basis is not None and structural_basis not in basis:
            basis = (*basis, structural_basis)
        if (
            sample_media == "application/zip"
            and structural_basis is ClassificationBasis.STRUCTURAL_PROBE
            and format_family
            in {
                FormatFamily.DOCX,
                FormatFamily.PPTX,
                FormatFamily.XLSX,
            }
        ):
            confidence = max(confidence, 0.95)
        media_type = normalized_media
        artifact_kind = normalized_kind
        mismatch = prior.media_type_mismatch or (
            media_type != prior.detected_media_type
            and not _is_compatible_refinement(
                prior.detected_media_type,
                media_type,
            )
        )
        if mismatch:
            confidence = min(confidence, 0.69)

        features = self._feature_probe.inspect(
            sample,
            total_size=len(content),
            media_type=media_type,
            format_family=format_family,
            max_pages=policy.max_sample_pages_per_artifact,
        )
        if features.sampled_bytes != sampled_bytes:
            features = features.model_copy(update={"sampled_bytes": sampled_bytes})
        review_codes: list[ClassificationReviewCode] = []
        if mismatch:
            review_codes.append(ClassificationReviewCode.MEDIA_TYPE_MISMATCH)
        if format_family is FormatFamily.UNKNOWN or artifact_kind is ArtifactKind.UNKNOWN:
            review_codes.append(ClassificationReviewCode.UNKNOWN_FORMAT)
        if features.encrypted:
            review_codes.append(ClassificationReviewCode.NEEDS_PASSWORD)
        if features.damaged:
            review_codes.append(ClassificationReviewCode.DAMAGED_FILE)
        if not sample or (
            sampled_bytes < len(content)
            and (
                format_family is FormatFamily.UNKNOWN
                or (sample_media == "application/zip" and format_family is FormatFamily.ARCHIVE)
            )
        ):
            review_codes.append(ClassificationReviewCode.SAMPLE_INSUFFICIENT)
        if format_family is not FormatFamily.UNKNOWN and confidence < (
            policy.minimum_classification_confidence
        ):
            review_codes.append(ClassificationReviewCode.LOW_CONFIDENCE)
        if format_family is FormatFamily.UNKNOWN:
            confidence = 0.0

        return ClassificationDecision(
            classified_media_type=media_type,
            artifact_kind=artifact_kind,
            format_family=format_family,
            features=features,
            basis=tuple(dict.fromkeys(basis)),
            confidence=confidence,
            source_media_type_mismatch=mismatch,
            review_codes=tuple(dict.fromkeys(review_codes)),
        )


def _inspect_sample(
    sample: bytes,
) -> tuple[str, ArtifactKind, ClassificationBasis, float]:
    if sample.startswith(_NETCDF_SIGNATURES):
        return (
            "application/x-netcdf",
            ArtifactKind.SCIENTIFIC_FILE,
            ClassificationBasis.SCIENTIFIC_SIGNATURE,
            1.0,
        )
    inspection = ContentSniffer.inspect(sample)
    if inspection.basis is ContentDetectionBasis.UNKNOWN:
        return (
            inspection.detected_media_type,
            inspection.artifact_kind,
            ClassificationBasis.M07_INSPECTION,
            inspection.confidence,
        )
    basis = {
        ContentDetectionBasis.MAGIC_BYTES: ClassificationBasis.MAGIC_BYTES,
        ContentDetectionBasis.STRUCTURAL_PROBE: ClassificationBasis.STRUCTURAL_PROBE,
        ContentDetectionBasis.TEXT_PROBE: ClassificationBasis.STRUCTURAL_PROBE,
    }[inspection.basis]
    return (
        inspection.detected_media_type,
        inspection.artifact_kind,
        basis,
        inspection.confidence,
    )


def _format_family(
    media_type: str,
    artifact_kind: ArtifactKind,
    sample: bytes,
    *,
    zip_tail: bytes,
    zip_tail_offset: int,
    total_size: int,
) -> tuple[FormatFamily, str, ArtifactKind, ClassificationBasis | None]:
    media = media_type.casefold()
    if media in {"text/plain", "application/octet-stream"}:
        if sample.lstrip().startswith(b"<?xml"):
            return (
                FormatFamily.XML,
                "application/xml",
                ArtifactKind.DOCUMENT,
                ClassificationBasis.STRUCTURAL_PROBE,
            )
        if _looks_like_sequence(sample):
            return (
                FormatFamily.SEQUENCE,
                "text/x-fasta",
                ArtifactKind.SCIENTIFIC_FILE,
                ClassificationBasis.SCIENTIFIC_SIGNATURE,
            )
    direct = {
        "application/pdf": FormatFamily.PDF,
        "text/html": FormatFamily.HTML,
        "text/plain": FormatFamily.PLAIN_TEXT,
        "text/csv": FormatFamily.CSV,
        "text/tab-separated-values": FormatFamily.CSV,
        "application/json": FormatFamily.JSON,
        "application/xml": FormatFamily.XML,
        "text/xml": FormatFamily.XML,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (FormatFamily.XLSX),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
            FormatFamily.DOCX
        ),
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": (
            FormatFamily.PPTX
        ),
        "application/vnd.apache.parquet": FormatFamily.PARQUET,
        "application/fits": FormatFamily.FITS,
        "application/x-fits": FormatFamily.FITS,
        "application/x-hdf5": FormatFamily.HDF5,
        "application/x-netcdf": FormatFamily.NETCDF,
        "text/x-fasta": FormatFamily.SEQUENCE,
        "application/gzip": FormatFamily.ARCHIVE,
    }
    family = direct.get(media)
    if family is not None:
        return family, media, _kind_for_family(family, artifact_kind), None
    if media.startswith("image/"):
        return FormatFamily.IMAGE, media, ArtifactKind.IMAGE, None
    if media == "application/zip":
        ooxml = _ooxml_family(
            sample,
            tail=zip_tail,
            tail_offset=zip_tail_offset,
            total_size=total_size,
        )
        if ooxml is not None:
            family, media = ooxml
            return (
                family,
                media,
                _kind_for_family(family, artifact_kind),
                (ClassificationBasis.STRUCTURAL_PROBE),
            )
        return FormatFamily.ARCHIVE, media, ArtifactKind.ARCHIVE, None
    return (
        FormatFamily.UNKNOWN,
        "application/octet-stream",
        ArtifactKind.UNKNOWN,
        None,
    )


def _kind_for_family(family: FormatFamily, fallback: ArtifactKind) -> ArtifactKind:
    if family in {
        FormatFamily.CSV,
        FormatFamily.JSON,
        FormatFamily.XLSX,
        FormatFamily.PARQUET,
    }:
        return ArtifactKind.TABLE
    if family in {
        FormatFamily.PDF,
        FormatFamily.PLAIN_TEXT,
        FormatFamily.DOCX,
        FormatFamily.PPTX,
        FormatFamily.XML,
    }:
        return ArtifactKind.DOCUMENT
    if family is FormatFamily.HTML:
        return ArtifactKind.LANDING_PAGE
    if family is FormatFamily.IMAGE:
        return ArtifactKind.IMAGE
    if family is FormatFamily.ARCHIVE:
        return ArtifactKind.ARCHIVE
    if family in {
        FormatFamily.FITS,
        FormatFamily.HDF5,
        FormatFamily.NETCDF,
        FormatFamily.GEORASTER,
        FormatFamily.SEQUENCE,
        FormatFamily.SCIENTIFIC_OTHER,
    }:
        return ArtifactKind.SCIENTIFIC_FILE
    return fallback


def _ooxml_family(
    prefix: bytes,
    *,
    tail: bytes,
    tail_offset: int,
    total_size: int,
) -> tuple[FormatFamily, str] | None:
    end_record = _find_zip_end_record(
        tail,
        tail_offset=tail_offset,
        total_size=total_size,
    )
    if end_record is None:
        return None
    entries = _parse_central_directory(
        tail,
        tail_offset=tail_offset,
        end_record=end_record,
    )
    if entries is None:
        return None
    visible_local_offsets = _visible_local_chain_offsets(
        prefix,
        entries=entries,
        central_directory_offset=end_record.central_directory_offset,
    )
    if visible_local_offsets is None:
        return None
    entries_by_name = {entry.name: entry for entry in entries}
    content_types = entries_by_name.get("[Content_Types].xml")
    candidates = (
        (
            "xl/workbook.xml",
            FormatFamily.XLSX,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        (
            "word/document.xml",
            FormatFamily.DOCX,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (
            "ppt/presentation.xml",
            FormatFamily.PPTX,
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
    )
    matched = [candidate for candidate in candidates if candidate[0] in entries_by_name]
    if content_types is None or len(matched) != 1:
        return None
    canonical_name, family, media_type = matched[0]
    canonical = entries_by_name[canonical_name]
    if not {
        content_types.local_header_offset,
        canonical.local_header_offset,
    }.issubset(visible_local_offsets):
        return None
    if not all(
        _local_entry_matches(
            prefix,
            entry=entry,
            central_directory_offset=end_record.central_directory_offset,
        )
        for entry in (content_types, canonical)
    ):
        return None
    return family, media_type


def _bounded_samples(
    content: bytes,
    max_sample_bytes: int,
) -> tuple[bytes, bytes, int, int]:
    """Allocate disjoint prefix/tail reads within one immutable byte budget."""

    budget = min(len(content), max_sample_bytes)
    if len(content) <= budget:
        return content, content, 0, budget
    if not content.startswith(_ZIP_LOCAL_FILE_HEADER):
        return content[:budget], b"", len(content), budget

    probe_tail_size = min(_ZIP_MAX_END_SEARCH_BYTES, max(22, budget // 2))
    tail = content[-probe_tail_size:]
    tail_offset = len(content) - len(tail)
    end_record = _find_zip_end_record(
        tail,
        tail_offset=tail_offset,
        total_size=len(content),
    )
    tail_size = len(tail)
    if end_record is not None:
        required_tail_size = len(content) - end_record.central_directory_offset
        if required_tail_size <= budget - len(_ZIP_LOCAL_FILE_HEADER):
            tail_size = max(tail_size, required_tail_size)
    prefix_size = budget - tail_size
    return (
        content[:prefix_size],
        content[-tail_size:],
        len(content) - tail_size,
        prefix_size + tail_size,
    )


def _find_zip_end_record(
    tail: bytes,
    *,
    tail_offset: int,
    total_size: int,
) -> _ZipEndRecord | None:
    if tail_offset + len(tail) != total_size:
        return None
    candidates: list[_ZipEndRecord] = []
    position = tail.rfind(_ZIP_END_OF_CENTRAL_DIRECTORY)
    while position >= 0:
        record_end = position + _ZIP_END_OF_CENTRAL_DIRECTORY_SIZE
        if record_end <= len(tail):
            record = tail[position:record_end]
            disk_number = int.from_bytes(record[4:6], "little")
            central_disk = int.from_bytes(record[6:8], "little")
            disk_entries = int.from_bytes(record[8:10], "little")
            total_entries = int.from_bytes(record[10:12], "little")
            central_size = int.from_bytes(record[12:16], "little")
            central_offset = int.from_bytes(record[16:20], "little")
            comment_size = int.from_bytes(record[20:22], "little")
            absolute_position = tail_offset + position
            if (
                record_end + comment_size == len(tail)
                and disk_number == 0
                and central_disk == 0
                and disk_entries == total_entries
                and 0 < total_entries < 0xFFFF
                and central_size != 0xFFFFFFFF
                and central_offset != 0xFFFFFFFF
                and central_offset + central_size == absolute_position
                and total_entries <= central_size // _ZIP_CENTRAL_DIRECTORY_HEADER_SIZE
            ):
                candidates.append(
                    _ZipEndRecord(
                        central_directory_offset=central_offset,
                        central_directory_size=central_size,
                        entry_count=total_entries,
                        record_offset=absolute_position,
                    )
                )
        position = tail.rfind(_ZIP_END_OF_CENTRAL_DIRECTORY, 0, position)
    return candidates[0] if len(candidates) == 1 else None


def _parse_central_directory(
    tail: bytes,
    *,
    tail_offset: int,
    end_record: _ZipEndRecord,
) -> tuple[_ZipCentralEntry, ...] | None:
    start = end_record.central_directory_offset - tail_offset
    end = start + end_record.central_directory_size
    if start < 0 or end > len(tail) or tail_offset + end != end_record.record_offset:
        return None
    entries: list[_ZipCentralEntry] = []
    names: set[str] = set()
    local_offsets: set[int] = set()
    position = start
    for _ in range(end_record.entry_count):
        header_end = position + _ZIP_CENTRAL_DIRECTORY_HEADER_SIZE
        if header_end > end or tail[position : position + 4] != _ZIP_CENTRAL_DIRECTORY_HEADER:
            return None
        header = tail[position:header_end]
        flags = int.from_bytes(header[8:10], "little")
        method = int.from_bytes(header[10:12], "little")
        crc32 = int.from_bytes(header[16:20], "little")
        compressed_size = int.from_bytes(header[20:24], "little")
        uncompressed_size = int.from_bytes(header[24:28], "little")
        name_size = int.from_bytes(header[28:30], "little")
        extra_size = int.from_bytes(header[30:32], "little")
        comment_size = int.from_bytes(header[32:34], "little")
        disk_start = int.from_bytes(header[34:36], "little")
        local_offset = int.from_bytes(header[42:46], "little")
        entry_end = header_end + name_size + extra_size + comment_size
        if (
            entry_end > end
            or flags & _ZIP_ENCRYPTION_FLAGS
            or method not in _ZIP_SUPPORTED_METHODS
            or name_size == 0
            or name_size > _ZIP_MEMBER_NAME_LIMIT
            or disk_start != 0
            or 0xFFFFFFFF in {compressed_size, uncompressed_size, local_offset}
            or local_offset + _ZIP_LOCAL_FILE_HEADER_SIZE > end_record.central_directory_offset
        ):
            return None
        raw_name = tail[header_end : header_end + name_size]
        extra = tail[header_end + name_size : header_end + name_size + extra_size]
        name = _decode_zip_name(raw_name, flags=flags)
        if (
            name is None
            or not _valid_zip_member_name(name)
            or not _valid_non_zip64_extra(extra)
            or name in names
            or local_offset in local_offsets
        ):
            return None
        names.add(name)
        local_offsets.add(local_offset)
        entries.append(
            _ZipCentralEntry(
                name=name,
                flags=flags,
                method=method,
                crc32=crc32,
                compressed_size=compressed_size,
                uncompressed_size=uncompressed_size,
                local_header_offset=local_offset,
            )
        )
        position = entry_end
    return tuple(entries) if position == end else None


def _visible_local_chain_offsets(
    prefix: bytes,
    *,
    entries: tuple[_ZipCentralEntry, ...],
    central_directory_offset: int,
) -> frozenset[int] | None:
    entries_by_offset = {entry.local_header_offset: entry for entry in entries}
    if not entries_by_offset or min(entries_by_offset) != 0:
        return None
    visible_offsets: set[int] = set()
    offset = 0
    while offset < len(prefix) and offset < central_directory_offset:
        entry = entries_by_offset.get(offset)
        local = _parse_local_entry(prefix, offset=offset)
        if entry is None or local is None or not _local_fields_match(local, entry):
            return None
        visible_offsets.add(offset)
        next_offset = local.payload_offset + entry.compressed_size
        if next_offset > central_directory_offset:
            return None
        if entry.flags & 0x8:
            descriptor_end = _data_descriptor_end(prefix, offset=next_offset, entry=entry)
            if descriptor_end is None:
                return frozenset(visible_offsets) if next_offset >= len(prefix) else None
            next_offset = descriptor_end
        if next_offset >= len(prefix) or next_offset == central_directory_offset:
            return frozenset(visible_offsets)
        offset = next_offset
    return frozenset(visible_offsets)


def _local_entry_matches(
    prefix: bytes,
    *,
    entry: _ZipCentralEntry,
    central_directory_offset: int,
) -> bool:
    local = _parse_local_entry(prefix, offset=entry.local_header_offset)
    if local is None or not _local_fields_match(local, entry):
        return False
    return local.payload_offset + entry.compressed_size <= central_directory_offset


def _parse_local_entry(prefix: bytes, *, offset: int) -> _ZipLocalEntry | None:
    header_end = offset + _ZIP_LOCAL_FILE_HEADER_SIZE
    if header_end > len(prefix) or prefix[offset : offset + 4] != _ZIP_LOCAL_FILE_HEADER:
        return None
    header = prefix[offset:header_end]
    flags = int.from_bytes(header[6:8], "little")
    method = int.from_bytes(header[8:10], "little")
    crc32 = int.from_bytes(header[14:18], "little")
    compressed_size = int.from_bytes(header[18:22], "little")
    uncompressed_size = int.from_bytes(header[22:26], "little")
    name_size = int.from_bytes(header[26:28], "little")
    extra_size = int.from_bytes(header[28:30], "little")
    payload_offset = header_end + name_size + extra_size
    if (
        payload_offset > len(prefix)
        or flags & _ZIP_ENCRYPTION_FLAGS
        or method not in _ZIP_SUPPORTED_METHODS
        or name_size == 0
        or name_size > _ZIP_MEMBER_NAME_LIMIT
        or 0xFFFFFFFF in {compressed_size, uncompressed_size}
    ):
        return None
    raw_name = prefix[header_end : header_end + name_size]
    extra = prefix[header_end + name_size : payload_offset]
    name = _decode_zip_name(raw_name, flags=flags)
    if name is None or not _valid_zip_member_name(name) or not _valid_non_zip64_extra(extra):
        return None
    return _ZipLocalEntry(
        name=name,
        flags=flags,
        method=method,
        crc32=crc32,
        compressed_size=compressed_size,
        uncompressed_size=uncompressed_size,
        payload_offset=payload_offset,
    )


def _local_fields_match(local: _ZipLocalEntry, central: _ZipCentralEntry) -> bool:
    if local.name != central.name or local.flags != central.flags or local.method != central.method:
        return False
    if local.flags & 0x8:
        return (
            local.crc32 in {0, central.crc32}
            and local.compressed_size in {0, central.compressed_size}
            and local.uncompressed_size in {0, central.uncompressed_size}
        )
    return (
        local.crc32 == central.crc32
        and local.compressed_size == central.compressed_size
        and local.uncompressed_size == central.uncompressed_size
    )


def _data_descriptor_end(
    prefix: bytes,
    *,
    offset: int,
    entry: _ZipCentralEntry,
) -> int | None:
    signature_size = 4 if prefix[offset : offset + 4] == b"PK\x07\x08" else 0
    descriptor_end = offset + signature_size + 12
    if descriptor_end > len(prefix):
        return None
    descriptor = prefix[offset + signature_size : descriptor_end]
    values = (
        int.from_bytes(descriptor[0:4], "little"),
        int.from_bytes(descriptor[4:8], "little"),
        int.from_bytes(descriptor[8:12], "little"),
    )
    expected = (entry.crc32, entry.compressed_size, entry.uncompressed_size)
    return descriptor_end if values == expected else None


def _decode_zip_name(raw_name: bytes, *, flags: int) -> str | None:
    try:
        return raw_name.decode("utf-8" if flags & 0x800 else "cp437")
    except UnicodeDecodeError:
        return None


def _valid_zip_member_name(name: str) -> bool:
    trimmed = name[:-1] if name.endswith("/") else name
    parts = trimmed.split("/")
    return (
        bool(trimmed)
        and "\\" not in name
        and not name.startswith("/")
        and all(part not in {"", ".", ".."} and "\x00" not in part for part in parts)
    )


def _valid_non_zip64_extra(extra: bytes) -> bool:
    offset = 0
    while offset < len(extra):
        if offset + 4 > len(extra):
            return False
        field_id = int.from_bytes(extra[offset : offset + 2], "little")
        field_size = int.from_bytes(extra[offset + 2 : offset + 4], "little")
        offset += 4
        if field_id == _ZIP64_EXTRA_FIELD_ID or offset + field_size > len(extra):
            return False
        offset += field_size
    return True


def _is_valid_zip(sample: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(sample)) as archive:
            archive.infolist()
    except (OSError, zipfile.BadZipFile):
        return False
    return True


def _looks_like_sequence(sample: bytes) -> bool:
    try:
        lines = sample.decode("ascii").splitlines()
    except UnicodeDecodeError:
        return False
    if len(lines) < 2 or not lines[0].startswith(">"):
        return False
    sequence = "".join(line.strip() for line in lines[1:4]).upper()
    return bool(sequence) and set(sequence) <= set("ACGTUNRYKMSWBDHV-*")


def _is_compatible_refinement(prior_media: str, classified_media: str) -> bool:
    if prior_media == "application/octet-stream":
        return True
    if prior_media == "application/zip" and classified_media.startswith(
        "application/vnd.openxmlformats-officedocument"
    ):
        return True
    return prior_media == "text/plain" and classified_media in {
        "application/xml",
        "application/x-netcdf",
        "text/x-fasta",
    }

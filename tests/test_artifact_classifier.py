from __future__ import annotations

import hashlib
import io
import os
import struct
import subprocess
import zipfile
import zlib
from datetime import UTC, datetime

import pytest

from scidatafusion.artifacts.sniffer import ContentSniffer
from scidatafusion.contracts.artifacts import ArtifactKind, BronzeObject
from scidatafusion.contracts.parsing import (
    ClassificationBasis,
    ClassificationReviewCode,
    FormatFamily,
    ParsePlanningPolicy,
)
from scidatafusion.parsing.classifier import (
    BoundedStructuralFeatureProbe,
    DeterministicArtifactClassifier,
)

NOW = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)


def _bronze(
    content: bytes,
    *,
    declared_media_type: str | None = None,
) -> BronzeObject:
    byte_sha256 = hashlib.sha256(content).hexdigest()
    return BronzeObject(
        object_id=f"brz_{byte_sha256[:32]}",
        byte_sha256=byte_sha256,
        size_bytes=len(content),
        storage_uri=f"bronze://sha256/{byte_sha256}",
        media=ContentSniffer.inspect(
            content,
            declared_media_type=declared_media_type,
        ),
        recorded_at=NOW,
        object_metadata_hash="a" * 64,
    )


def _zip(*names: str) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for name in names:
            archive.writestr(name, b"fixture")
    return payload.getvalue()


def _large_truncated_pptx(*, size: int) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("ppt/presentation.xml", b"<p:presentation/>")
        archive.writestr("ppt/media/large.bin", b"x" * size)
    return payload.getvalue()


def _overlapping_ooxml_headers_zip() -> bytes:
    def local_entry(name: bytes, data: bytes) -> bytes:
        crc32 = zlib.crc32(data)
        header = struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50,
            20,
            0,
            0,
            0,
            0,
            crc32,
            len(data),
            len(data),
            len(name),
            0,
        )
        return header + name + data

    fake_content_types = local_entry(b"[Content_Types].xml", b"x")
    fake_presentation = local_entry(b"ppt/presentation.xml", b"y")
    padding = b"payload-prefix"
    outer_name = b"payload.bin"
    outer_payload = padding + fake_content_types + fake_presentation
    outer = local_entry(outer_name, outer_payload)
    outer_payload_offset = 30 + len(outer_name)
    fake_content_types_offset = outer_payload_offset + len(padding)
    fake_presentation_offset = fake_content_types_offset + len(fake_content_types)
    central_offset = len(outer)

    central_entries: list[bytes] = []
    for name, data, local_offset in (
        (outer_name, outer_payload, 0),
        (b"[Content_Types].xml", b"x", fake_content_types_offset),
        (b"ppt/presentation.xml", b"y", fake_presentation_offset),
    ):
        central_entries.append(
            struct.pack(
                "<IHHHHHHIIIHHHHHII",
                0x02014B50,
                20,
                20,
                0,
                0,
                0,
                0,
                zlib.crc32(data),
                len(data),
                len(data),
                len(name),
                0,
                0,
                0,
                0,
                0,
                local_offset,
            )
            + name
        )
    central = b"".join(central_entries)
    end = struct.pack(
        "<IHHHHIIH",
        0x06054B50,
        0,
        0,
        len(central_entries),
        len(central_entries),
        len(central),
        central_offset,
        0,
    )
    return outer + central + end


@pytest.mark.parametrize(
    ("content", "expected_family", "expected_media_type", "expected_kind"),
    [
        (
            b"%PDF-1.7\n1 0 obj\n<< /Type /Page >>\nendobj\n%%EOF\n",
            FormatFamily.PDF,
            "application/pdf",
            ArtifactKind.DOCUMENT,
        ),
        (
            b"object_id,flux\nSN-1,12.5\nSN-2,13.1\n",
            FormatFamily.CSV,
            "text/csv",
            ArtifactKind.TABLE,
        ),
        (
            b"<!doctype html><html><body>landing page</body></html>",
            FormatFamily.HTML,
            "text/html",
            ArtifactKind.LANDING_PAGE,
        ),
        (
            b"deterministic scientific notes\nwithout extracted values\n",
            FormatFamily.PLAIN_TEXT,
            "text/plain",
            ArtifactKind.DOCUMENT,
        ),
        (
            _zip("data/readme.txt"),
            FormatFamily.ARCHIVE,
            "application/zip",
            ArtifactKind.ARCHIVE,
        ),
        (
            b"SIMPLE  =                    T" + b" " * 256,
            FormatFamily.FITS,
            "application/fits",
            ArtifactKind.SCIENTIFIC_FILE,
        ),
        (
            b"\x89HDF\r\n\x1a\n" + b"\x00" * 64,
            FormatFamily.HDF5,
            "application/x-hdf5",
            ArtifactKind.SCIENTIFIC_FILE,
        ),
        (
            b"CDF\x02" + b"\x00" * 64,
            FormatFamily.NETCDF,
            "application/x-netcdf",
            ArtifactKind.SCIENTIFIC_FILE,
        ),
        (
            b'{"rows":[{"id":1}]}',
            FormatFamily.JSON,
            "application/json",
            ArtifactKind.TABLE,
        ),
        (
            b"PAR1fixturePAR1",
            FormatFamily.PARQUET,
            "application/vnd.apache.parquet",
            ArtifactKind.TABLE,
        ),
        (
            b"\x89PNG\r\n\x1a\n" + b"fixture",
            FormatFamily.IMAGE,
            "image/png",
            ArtifactKind.IMAGE,
        ),
        (
            b"\x1f\x8b" + b"fixture",
            FormatFamily.ARCHIVE,
            "application/gzip",
            ArtifactKind.ARCHIVE,
        ),
        (
            b"<?xml version='1.0'?><root/>",
            FormatFamily.XML,
            "application/xml",
            ArtifactKind.DOCUMENT,
        ),
        (
            b">sequence\nACGTNRYKMSWBDHV\n",
            FormatFamily.SEQUENCE,
            "text/x-fasta",
            ArtifactKind.SCIENTIFIC_FILE,
        ),
    ],
)
def test_signature_first_classifier_recognizes_supported_families(
    content: bytes,
    expected_family: FormatFamily,
    expected_media_type: str,
    expected_kind: ArtifactKind,
) -> None:
    decision = DeterministicArtifactClassifier().classify(
        _bronze(content),
        content,
        ParsePlanningPolicy(),
    )

    assert decision.format_family is expected_family
    assert decision.classified_media_type == expected_media_type
    assert decision.artifact_kind is expected_kind
    assert decision.features.sampled_bytes == len(content)
    assert decision.confidence > 0.0


@pytest.mark.parametrize(
    ("member", "expected_family", "expected_media_type", "expected_kind"),
    [
        (
            "word/document.xml",
            FormatFamily.DOCX,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ArtifactKind.DOCUMENT,
        ),
        (
            "xl/workbook.xml",
            FormatFamily.XLSX,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ArtifactKind.TABLE,
        ),
        (
            "ppt/presentation.xml",
            FormatFamily.PPTX,
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ArtifactKind.DOCUMENT,
        ),
    ],
)
def test_complete_ooxml_containers_are_classified_by_internal_structure(
    member: str,
    expected_family: FormatFamily,
    expected_media_type: str,
    expected_kind: ArtifactKind,
) -> None:
    content = _zip("[Content_Types].xml", member)

    decision = DeterministicArtifactClassifier().classify(
        _bronze(content),
        content,
        ParsePlanningPolicy(),
    )

    assert decision.format_family is expected_family
    assert decision.classified_media_type == expected_media_type
    assert decision.artifact_kind is expected_kind
    assert ClassificationBasis.STRUCTURAL_PROBE in decision.basis


def test_unknown_binary_remains_unknown_and_requires_review() -> None:
    content = b"\x00\x01\x02\x03unrecognized\xff"

    decision = DeterministicArtifactClassifier().classify(
        _bronze(content),
        content,
        ParsePlanningPolicy(),
    )

    assert decision.format_family is FormatFamily.UNKNOWN
    assert decision.artifact_kind is ArtifactKind.UNKNOWN
    assert decision.classified_media_type == "application/octet-stream"
    assert decision.confidence == 0.0
    assert decision.review_codes == (ClassificationReviewCode.UNKNOWN_FORMAT,)


def test_spoofed_declared_mime_cannot_override_pdf_signature() -> None:
    content = b"%PDF-1.7\n1 0 obj\n<< /Type /Page >>\nendobj\n%%EOF\n"

    decision = DeterministicArtifactClassifier().classify(
        _bronze(content, declared_media_type="text/html"),
        content,
        ParsePlanningPolicy(),
    )

    assert decision.format_family is FormatFamily.PDF
    assert decision.classified_media_type == "application/pdf"
    assert decision.source_media_type_mismatch
    assert ClassificationReviewCode.MEDIA_TYPE_MISMATCH in decision.review_codes


def test_malicious_html_is_inert_and_body_is_not_retained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "M08_BODY_MUST_NOT_SURVIVE"
    attempted_execution: list[object] = []

    def reject_execution(*args: object, **kwargs: object) -> None:
        attempted_execution.extend((args, kwargs))
        raise AssertionError("classification must not execute HTML content")

    monkeypatch.setattr(os, "system", reject_execution)
    monkeypatch.setattr(subprocess, "run", reject_execution)
    content = (
        "<!doctype html><html><body>"
        "<script>require('child_process').exec('malicious command')</script>"
        f"<p>{marker}</p></body></html>"
    ).encode()

    decision = DeterministicArtifactClassifier().classify(
        _bronze(content),
        content,
        ParsePlanningPolicy(),
    )

    assert decision.format_family is FormatFamily.HTML
    assert attempted_execution == []
    assert marker not in repr(decision)
    assert not hasattr(decision, "content")
    assert not hasattr(decision.features, "text")


def test_pdf_encryption_damage_and_page_metadata_are_explicit() -> None:
    classifier = DeterministicArtifactClassifier()
    encrypted = b"%PDF-1.7\n/Encrypt 2 0 R\n/Type /Page\n%%EOF\n"
    encrypted_decision = classifier.classify(
        _bronze(encrypted),
        encrypted,
        ParsePlanningPolicy(),
    )
    damaged = b"%PDF-1.7\n/Type /Page\n"
    damaged_decision = classifier.classify(
        _bronze(damaged),
        damaged,
        ParsePlanningPolicy(),
    )

    assert encrypted_decision.features.encrypted is True
    assert encrypted_decision.features.total_pages == 1
    assert ClassificationReviewCode.NEEDS_PASSWORD in encrypted_decision.review_codes
    assert damaged_decision.features.damaged is True
    assert ClassificationReviewCode.DAMAGED_FILE in damaged_decision.review_codes


def test_structural_probe_rejects_invalid_pdf_and_zip_without_executing_them() -> None:
    probe = BoundedStructuralFeatureProbe()

    pdf = probe.inspect(
        b"not-a-pdf",
        total_size=9,
        media_type="application/pdf",
        format_family=FormatFamily.PDF,
        max_pages=1,
    )
    archive = probe.inspect(
        b"PK\x03\x04broken",
        total_size=10,
        media_type="application/zip",
        format_family=FormatFamily.ARCHIVE,
        max_pages=1,
    )

    assert pdf.damaged is True
    assert archive.damaged is True


def test_empty_or_truncated_unknown_sample_is_never_promoted() -> None:
    classifier = DeterministicArtifactClassifier()
    prior = _bronze(b"prior")

    empty = classifier.classify(prior, b"", ParsePlanningPolicy())
    truncated_content = b"\x00" * 2048
    truncated = classifier.classify(
        _bronze(truncated_content),
        truncated_content,
        ParsePlanningPolicy(max_sample_bytes_per_artifact=1024),
    )

    assert ClassificationReviewCode.SAMPLE_INSUFFICIENT in empty.review_codes
    assert truncated.format_family is FormatFamily.UNKNOWN
    assert ClassificationReviewCode.SAMPLE_INSUFFICIENT in truncated.review_codes


def test_truncated_pptx_cross_checks_tail_central_directory_within_budget() -> None:
    policy = ParsePlanningPolicy(max_sample_bytes_per_artifact=1024)
    content = _large_truncated_pptx(size=policy.max_sample_bytes_per_artifact * 2)

    decision = DeterministicArtifactClassifier().classify(
        _bronze(content),
        content,
        policy,
    )

    assert len(content) > policy.max_sample_bytes_per_artifact
    assert decision.features.sampled_bytes == policy.max_sample_bytes_per_artifact
    assert decision.format_family is FormatFamily.PPTX
    assert (
        decision.classified_media_type
        == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    assert ClassificationBasis.STRUCTURAL_PROBE in decision.basis
    assert decision.confidence == 0.95
    assert ClassificationReviewCode.UNKNOWN_FORMAT not in decision.review_codes


def test_isolated_fake_ooxml_local_headers_cannot_promote_archive() -> None:
    policy = ParsePlanningPolicy(max_sample_bytes_per_artifact=1024)
    content = bytearray(_large_truncated_pptx(size=policy.max_sample_bytes_per_artifact * 2))
    for canonical in (b"[Content_Types].xml", b"ppt/presentation.xml"):
        central_name = content.rfind(canonical)
        assert central_name >= 0
        content[central_name : central_name + len(canonical)] = b"x" * len(canonical)

    decision = DeterministicArtifactClassifier().classify(
        _bronze(bytes(content)),
        bytes(content),
        policy,
    )

    assert decision.features.sampled_bytes <= policy.max_sample_bytes_per_artifact
    assert decision.format_family is FormatFamily.ARCHIVE
    assert decision.classified_media_type == "application/zip"
    assert ClassificationReviewCode.SAMPLE_INSUFFICIENT in decision.review_codes


def test_overlapping_central_entries_cannot_promote_payload_headers() -> None:
    content = _overlapping_ooxml_headers_zip()

    decision = DeterministicArtifactClassifier().classify(
        _bronze(content),
        content,
        ParsePlanningPolicy(),
    )

    assert decision.format_family is FormatFamily.ARCHIVE
    assert decision.classified_media_type == "application/zip"


@pytest.mark.parametrize(
    "members",
    [
        ("ppt/presentation.xml",),
        ("[Content_Types].xml", "ppt/slides/slide1.xml"),
    ],
)
def test_zip_with_incomplete_ooxml_markers_remains_archive(
    members: tuple[str, ...],
) -> None:
    content = _zip(*members)

    decision = DeterministicArtifactClassifier().classify(
        _bronze(content),
        content,
        ParsePlanningPolicy(),
    )

    assert decision.format_family is FormatFamily.ARCHIVE
    assert decision.classified_media_type == "application/zip"

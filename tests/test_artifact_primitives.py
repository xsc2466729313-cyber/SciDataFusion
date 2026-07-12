from __future__ import annotations

import hashlib
import io
import stat
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from scidatafusion.artifacts import (
    ContentSniffer,
    FileSystemBronzeStore,
    MemoryBronzeStore,
    SafeArchiveInspector,
)
from scidatafusion.contracts.artifacts import (
    ArtifactKind,
    ContentDetectionBasis,
    DownloadPolicy,
)
from scidatafusion.errors import AppError, ErrorCode


def _zip(entries: list[tuple[str, bytes]], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def _policy(**updates: object) -> DownloadPolicy:
    values: dict[str, object] = {
        "max_total_bytes": 1_000_000,
        "max_file_bytes": 1_000_000,
        "max_archive_uncompressed_bytes": 1_000_000,
        "max_archive_member_bytes": 1_000_000,
    }
    values.update(updates)
    return DownloadPolicy(**values)  # type: ignore[arg-type]


def test_memory_bronze_store_is_content_addressed_deduplicated_and_concurrent() -> None:
    store = MemoryBronzeStore()
    content = b"immutable scientific source bytes\n"
    expected_hash = hashlib.sha256(content).hexdigest()

    first = store.put(content)
    second = store.put(content)

    assert first.byte_sha256 == expected_hash
    assert first.storage_uri == f"bronze://sha256/{expected_hash}"
    assert first.newly_stored
    assert not second.newly_stored
    assert store.read(expected_hash) == content
    assert store.contains(expected_hash)
    assert not store.contains("f" * 64)

    concurrent_store = MemoryBronzeStore()
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = tuple(pool.map(concurrent_store.put, (content,) * 16))
    assert sum(item.newly_stored for item in receipts) == 1
    assert {item.byte_sha256 for item in receipts} == {expected_hash}


def test_bronze_stores_reject_empty_invalid_and_tampered_objects(tmp_path: Path) -> None:
    memory = MemoryBronzeStore()
    with pytest.raises(AppError) as empty_error:
        memory.put(b"")
    assert empty_error.value.code is ErrorCode.VALIDATION_FAILED
    with pytest.raises(AppError) as invalid_hash:
        memory.read("NOT-A-HASH")
    assert invalid_hash.value.code is ErrorCode.INVALID_REQUEST

    store = FileSystemBronzeStore(tmp_path / "bronze")
    content = b"source replay bytes"
    receipt = store.put(content)
    assert store.read(receipt.byte_sha256) == content
    assert not store.put(content).newly_stored

    target = tmp_path / "bronze" / "sha256" / receipt.byte_sha256[:2] / receipt.byte_sha256
    target.write_bytes(b"tampered")
    with pytest.raises(AppError) as tampered:
        store.read(receipt.byte_sha256)
    assert tampered.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR

    invalid_root = tmp_path / "not-a-directory"
    invalid_root.write_text("file", encoding="utf-8")
    with pytest.raises(AppError) as invalid_root_error:
        FileSystemBronzeStore(invalid_root)
    assert invalid_root_error.value.code is ErrorCode.CONFIGURATION_ERROR


@pytest.mark.parametrize(
    ("content", "media_type", "kind", "basis"),
    [
        (b"%PDF-1.7\n", "application/pdf", ArtifactKind.DOCUMENT, "magic_bytes"),
        (b"\x89PNG\r\n\x1a\nrest", "image/png", ArtifactKind.IMAGE, "magic_bytes"),
        (b"\xff\xd8\xffrest", "image/jpeg", ArtifactKind.IMAGE, "magic_bytes"),
        (b"II*\x00rest", "image/tiff", ArtifactKind.IMAGE, "magic_bytes"),
        (
            b"\x89HDF\r\n\x1a\nrest",
            "application/x-hdf5",
            ArtifactKind.SCIENTIFIC_FILE,
            "magic_bytes",
        ),
        (
            b"SIMPLE  =                    T",
            "application/fits",
            ArtifactKind.SCIENTIFIC_FILE,
            "structural_probe",
        ),
        (
            b"PAR1metadataPAR1",
            "application/vnd.apache.parquet",
            ArtifactKind.TABLE,
            "structural_probe",
        ),
        (b"\x1f\x8bcompressed", "application/gzip", ArtifactKind.ARCHIVE, "magic_bytes"),
        (b"<html><body>data</body></html>", "text/html", ArtifactKind.LANDING_PAGE, "text_probe"),
        (b'{"rows":[1,2]}', "application/json", ArtifactKind.TABLE, "structural_probe"),
        (b"x,y\n1,2\n", "text/csv", ArtifactKind.TABLE, "structural_probe"),
        (b"plain scientific notes", "text/plain", ArtifactKind.DOCUMENT, "text_probe"),
    ],
)
def test_content_sniffer_uses_bytes_not_names(
    content: bytes,
    media_type: str,
    kind: ArtifactKind,
    basis: str,
) -> None:
    inspection = ContentSniffer.inspect(content)

    assert inspection.detected_media_type == media_type
    assert inspection.artifact_kind is kind
    assert inspection.basis.value == basis
    assert not inspection.media_type_mismatch


def test_content_sniffer_detects_office_zip_spoofing_and_unknown_bytes() -> None:
    xlsx = _zip(
        [
            ("[Content_Types].xml", b"types"),
            ("xl/workbook.xml", b"workbook"),
        ]
    )
    docx = _zip(
        [
            ("[Content_Types].xml", b"types"),
            ("word/document.xml", b"document"),
        ]
    )

    assert ContentSniffer.inspect(xlsx).detected_media_type.endswith("spreadsheetml.sheet")
    assert ContentSniffer.inspect(docx).detected_media_type.endswith("wordprocessingml.document")
    assert (
        ContentSniffer.inspect(_zip([("data.csv", b"x,y\n1,2\n")])).artifact_kind
        is ArtifactKind.ARCHIVE
    )

    spoofed = ContentSniffer.inspect(
        b"%PDF-1.7\n",
        declared_media_type="text/html; charset=utf-8",
    )
    assert spoofed.detected_media_type == "application/pdf"
    assert spoofed.declared_media_type == "text/html"
    assert spoofed.media_type_mismatch

    unknown = ContentSniffer.inspect(b"\x00\x01\x02\x03")
    assert unknown.detected_media_type == "application/octet-stream"
    assert unknown.basis is ContentDetectionBasis.UNKNOWN
    assert unknown.confidence == 0.0


def test_safe_archive_extracts_regular_members_without_writing(tmp_path: Path) -> None:
    content = _zip(
        [
            ("tables/", b""),
            ("tables/data.csv", b"x,y\n1,2\n"),
            ("README.txt", b"source notes"),
        ]
    )

    members = SafeArchiveInspector.extract_zip(content, _policy())

    assert [(item.path, item.content) for item in members] == [
        ("tables/data.csv", b"x,y\n1,2\n"),
        ("README.txt", b"source notes"),
    ]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "member_path",
    [
        "../escape.csv",
        "/absolute.csv",
        "folder\\escape.csv",
        "C:/escape.csv",
        "folder//not-normalized.csv",
    ],
)
def test_archive_rejects_unsafe_member_paths(member_path: str) -> None:
    if "\\" in member_path:
        normalized = member_path.replace("\\", "/")
        content = _zip([(normalized, b"payload")]).replace(
            normalized.encode(),
            member_path.encode(),
        )
    else:
        content = _zip([(member_path, b"payload")])
    with pytest.raises(AppError, match="safe normalized") as exc_info:
        SafeArchiveInspector.extract_zip(content, _policy())
    assert exc_info.value.code is ErrorCode.SECURITY_POLICY_VIOLATION


def test_archive_rejects_duplicates_symlinks_bombs_limits_and_corruption() -> None:
    duplicate_buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(duplicate_buffer, "w") as archive:
            archive.writestr("same.csv", b"one")
            archive.writestr("same.csv", b"two")
    with pytest.raises(AppError, match="duplicate normalized"):
        SafeArchiveInspector.extract_zip(duplicate_buffer.getvalue(), _policy())

    symlink_buffer = io.BytesIO()
    symlink = zipfile.ZipInfo("link")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(symlink_buffer, "w") as archive:
        archive.writestr(symlink, "target")
    with pytest.raises(AppError, match="symbolic-link"):
        SafeArchiveInspector.extract_zip(symlink_buffer.getvalue(), _policy())

    fifo_buffer = io.BytesIO()
    fifo = zipfile.ZipInfo("pipe")
    fifo.create_system = 3
    fifo.external_attr = (stat.S_IFIFO | 0o600) << 16
    with zipfile.ZipFile(fifo_buffer, "w") as archive:
        archive.writestr(fifo, b"")
    with pytest.raises(AppError, match="non-regular"):
        SafeArchiveInspector.extract_zip(fifo_buffer.getvalue(), _policy())

    many = _zip([("one", b"1"), ("two", b"2")])
    with pytest.raises(AppError, match="too many"):
        SafeArchiveInspector.extract_zip(many, _policy(max_archive_entries=1))

    oversized = _zip([("large.bin", b"x" * 101)], compression=zipfile.ZIP_STORED)
    with pytest.raises(AppError, match="member exceeds"):
        SafeArchiveInspector.extract_zip(
            oversized,
            _policy(
                max_archive_member_bytes=100,
                max_archive_uncompressed_bytes=1000,
            ),
        )

    compressed = _zip([("bomb.txt", b"0" * 10_000)])
    with pytest.raises(AppError, match="compression ratio"):
        SafeArchiveInspector.extract_zip(
            compressed,
            _policy(max_archive_compression_ratio=2.0),
        )

    corrupt = bytearray(_zip([("data.bin", b"unique-payload")], compression=zipfile.ZIP_STORED))
    payload_offset = corrupt.find(b"unique-payload")
    assert payload_offset >= 0
    corrupt[payload_offset] ^= 0xFF
    with pytest.raises(AppError, match="CRC"):
        SafeArchiveInspector.extract_zip(bytes(corrupt), _policy())

    with pytest.raises(AppError, match="valid ZIP"):
        SafeArchiveInspector.extract_zip(b"PK\x03\x04broken", _policy())
    with pytest.raises(AppError, match="nesting depth"):
        SafeArchiveInspector.extract_zip(_zip([("x", b"y")]), _policy(max_archive_depth=0), depth=1)

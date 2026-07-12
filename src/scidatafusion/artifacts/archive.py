"""Bounded in-memory ZIP inspection that never writes or executes archive members."""

from __future__ import annotations

import io
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import NoReturn

from scidatafusion.contracts.artifacts import DownloadPolicy
from scidatafusion.errors import AppError, ErrorCode


@dataclass(frozen=True, slots=True)
class ExtractedArchiveMember:
    """One validated regular ZIP member retained as inert bytes."""

    path: str
    content: bytes
    compressed_size: int
    uncompressed_size: int
    crc32: int


class SafeArchiveInspector:
    """Validate and extract regular ZIP members under explicit anti-bomb limits."""

    @staticmethod
    def extract_zip(
        content: bytes,
        policy: DownloadPolicy,
        *,
        depth: int = 0,
    ) -> tuple[ExtractedArchiveMember, ...]:
        """Return inert members or reject the complete archive without partial output."""

        if depth > policy.max_archive_depth:
            _reject("archive nesting depth exceeds the configured limit")
        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
        except (OSError, zipfile.BadZipFile) as exc:
            _reject("downloaded bytes are not a valid ZIP archive", cause=exc)
        with archive:
            entries = archive.infolist()
            if len(entries) > policy.max_archive_entries:
                _reject("ZIP archive contains too many entries")
            members: list[ExtractedArchiveMember] = []
            seen_paths: set[str] = set()
            declared_total = 0
            actual_total = 0
            for entry in entries:
                path = _safe_member_path(entry.orig_filename)
                if path in seen_paths:
                    _reject("ZIP archive contains duplicate normalized member paths")
                seen_paths.add(path)
                if entry.is_dir():
                    continue
                if entry.flag_bits & 0x1:
                    _reject("encrypted ZIP members are not accepted")
                unix_mode = (entry.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(unix_mode)
                if unix_mode and stat.S_ISLNK(unix_mode):
                    _reject("symbolic-link ZIP members are not accepted")
                if file_type and file_type != stat.S_IFREG:
                    _reject("non-regular ZIP members are not accepted")
                if entry.file_size > policy.max_archive_member_bytes:
                    _reject("ZIP member exceeds the configured uncompressed byte limit")
                declared_total += entry.file_size
                if declared_total > policy.max_archive_uncompressed_bytes:
                    _reject("ZIP archive exceeds the total uncompressed byte limit")
                ratio = entry.file_size / max(entry.compress_size, 1)
                if ratio > policy.max_archive_compression_ratio:
                    _reject("ZIP member exceeds the configured compression ratio")
                try:
                    member_content = archive.read(entry)
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    _reject("ZIP member failed CRC or decompression validation", cause=exc)
                if len(member_content) != entry.file_size:
                    _reject("ZIP member size differs from its central-directory declaration")
                actual_total += len(member_content)
                if actual_total > policy.max_archive_uncompressed_bytes:
                    _reject("ZIP extraction exceeded the total uncompressed byte limit")
                members.append(
                    ExtractedArchiveMember(
                        path=path,
                        content=member_content,
                        compressed_size=entry.compress_size,
                        uncompressed_size=len(member_content),
                        crc32=entry.CRC,
                    )
                )
        return tuple(members)


def _safe_member_path(value: str) -> str:
    path = PurePosixPath(value)
    normalized = str(path)
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or value.startswith("/")
        or path.is_absolute()
        or normalized != value.rstrip("/")
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in path.parts[0]
    ):
        _reject("ZIP member path is not a safe normalized relative POSIX path")
    return normalized


def _reject(message: str, *, cause: BaseException | None = None) -> NoReturn:
    error = AppError(ErrorCode.SECURITY_POLICY_VIOLATION, message)
    if cause is not None:
        raise error from cause
    raise error

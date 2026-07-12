"""Content-addressed immutable byte stores for the M07 Bronze layer."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Protocol

from scidatafusion.errors import AppError, ErrorCode


@dataclass(frozen=True, slots=True)
class BronzeWriteReceipt:
    """Stable address and deduplication outcome for one immutable byte sequence."""

    byte_sha256: str
    size_bytes: int
    storage_uri: str
    newly_stored: bool


class BronzeByteStore(Protocol):
    """Minimal immutable object-store boundary consumed by the M07 service."""

    def put(self, content: bytes) -> BronzeWriteReceipt:
        """Persist bytes exactly once and return their content address."""

    def read(self, byte_sha256: str) -> bytes:
        """Replay bytes only after verifying their content address."""

    def contains(self, byte_sha256: str) -> bool:
        """Return whether one valid content-addressed object exists."""


class MemoryBronzeStore:
    """Thread-safe immutable store for tests and explicitly ephemeral workflows."""

    def __init__(self, *, max_total_bytes: int = 128_000_000) -> None:
        if not 1 <= max_total_bytes <= 1_000_000_000:
            raise AppError(
                ErrorCode.CONFIGURATION_ERROR,
                "Memory Bronze capacity must be between 1 byte and 1 GB",
            )
        self._max_total_bytes = max_total_bytes
        self._stored_bytes = 0
        self._objects: dict[str, bytes] = {}
        self._lock = RLock()

    def put(self, content: bytes) -> BronzeWriteReceipt:
        """Store a non-empty immutable byte copy or report content deduplication."""

        _require_non_empty_bytes(content)
        byte_sha256 = hashlib.sha256(content).hexdigest()
        immutable_content = bytes(content)
        with self._lock:
            existing = self._objects.get(byte_sha256)
            if existing is not None and existing != immutable_content:
                _integrity_error("SHA-256 collision detected in the Bronze byte store")
            newly_stored = existing is None
            if newly_stored:
                if self._stored_bytes + len(immutable_content) > self._max_total_bytes:
                    raise AppError(
                        ErrorCode.BUDGET_EXCEEDED,
                        "Memory Bronze capacity would be exceeded",
                    )
                self._objects[byte_sha256] = immutable_content
                self._stored_bytes += len(immutable_content)
        return _receipt(byte_sha256, len(immutable_content), newly_stored)

    def read(self, byte_sha256: str) -> bytes:
        """Return an immutable copy after verifying the stored bytes."""

        _require_sha256(byte_sha256)
        with self._lock:
            content = self._objects.get(byte_sha256)
        if content is None:
            raise AppError(ErrorCode.INVALID_REQUEST, "Bronze object does not exist")
        _verify_content_hash(content, byte_sha256)
        return bytes(content)

    def contains(self, byte_sha256: str) -> bool:
        """Check existence and integrity without exposing stored bytes."""

        try:
            self.read(byte_sha256)
        except AppError as exc:
            if exc.code is ErrorCode.INVALID_REQUEST:
                return False
            raise
        return True


class FileSystemBronzeStore:
    """Local durable store using atomic, no-overwrite hard-link publication."""

    def __init__(self, root: Path) -> None:
        try:
            root.mkdir(parents=True, exist_ok=True)
            self._root = root.resolve(strict=True)
        except OSError as exc:
            raise AppError(
                ErrorCode.CONFIGURATION_ERROR,
                "Bronze root could not be initialized",
            ) from exc
        if not self._root.is_dir():
            raise AppError(ErrorCode.CONFIGURATION_ERROR, "Bronze root must be a directory")
        self._objects_root = self._root / "sha256"
        self._objects_root.mkdir(exist_ok=True)
        if self._objects_root.is_symlink():
            raise AppError(
                ErrorCode.SECURITY_POLICY_VIOLATION,
                "Bronze object directory cannot be a symbolic link",
            )
        self._lock = RLock()

    def put(self, content: bytes) -> BronzeWriteReceipt:
        """Publish bytes atomically without replacing an existing content address."""

        _require_non_empty_bytes(content)
        byte_sha256 = hashlib.sha256(content).hexdigest()
        target = self._target(byte_sha256)
        with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                self._verify_file(target, byte_sha256)
                return _receipt(byte_sha256, len(content), False)
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=target.parent,
                    prefix=".bronze-",
                    delete=False,
                ) as handle:
                    temporary_path = Path(handle.name)
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary_path, target)
                    newly_stored = True
                except FileExistsError:
                    self._verify_file(target, byte_sha256)
                    newly_stored = False
            except OSError as exc:
                raise AppError(
                    ErrorCode.INTERNAL_ERROR,
                    "Failed to persist immutable Bronze bytes",
                ) from exc
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
        self._verify_file(target, byte_sha256)
        return _receipt(byte_sha256, len(content), newly_stored)

    def read(self, byte_sha256: str) -> bytes:
        """Read a regular, non-symlink object and verify SHA-256 on every replay."""

        target = self._target(byte_sha256)
        if not target.exists():
            raise AppError(ErrorCode.INVALID_REQUEST, "Bronze object does not exist")
        return self._verify_file(target, byte_sha256)

    def contains(self, byte_sha256: str) -> bool:
        """Check for one valid persisted object."""

        try:
            self.read(byte_sha256)
        except AppError as exc:
            if exc.code is ErrorCode.INVALID_REQUEST:
                return False
            raise
        return True

    def _target(self, byte_sha256: str) -> Path:
        _require_sha256(byte_sha256)
        target = self._objects_root / byte_sha256[:2] / byte_sha256
        resolved_parent = target.parent.resolve(strict=False)
        if (
            self._objects_root not in resolved_parent.parents
            and resolved_parent != self._objects_root
        ):
            raise AppError(
                ErrorCode.SECURITY_POLICY_VIOLATION,
                "Bronze content address escaped the configured root",
            )
        return target

    @staticmethod
    def _verify_file(target: Path, byte_sha256: str) -> bytes:
        if target.is_symlink() or not target.is_file():
            _integrity_error("Bronze content address is not a regular immutable file")
        try:
            content = target.read_bytes()
        except OSError as exc:
            raise AppError(ErrorCode.INTERNAL_ERROR, "Failed to read Bronze object") from exc
        _verify_content_hash(content, byte_sha256)
        return content


def _receipt(byte_sha256: str, size_bytes: int, newly_stored: bool) -> BronzeWriteReceipt:
    return BronzeWriteReceipt(
        byte_sha256=byte_sha256,
        size_bytes=size_bytes,
        storage_uri=f"bronze://sha256/{byte_sha256}",
        newly_stored=newly_stored,
    )


def _require_non_empty_bytes(content: bytes) -> None:
    if not content:
        raise AppError(ErrorCode.VALIDATION_FAILED, "Bronze objects cannot be empty")


def _require_sha256(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise AppError(ErrorCode.INVALID_REQUEST, "Bronze object hash must be lowercase SHA-256")


def _verify_content_hash(content: bytes, expected: str) -> None:
    if hashlib.sha256(content).hexdigest() != expected:
        _integrity_error("Bronze object bytes do not match their content address")


def _integrity_error(message: str) -> None:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)

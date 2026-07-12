"""Immutable content-addressed stores for canonical M09 DocumentIR artifacts."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import NoReturn, Protocol

from pydantic import ValidationError

from scidatafusion.contracts.documents import DocumentIR, DocumentIRRef
from scidatafusion.documents.integrity import (
    build_document_ir_ref,
    serialize_document_ir,
    verify_document_ir_integrity,
)
from scidatafusion.errors import AppError, ErrorCode

_MAX_DOCUMENT_IR_BYTES = 1_000_000_000


@dataclass(frozen=True, slots=True)
class DocumentIRWriteReceipt:
    """Immutable Silver reference and deduplication outcome for one DocumentIR."""

    ir_ref: DocumentIRRef
    newly_stored: bool

    @property
    def artifact_sha256(self) -> str:
        """Return the canonical serialized artifact address."""

        return self.ir_ref.artifact_sha256

    @property
    def size_bytes(self) -> int:
        """Return the canonical serialized artifact size."""

        return self.ir_ref.size_bytes

    @property
    def storage_uri(self) -> str:
        """Return the stable Silver URI."""

        return self.ir_ref.uri


class DocumentIRStore(Protocol):
    """Immutable persistence boundary for complete canonical DocumentIR artifacts."""

    def put(self, document: DocumentIR) -> DocumentIRWriteReceipt:
        """Persist one integrity-valid DocumentIR without replacing existing content."""

    def read(self, artifact_sha256: str) -> DocumentIR:
        """Load one strict DocumentIR after verifying bytes and all nested identities."""

    def contains(self, artifact_sha256: str) -> bool:
        """Return whether one valid content-addressed DocumentIR exists."""


class MemoryDocumentIRStore:
    """Thread-safe canonical DocumentIR store for tests and ephemeral workflows."""

    def __init__(
        self,
        *,
        max_object_bytes: int = 256_000_000,
        max_total_bytes: int = 512_000_000,
    ) -> None:
        _validate_limits(max_object_bytes, max_total_bytes)
        self._max_object_bytes = max_object_bytes
        self._max_total_bytes = max_total_bytes
        self._stored_bytes = 0
        self._objects: dict[str, bytes] = {}
        self._lock = RLock()

    def put(self, document: DocumentIR) -> DocumentIRWriteReceipt:
        """Store canonical JSON once and reject invalid, oversized, or conflicting content."""

        verify_document_ir_integrity(document)
        payload = serialize_document_ir(document)
        self._require_object_size(len(payload))
        reference = build_document_ir_ref(document)
        artifact_sha256 = reference.artifact_sha256
        with self._lock:
            existing = self._objects.get(artifact_sha256)
            if existing is not None and existing != payload:
                _integrity_error("SHA-256 collision detected in the M09 DocumentIR store")
            newly_stored = existing is None
            if newly_stored:
                if self._stored_bytes + len(payload) > self._max_total_bytes:
                    raise AppError(
                        ErrorCode.BUDGET_EXCEEDED,
                        "Memory DocumentIR capacity would be exceeded",
                    )
                self._objects[artifact_sha256] = bytes(payload)
                self._stored_bytes += len(payload)
        stored = self.read(artifact_sha256)
        if stored != document:
            _integrity_error("Stored M09 DocumentIR does not replay exactly")
        return DocumentIRWriteReceipt(ir_ref=reference, newly_stored=newly_stored)

    def read(self, artifact_sha256: str) -> DocumentIR:
        """Return a strictly reconstructed DocumentIR after verifying stored bytes."""

        _require_sha256(artifact_sha256)
        with self._lock:
            payload = self._objects.get(artifact_sha256)
        if payload is None:
            raise AppError(ErrorCode.INVALID_REQUEST, "DocumentIR artifact does not exist")
        self._require_stored_size(len(payload))
        return _decode_and_verify(bytes(payload), artifact_sha256)

    def contains(self, artifact_sha256: str) -> bool:
        """Check existence and integrity without exposing serialized bytes."""

        try:
            self.read(artifact_sha256)
        except AppError as exc:
            if exc.code is ErrorCode.INVALID_REQUEST:
                return False
            raise
        return True

    def _require_object_size(self, size_bytes: int) -> None:
        if size_bytes > self._max_object_bytes:
            raise AppError(
                ErrorCode.VALIDATION_FAILED,
                "DocumentIR artifact exceeds the configured object size limit",
            )

    def _require_stored_size(self, size_bytes: int) -> None:
        if not 1 <= size_bytes <= self._max_object_bytes:
            _integrity_error("Stored DocumentIR artifact violates the object size limit")


class FileSystemDocumentIRStore:
    """Durable canonical store using sharded atomic no-overwrite publication."""

    def __init__(self, root: Path, *, max_object_bytes: int = 256_000_000) -> None:
        _validate_limits(max_object_bytes, _MAX_DOCUMENT_IR_BYTES)
        try:
            root.mkdir(parents=True, exist_ok=True)
            if root.is_symlink():
                raise AppError(
                    ErrorCode.SECURITY_POLICY_VIOLATION,
                    "DocumentIR root cannot be a symbolic link",
                )
            self._root = root.resolve(strict=True)
        except OSError as exc:
            raise AppError(
                ErrorCode.CONFIGURATION_ERROR,
                "DocumentIR root could not be initialized",
            ) from exc
        if not self._root.is_dir():
            raise AppError(
                ErrorCode.CONFIGURATION_ERROR,
                "DocumentIR root must be a directory",
            )
        self._objects_root = self._root / "sha256"
        try:
            self._objects_root.mkdir(exist_ok=True)
        except OSError as exc:
            raise AppError(
                ErrorCode.CONFIGURATION_ERROR,
                "DocumentIR object root could not be initialized",
            ) from exc
        if self._objects_root.is_symlink() or not self._objects_root.is_dir():
            raise AppError(
                ErrorCode.SECURITY_POLICY_VIOLATION,
                "DocumentIR object root must be a regular directory",
            )
        self._max_object_bytes = max_object_bytes
        self._lock = RLock()

    def put(self, document: DocumentIR) -> DocumentIRWriteReceipt:
        """Publish canonical JSON atomically without replacing an existing address."""

        verify_document_ir_integrity(document)
        payload = serialize_document_ir(document)
        if len(payload) > self._max_object_bytes:
            raise AppError(
                ErrorCode.VALIDATION_FAILED,
                "DocumentIR artifact exceeds the configured object size limit",
            )
        reference = build_document_ir_ref(document)
        target = self._target(reference.artifact_sha256)
        with self._lock:
            self._ensure_shard(target.parent)
            if target.exists() or target.is_symlink():
                existing = self._verify_file(target, reference.artifact_sha256)
                if existing != document:
                    _integrity_error("DocumentIR content address contains different content")
                return DocumentIRWriteReceipt(ir_ref=reference, newly_stored=False)
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=target.parent,
                    prefix=".m09-document-ir-",
                    delete=False,
                ) as handle:
                    temporary_path = Path(handle.name)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary_path, target)
                    newly_stored = True
                except FileExistsError:
                    existing = self._verify_file(target, reference.artifact_sha256)
                    if existing != document:
                        _integrity_error(
                            "Concurrent DocumentIR publication produced a content conflict"
                        )
                    newly_stored = False
            except OSError as exc:
                raise AppError(
                    ErrorCode.INTERNAL_ERROR,
                    "Failed to persist immutable DocumentIR artifact",
                ) from exc
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
        stored = self._verify_file(target, reference.artifact_sha256)
        if stored != document:
            _integrity_error("Published DocumentIR could not be verified")
        return DocumentIRWriteReceipt(ir_ref=reference, newly_stored=newly_stored)

    def read(self, artifact_sha256: str) -> DocumentIR:
        """Read a regular non-symlink artifact and verify it on every replay."""

        target = self._target(artifact_sha256)
        if not target.exists():
            if target.is_symlink():
                _integrity_error("DocumentIR content address cannot be a symbolic link")
            raise AppError(ErrorCode.INVALID_REQUEST, "DocumentIR artifact does not exist")
        return self._verify_file(target, artifact_sha256)

    def contains(self, artifact_sha256: str) -> bool:
        """Check for one valid persisted DocumentIR artifact."""

        try:
            self.read(artifact_sha256)
        except AppError as exc:
            if exc.code is ErrorCode.INVALID_REQUEST:
                return False
            raise
        return True

    def _target(self, artifact_sha256: str) -> Path:
        _require_sha256(artifact_sha256)
        target = self._objects_root / artifact_sha256[:2] / f"{artifact_sha256}.json"
        resolved_parent = target.parent.resolve(strict=False)
        if (
            self._objects_root not in resolved_parent.parents
            and resolved_parent != self._objects_root
        ):
            raise AppError(
                ErrorCode.SECURITY_POLICY_VIOLATION,
                "DocumentIR content address escaped the configured root",
            )
        return target

    def _ensure_shard(self, shard: Path) -> None:
        try:
            shard.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                "Failed to initialize DocumentIR storage shard",
            ) from exc
        if shard.is_symlink() or not shard.is_dir():
            raise AppError(
                ErrorCode.SECURITY_POLICY_VIOLATION,
                "DocumentIR storage shard must be a regular directory",
            )
        resolved = shard.resolve(strict=True)
        if self._objects_root not in resolved.parents:
            raise AppError(
                ErrorCode.SECURITY_POLICY_VIOLATION,
                "DocumentIR storage shard escaped the configured root",
            )

    def _verify_file(self, target: Path, artifact_sha256: str) -> DocumentIR:
        if target.is_symlink() or not target.is_file():
            _integrity_error("DocumentIR content address is not a regular immutable file")
        try:
            size_bytes = target.stat().st_size
            if not 1 <= size_bytes <= self._max_object_bytes:
                _integrity_error("Stored DocumentIR artifact violates the object size limit")
            payload = target.read_bytes()
        except OSError as exc:
            raise AppError(ErrorCode.INTERNAL_ERROR, "Failed to read DocumentIR artifact") from exc
        return _decode_and_verify(payload, artifact_sha256)


def _decode_and_verify(payload: bytes, artifact_sha256: str) -> DocumentIR:
    if hashlib.sha256(payload).hexdigest() != artifact_sha256:
        _integrity_error("DocumentIR bytes do not match their content address")
    try:
        document = DocumentIR.model_validate_json(payload)
    except ValidationError as exc:
        raise AppError(
            ErrorCode.ARTIFACT_INTEGRITY_ERROR,
            "DocumentIR artifact failed strict contract validation",
        ) from exc
    verify_document_ir_integrity(document)
    if serialize_document_ir(document) != payload:
        _integrity_error("DocumentIR artifact is not canonical JSON")
    if build_document_ir_ref(document).artifact_sha256 != artifact_sha256:
        _integrity_error("DocumentIR reference does not match serialized content")
    return document


def _validate_limits(max_object_bytes: int, max_total_bytes: int) -> None:
    if not 1 <= max_object_bytes <= _MAX_DOCUMENT_IR_BYTES:
        raise AppError(
            ErrorCode.CONFIGURATION_ERROR,
            "DocumentIR object limit must be between 1 byte and 1 GB",
        )
    if not max_object_bytes <= max_total_bytes <= _MAX_DOCUMENT_IR_BYTES:
        raise AppError(
            ErrorCode.CONFIGURATION_ERROR,
            "DocumentIR total limit must include one object and be at most 1 GB",
        )


def _require_sha256(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise AppError(
            ErrorCode.INVALID_REQUEST,
            "DocumentIR artifact hash must be lowercase SHA-256",
        )


def _integrity_error(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)

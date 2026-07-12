"""Strict process-local and durable checkpoints for complete M09 results."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from threading import RLock
from typing import NoReturn, Protocol

from pydantic import ValidationError

from scidatafusion.contracts.documents import DocumentParsingResult
from scidatafusion.documents.integrity import verify_document_parsing_result_hashes
from scidatafusion.errors import AppError, ErrorCode

_DEFAULT_MAX_CHECKPOINT_BYTES = 64 * 1024 * 1024
_MAX_CHECKPOINT_BYTES = 1_000_000_000


class DocumentCheckpointStore(Protocol):
    """Persistence boundary for one immutable result per M09 idempotency key."""

    def load(self, idempotency_key: str) -> DocumentParsingResult | None:
        """Load and verify a prior complete result, or return no checkpoint."""

    def save(self, result: DocumentParsingResult) -> DocumentParsingResult:
        """Publish one verified result without replacing different prior content."""


class MemoryDocumentCheckpointStore:
    """Thread-safe canonical checkpoint store for tests and ephemeral execution."""

    def __init__(self, *, max_checkpoint_bytes: int = _DEFAULT_MAX_CHECKPOINT_BYTES) -> None:
        _validate_limit(max_checkpoint_bytes)
        self._max_checkpoint_bytes = max_checkpoint_bytes
        self._results: dict[str, bytes] = {}
        self._lock = RLock()

    def load(self, idempotency_key: str) -> DocumentParsingResult | None:
        """Reconstruct and verify the exact immutable result for one checkpoint key."""

        _require_sha256(idempotency_key)
        with self._lock:
            payload = self._results.get(idempotency_key)
        if payload is None:
            return None
        return _decode_and_verify(bytes(payload), idempotency_key, self._max_checkpoint_bytes)

    def save(self, result: DocumentParsingResult) -> DocumentParsingResult:
        """Store canonical bytes once and reject idempotency-key content reuse."""

        verify_document_parsing_result_hashes(result)
        payload = _serialize_result(result)
        _require_save_size(len(payload), self._max_checkpoint_bytes)
        with self._lock:
            existing = self._results.setdefault(result.idempotency_key, payload)
        if existing != payload:
            _integrity_error("M09 idempotency key already has a different checkpoint")
        stored = self.load(result.idempotency_key)
        if stored is None or stored != result:
            _integrity_error("Stored M09 checkpoint could not be verified")
        return stored


class FileSystemDocumentCheckpointStore:
    """Durable atomic M09 result store keyed by producer-bound idempotency hash."""

    def __init__(
        self,
        root: Path,
        *,
        max_checkpoint_bytes: int = _DEFAULT_MAX_CHECKPOINT_BYTES,
    ) -> None:
        _validate_limit(max_checkpoint_bytes)
        try:
            root.mkdir(parents=True, exist_ok=True)
            if root.is_symlink():
                raise AppError(
                    ErrorCode.SECURITY_POLICY_VIOLATION,
                    "M09 checkpoint root cannot be a symbolic link",
                )
            self._root = root.resolve(strict=True)
        except OSError as exc:
            raise AppError(
                ErrorCode.CONFIGURATION_ERROR,
                "M09 checkpoint root could not be initialized",
            ) from exc
        if not self._root.is_dir():
            raise AppError(
                ErrorCode.CONFIGURATION_ERROR,
                "M09 checkpoint root must be a directory",
            )
        self._max_checkpoint_bytes = max_checkpoint_bytes
        self._lock = RLock()

    def load(self, idempotency_key: str) -> DocumentParsingResult | None:
        """Load one bounded strict checkpoint without following a result symlink."""

        target = self._target(idempotency_key)
        if not target.exists():
            if target.is_symlink():
                _integrity_error("M09 checkpoint cannot be a symbolic link")
            return None
        if target.is_symlink() or not target.is_file():
            _integrity_error("M09 checkpoint is not a regular immutable file")
        try:
            size_bytes = target.stat().st_size
            if not 1 <= size_bytes <= self._max_checkpoint_bytes:
                _integrity_error("M09 checkpoint violates the metadata size limit")
            payload = target.read_bytes()
        except OSError as exc:
            raise AppError(ErrorCode.INTERNAL_ERROR, "Failed to read M09 checkpoint") from exc
        return _decode_and_verify(payload, idempotency_key, self._max_checkpoint_bytes)

    def save(self, result: DocumentParsingResult) -> DocumentParsingResult:
        """Atomically publish canonical result bytes without silent replacement."""

        verify_document_parsing_result_hashes(result)
        payload = _serialize_result(result)
        _require_save_size(len(payload), self._max_checkpoint_bytes)
        target = self._target(result.idempotency_key)
        with self._lock:
            self._ensure_shard(target.parent)
            existing = self.load(result.idempotency_key)
            if existing is not None:
                if existing != result:
                    _integrity_error("M09 idempotency key already has a different checkpoint")
                return existing
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=target.parent,
                    prefix=".m09-checkpoint-",
                    delete=False,
                ) as handle:
                    temporary_path = Path(handle.name)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary_path, target)
                except FileExistsError:
                    existing = self.load(result.idempotency_key)
                    if existing is None or existing != result:
                        _integrity_error(
                            "Concurrent M09 checkpoint publication produced a conflict"
                        )
                    return existing
            except OSError as exc:
                raise AppError(
                    ErrorCode.INTERNAL_ERROR,
                    "Failed to persist M09 checkpoint",
                ) from exc
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
        stored = self.load(result.idempotency_key)
        if stored is None or stored != result:
            _integrity_error("Published M09 checkpoint could not be verified")
        return stored

    def _target(self, idempotency_key: str) -> Path:
        _require_sha256(idempotency_key)
        target = self._root / idempotency_key[:2] / f"{idempotency_key}.json"
        resolved_parent = target.parent.resolve(strict=False)
        if self._root not in resolved_parent.parents and resolved_parent != self._root:
            raise AppError(
                ErrorCode.SECURITY_POLICY_VIOLATION,
                "M09 checkpoint address escaped the configured root",
            )
        return target

    def _ensure_shard(self, shard: Path) -> None:
        try:
            shard.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                "Failed to initialize M09 checkpoint shard",
            ) from exc
        if shard.is_symlink() or not shard.is_dir():
            raise AppError(
                ErrorCode.SECURITY_POLICY_VIOLATION,
                "M09 checkpoint shard must be a regular directory",
            )
        resolved = shard.resolve(strict=True)
        if self._root not in resolved.parents:
            raise AppError(
                ErrorCode.SECURITY_POLICY_VIOLATION,
                "M09 checkpoint shard escaped the configured root",
            )


def _serialize_result(result: DocumentParsingResult) -> bytes:
    try:
        encoded = json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "M09 checkpoint could not be serialized as canonical JSON",
        ) from exc
    return encoded.encode("utf-8")


def _decode_and_verify(
    payload: bytes,
    idempotency_key: str,
    max_checkpoint_bytes: int,
) -> DocumentParsingResult:
    if not 1 <= len(payload) <= max_checkpoint_bytes:
        _integrity_error("M09 checkpoint violates the metadata size limit")
    try:
        result = DocumentParsingResult.model_validate_json(payload)
    except ValidationError as exc:
        raise AppError(
            ErrorCode.ARTIFACT_INTEGRITY_ERROR,
            "M09 checkpoint failed strict contract validation",
        ) from exc
    if result.idempotency_key != idempotency_key:
        _integrity_error("M09 checkpoint does not match its content-addressed key")
    verify_document_parsing_result_hashes(result)
    if _serialize_result(result) != payload:
        _integrity_error("M09 checkpoint is not canonical JSON")
    return result


def _require_save_size(size_bytes: int, maximum: int) -> None:
    if not 1 <= size_bytes <= maximum:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "M09 checkpoint exceeds the configured metadata size limit",
        )


def _validate_limit(value: int) -> None:
    if not 1 <= value <= _MAX_CHECKPOINT_BYTES:
        raise AppError(
            ErrorCode.CONFIGURATION_ERROR,
            "M09 checkpoint limit must be between 1 byte and 1 GB",
        )


def _require_sha256(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise AppError(ErrorCode.INVALID_REQUEST, "M09 checkpoint key must be lowercase SHA-256")


def _integrity_error(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)

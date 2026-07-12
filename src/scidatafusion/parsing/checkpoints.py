"""Strict process-local and durable checkpoints for complete M08 results."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from threading import RLock
from typing import NoReturn, Protocol

from pydantic import ValidationError

from scidatafusion.contracts.parsing import ParsePlanningResult
from scidatafusion.errors import AppError, ErrorCode

_MAX_CHECKPOINT_BYTES = 32 * 1024 * 1024


class ParseCheckpointStore(Protocol):
    """Persistence boundary for one immutable result per M08 idempotency key."""

    def load(self, idempotency_key: str) -> ParsePlanningResult | None:
        """Load and strictly validate a prior result, or return no checkpoint."""

    def save(self, result: ParsePlanningResult) -> ParsePlanningResult:
        """Publish a result without replacing a different prior checkpoint."""


class MemoryParseCheckpointStore:
    """Thread-safe checkpoint store for tests and explicitly ephemeral planning."""

    def __init__(self) -> None:
        self._results: dict[str, ParsePlanningResult] = {}
        self._lock = RLock()

    def load(self, idempotency_key: str) -> ParsePlanningResult | None:
        """Return the exact immutable result for one valid checkpoint key."""

        _require_sha256(idempotency_key)
        with self._lock:
            return self._results.get(idempotency_key)

    def save(self, result: ParsePlanningResult) -> ParsePlanningResult:
        """Store once and reject key reuse for different result content."""

        with self._lock:
            existing = self._results.setdefault(result.idempotency_key, result)
        if existing != result:
            _integrity_error("M08 idempotency key already has a different checkpoint")
        return existing


class FileSystemParseCheckpointStore:
    """Durable atomic M08 result store keyed by producer-bound idempotency hash."""

    def __init__(self, root: Path) -> None:
        try:
            root.mkdir(parents=True, exist_ok=True)
            if root.is_symlink():
                raise AppError(
                    ErrorCode.SECURITY_POLICY_VIOLATION,
                    "M08 checkpoint root cannot be a symbolic link",
                )
            self._root = root.resolve(strict=True)
        except OSError as exc:
            raise AppError(
                ErrorCode.CONFIGURATION_ERROR,
                "M08 checkpoint root could not be initialized",
            ) from exc
        if not self._root.is_dir():
            raise AppError(
                ErrorCode.SECURITY_POLICY_VIOLATION,
                "M08 checkpoint root must be a regular directory",
            )
        self._lock = RLock()

    def load(self, idempotency_key: str) -> ParsePlanningResult | None:
        """Load one bounded strict checkpoint without following a result symlink."""

        target = self._target(idempotency_key)
        if not target.exists():
            return None
        if target.is_symlink() or not target.is_file():
            _integrity_error("M08 checkpoint is not a regular immutable file")
        try:
            if target.stat().st_size > _MAX_CHECKPOINT_BYTES:
                _integrity_error("M08 checkpoint exceeds the metadata size limit")
            payload = target.read_bytes()
            result = ParsePlanningResult.model_validate_json(payload)
        except OSError as exc:
            raise AppError(ErrorCode.INTERNAL_ERROR, "Failed to read M08 checkpoint") from exc
        except ValidationError as exc:
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "M08 checkpoint failed strict contract validation",
            ) from exc
        if result.idempotency_key != idempotency_key:
            _integrity_error("M08 checkpoint does not match its content-addressed key")
        return result

    def save(self, result: ParsePlanningResult) -> ParsePlanningResult:
        """Atomically publish one strict checkpoint without silent replacement."""

        payload = result.model_dump_json().encode("utf-8")
        if len(payload) > _MAX_CHECKPOINT_BYTES:
            raise AppError(
                ErrorCode.VALIDATION_FAILED,
                "M08 checkpoint exceeds the metadata size limit",
            )
        target = self._target(result.idempotency_key)
        with self._lock:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise AppError(
                    ErrorCode.INTERNAL_ERROR,
                    "Failed to initialize M08 checkpoint shard",
                ) from exc
            existing = self.load(result.idempotency_key)
            if existing is not None:
                if existing != result:
                    _integrity_error("M08 idempotency key already has a different checkpoint")
                return existing
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=target.parent,
                    prefix=".m08-checkpoint-",
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
                    if existing != result:
                        _integrity_error(
                            "Concurrent M08 checkpoint publication produced a conflict"
                        )
                    return result if existing is None else existing
            except OSError as exc:
                raise AppError(
                    ErrorCode.INTERNAL_ERROR,
                    "Failed to persist M08 checkpoint",
                ) from exc
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
        stored = self.load(result.idempotency_key)
        if stored is None or stored != result:
            _integrity_error("Published M08 checkpoint could not be verified")
        return stored

    def _target(self, idempotency_key: str) -> Path:
        _require_sha256(idempotency_key)
        target = self._root / idempotency_key[:2] / f"{idempotency_key}.json"
        resolved_parent = target.parent.resolve(strict=False)
        if self._root not in resolved_parent.parents and resolved_parent != self._root:
            raise AppError(
                ErrorCode.SECURITY_POLICY_VIOLATION,
                "M08 checkpoint address escaped the configured root",
            )
        return target


def _require_sha256(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise AppError(ErrorCode.INVALID_REQUEST, "M08 checkpoint key must be lowercase SHA-256")


def _integrity_error(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)

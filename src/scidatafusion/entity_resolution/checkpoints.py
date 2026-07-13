"""Canonical process-local checkpoints for complete M16 results."""

from __future__ import annotations

import json
from threading import RLock
from typing import NoReturn, Protocol

from pydantic import ValidationError

from scidatafusion.contracts.entity_resolution import EntityResolutionResult
from scidatafusion.entity_resolution.integrity import verify_entity_result_hashes
from scidatafusion.errors import AppError, ErrorCode


class EntityResolutionCheckpointStore(Protocol):
    def load(self, idempotency_key: str) -> EntityResolutionResult | None: ...
    def save(self, result: EntityResolutionResult) -> EntityResolutionResult: ...


class MemoryEntityResolutionCheckpointStore:
    def __init__(self, *, max_checkpoint_bytes: int = 512_000_000) -> None:
        if not 1 <= max_checkpoint_bytes <= 1_000_000_000:
            raise AppError(ErrorCode.CONFIGURATION_ERROR, "invalid M16 checkpoint size limit")
        self._maximum = max_checkpoint_bytes
        self._values: dict[str, bytes] = {}
        self._lock = RLock()

    def load(self, idempotency_key: str) -> EntityResolutionResult | None:
        _require_hash(idempotency_key)
        with self._lock:
            payload = self._values.get(idempotency_key)
        if payload is None:
            return None
        try:
            result = EntityResolutionResult.model_validate_json(payload)
        except ValidationError as exc:
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "M16 checkpoint failed strict validation",
            ) from exc
        if result.idempotency_key != idempotency_key or _serialize(result) != payload:
            _integrity_error("M16 checkpoint key or canonical representation is invalid")
        verify_entity_result_hashes(result)
        return result

    def save(self, result: EntityResolutionResult) -> EntityResolutionResult:
        verify_entity_result_hashes(result)
        payload = _serialize(result)
        if len(payload) > self._maximum:
            raise AppError(ErrorCode.VALIDATION_FAILED, "M16 checkpoint exceeds its size limit")
        with self._lock:
            existing = self._values.get(result.idempotency_key)
            if existing is not None and existing != payload:
                _integrity_error("M16 idempotency key already has a different checkpoint")
            self._values.setdefault(result.idempotency_key, payload)
        stored = self.load(result.idempotency_key)
        if stored is None or stored != result:
            _integrity_error("M16 checkpoint did not replay exactly")
        return stored


def _serialize(result: EntityResolutionResult) -> bytes:
    try:
        value = json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise AppError(ErrorCode.VALIDATION_FAILED, "M16 checkpoint is not JSON") from exc
    return value.encode()


def _require_hash(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise AppError(ErrorCode.INVALID_REQUEST, "M16 checkpoint key must be lowercase SHA-256")


def _integrity_error(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)

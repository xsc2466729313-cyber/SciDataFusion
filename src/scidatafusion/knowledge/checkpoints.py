"""Canonical process-local checkpoints for M19 knowledge results."""

from __future__ import annotations

import json
from threading import RLock
from typing import NoReturn, Protocol

from pydantic import ValidationError

from scidatafusion.contracts.knowledge import KnowledgeResult
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.knowledge.integrity import verify_knowledge_result_hashes


class KnowledgeCheckpointStore(Protocol):
    def load(self, idempotency_key: str) -> KnowledgeResult | None: ...
    def save(self, result: KnowledgeResult) -> KnowledgeResult: ...


class MemoryKnowledgeCheckpointStore:
    def __init__(self, *, max_checkpoint_bytes: int = 768_000_000) -> None:
        if not 1 <= max_checkpoint_bytes <= 1_000_000_000:
            raise AppError(ErrorCode.CONFIGURATION_ERROR, "invalid M19 checkpoint size limit")
        self._maximum = max_checkpoint_bytes
        self._values: dict[str, bytes] = {}
        self._lock = RLock()

    def load(self, idempotency_key: str) -> KnowledgeResult | None:
        _require_hash(idempotency_key)
        with self._lock:
            payload = self._values.get(idempotency_key)
        if payload is None:
            return None
        try:
            result = KnowledgeResult.model_validate_json(payload)
        except ValidationError as exc:
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR, "M19 checkpoint failed strict validation"
            ) from exc
        if result.idempotency_key != idempotency_key or _serialize(result) != payload:
            _integrity_error("M19 checkpoint key or canonical representation is invalid")
        verify_knowledge_result_hashes(result)
        return result

    def save(self, result: KnowledgeResult) -> KnowledgeResult:
        verify_knowledge_result_hashes(result)
        payload = _serialize(result)
        if len(payload) > self._maximum:
            raise AppError(ErrorCode.VALIDATION_FAILED, "M19 checkpoint exceeds its size limit")
        with self._lock:
            existing = self._values.get(result.idempotency_key)
            if existing is not None and existing != payload:
                _integrity_error("M19 idempotency key already has a different checkpoint")
            self._values.setdefault(result.idempotency_key, payload)
        stored = self.load(result.idempotency_key)
        if stored is None or stored != result:
            _integrity_error("M19 checkpoint did not replay exactly")
        return stored


def _serialize(result: KnowledgeResult) -> bytes:
    try:
        value = json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise AppError(ErrorCode.VALIDATION_FAILED, "M19 checkpoint is not JSON") from exc
    return value.encode()


def _require_hash(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise AppError(ErrorCode.INVALID_REQUEST, "M19 checkpoint key must be lowercase SHA-256")


def _integrity_error(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)

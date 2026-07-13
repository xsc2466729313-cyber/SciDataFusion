"""Canonical process-local checkpoints for complete M12 results."""

from __future__ import annotations

import json
from threading import RLock
from typing import Protocol

from pydantic import ValidationError

from scidatafusion.contracts.datasets import ScientificParsingResult
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.scientific_formats.integrity import verify_scientific_result_hashes


class ScientificCheckpointStore(Protocol):
    def load(self, idempotency_key: str) -> ScientificParsingResult | None:
        """Load a validated result checkpoint."""

    def save(self, result: ScientificParsingResult) -> ScientificParsingResult:
        """Publish one result without overwriting a conflicting checkpoint."""


class MemoryScientificCheckpointStore:
    def __init__(self, *, max_checkpoint_bytes: int = 32_000_000) -> None:
        if not 1 <= max_checkpoint_bytes <= 256_000_000:
            raise AppError(ErrorCode.CONFIGURATION_ERROR, "invalid M12 checkpoint limit")
        self._maximum = max_checkpoint_bytes
        self._values: dict[str, bytes] = {}
        self._lock = RLock()

    def load(self, idempotency_key: str) -> ScientificParsingResult | None:
        _require_hash(idempotency_key)
        with self._lock:
            payload = self._values.get(idempotency_key)
        if payload is None:
            return None
        if not 1 <= len(payload) <= self._maximum:
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, "M12 checkpoint size is invalid")
        try:
            result = ScientificParsingResult.model_validate_json(payload)
        except ValidationError as exc:
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR, "M12 checkpoint contract is invalid"
            ) from exc
        if result.idempotency_key != idempotency_key or _serialize(result) != payload:
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, "M12 checkpoint is not canonical")
        verify_scientific_result_hashes(result)
        return result

    def save(self, result: ScientificParsingResult) -> ScientificParsingResult:
        verify_scientific_result_hashes(result)
        payload = _serialize(result)
        if len(payload) > self._maximum:
            raise AppError(ErrorCode.BUDGET_EXCEEDED, "M12 checkpoint exceeds storage limit")
        with self._lock:
            existing = self._values.get(result.idempotency_key)
            if existing is not None and existing != payload:
                raise AppError(
                    ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "M12 idempotency key already has a conflicting checkpoint",
                )
            self._values.setdefault(result.idempotency_key, payload)
        stored = self.load(result.idempotency_key)
        if stored is None or stored != result:
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, "M12 checkpoint replay failed")
        return stored


def _serialize(result: ScientificParsingResult) -> bytes:
    return json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _require_hash(value: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise AppError(ErrorCode.INVALID_REQUEST, "M12 checkpoint key must be lowercase SHA-256")

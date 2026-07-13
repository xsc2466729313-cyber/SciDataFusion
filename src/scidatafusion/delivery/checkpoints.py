"""Canonical process-local checkpoints for M20 delivery results."""

from __future__ import annotations

import json
from threading import RLock
from typing import Protocol

from pydantic import ValidationError

from scidatafusion.contracts.delivery import DeliveryResult
from scidatafusion.errors import AppError, ErrorCode


class DeliveryCheckpointStore(Protocol):
    def load(self, idempotency_key: str) -> DeliveryResult | None: ...
    def save(self, result: DeliveryResult) -> DeliveryResult: ...


class MemoryDeliveryCheckpointStore:
    def __init__(self, *, maximum_bytes: int = 128_000_000) -> None:
        if not 1_024 <= maximum_bytes <= 1_000_000_000:
            raise AppError(ErrorCode.CONFIGURATION_ERROR, "invalid M20 checkpoint size limit")
        self._maximum = maximum_bytes
        self._values: dict[str, bytes] = {}
        self._lock = RLock()

    def load(self, idempotency_key: str) -> DeliveryResult | None:
        _require_hash(idempotency_key)
        with self._lock:
            payload = self._values.get(idempotency_key)
        if payload is None:
            return None
        try:
            result = DeliveryResult.model_validate_json(payload)
        except ValidationError as exc:
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "M20 checkpoint failed strict validation",
            ) from exc
        if result.idempotency_key != idempotency_key or _serialize(result) != payload:
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "M20 checkpoint key or representation is invalid",
            )
        return result

    def save(self, result: DeliveryResult) -> DeliveryResult:
        payload = _serialize(result)
        if len(payload) > self._maximum:
            raise AppError(ErrorCode.VALIDATION_FAILED, "M20 checkpoint exceeds size limit")
        with self._lock:
            existing = self._values.get(result.idempotency_key)
            if existing is not None and existing != payload:
                raise AppError(
                    ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "M20 idempotency key already has a different checkpoint",
                )
            self._values.setdefault(result.idempotency_key, payload)
        stored = self.load(result.idempotency_key)
        if stored is None or stored != result:
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, "M20 checkpoint replay failed")
        return stored


def _serialize(result: DeliveryResult) -> bytes:
    try:
        encoded = json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise AppError(ErrorCode.VALIDATION_FAILED, "M20 checkpoint is not JSON") from exc
    return encoded.encode("utf-8")


def _require_hash(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise AppError(ErrorCode.INVALID_REQUEST, "M20 checkpoint key must be SHA-256")

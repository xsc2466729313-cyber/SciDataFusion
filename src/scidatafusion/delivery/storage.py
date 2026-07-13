"""Content-addressed process-local storage for immutable M20 bytes."""

from __future__ import annotations

import hashlib
import hmac
from threading import RLock
from typing import Protocol

from scidatafusion.errors import AppError, ErrorCode


class DeliveryByteStore(Protocol):
    def put(self, payload: bytes) -> str: ...
    def get(self, sha256: str) -> bytes | None: ...


class MemoryDeliveryStore:
    def __init__(self, *, maximum_bytes: int = 1_000_000_000) -> None:
        if not 1_024 <= maximum_bytes <= 1_000_000_000:
            raise AppError(ErrorCode.CONFIGURATION_ERROR, "invalid M20 store size limit")
        self._maximum = maximum_bytes
        self._values: dict[str, bytes] = {}
        self._lock = RLock()

    def put(self, payload: bytes) -> str:
        if len(payload) > self._maximum:
            raise AppError(ErrorCode.VALIDATION_FAILED, "M20 artifact exceeds store limit")
        digest = hashlib.sha256(payload).hexdigest()
        with self._lock:
            existing = self._values.get(digest)
            if existing is not None and not hmac.compare_digest(existing, payload):
                raise AppError(
                    ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "M20 content address already contains different bytes",
                )
            self._values.setdefault(digest, payload)
        return digest

    def get(self, sha256: str) -> bytes | None:
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise AppError(ErrorCode.INVALID_REQUEST, "M20 artifact key must be SHA-256")
        with self._lock:
            value = self._values.get(sha256)
        if value is not None and not hmac.compare_digest(hashlib.sha256(value).hexdigest(), sha256):
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, "M20 stored bytes are corrupt")
        return value

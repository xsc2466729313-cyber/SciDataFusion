"""Content-addressed storage for canonical M12 DatasetIR artifacts."""

from __future__ import annotations

import hashlib
from threading import RLock
from typing import Protocol

from pydantic import ValidationError

from scidatafusion.contracts.datasets import DatasetIR, DatasetIRRef
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.scientific_formats.integrity import (
    build_dataset_ref,
    serialize_dataset_ir,
    verify_dataset_ir,
)


class DatasetIRStore(Protocol):
    def put(self, dataset: DatasetIR) -> DatasetIRRef:
        """Publish one immutable DatasetIR or replay the identical content address."""

    def read(self, artifact_sha256: str) -> DatasetIR:
        """Read one DatasetIR after canonical and nested-hash validation."""


class MemoryDatasetIRStore:
    def __init__(self, *, max_object_bytes: int = 256_000_000) -> None:
        if not 1 <= max_object_bytes <= 1_000_000_000:
            raise AppError(ErrorCode.CONFIGURATION_ERROR, "invalid M12 DatasetIR store limit")
        self._maximum = max_object_bytes
        self._values: dict[str, bytes] = {}
        self._lock = RLock()

    def put(self, dataset: DatasetIR) -> DatasetIRRef:
        verify_dataset_ir(dataset)
        payload = serialize_dataset_ir(dataset)
        if len(payload) > self._maximum:
            raise AppError(ErrorCode.BUDGET_EXCEEDED, "M12 DatasetIR exceeds storage limit")
        reference = build_dataset_ref(dataset)
        with self._lock:
            existing = self._values.get(reference.artifact_sha256)
            if existing is not None and existing != payload:
                raise AppError(
                    ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "M12 DatasetIR content address contains conflicting bytes",
                )
            self._values.setdefault(reference.artifact_sha256, payload)
        if self.read(reference.artifact_sha256) != dataset:
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, "M12 DatasetIR replay failed")
        return reference

    def read(self, artifact_sha256: str) -> DatasetIR:
        _require_hash(artifact_sha256)
        with self._lock:
            payload = self._values.get(artifact_sha256)
        if payload is None:
            raise AppError(ErrorCode.INVALID_REQUEST, "M12 DatasetIR does not exist")
        if not 1 <= len(payload) <= self._maximum:
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, "M12 DatasetIR size is invalid")
        if hashlib.sha256(payload).hexdigest() != artifact_sha256:
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, "M12 DatasetIR hash is invalid")
        try:
            dataset = DatasetIR.model_validate_json(payload)
        except ValidationError as exc:
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR, "M12 DatasetIR contract is invalid"
            ) from exc
        verify_dataset_ir(dataset)
        if serialize_dataset_ir(dataset) != payload:
            raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, "M12 DatasetIR is not canonical")
        return dataset


def _require_hash(value: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise AppError(ErrorCode.INVALID_REQUEST, "M12 DatasetIR key must be lowercase SHA-256")

"""Content-addressed in-memory storage for canonical M10 TableIR artifacts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from threading import RLock
from typing import NoReturn, Protocol

from pydantic import ValidationError

from scidatafusion.contracts.tables import TableIR, TableIRRef
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.tables.integrity import (
    build_table_ir_ref,
    serialize_table_ir,
    verify_table_ir_integrity,
)


@dataclass(frozen=True, slots=True)
class TableIRWriteReceipt:
    ir_ref: TableIRRef
    newly_stored: bool


class TableIRStore(Protocol):
    def put(self, table: TableIR) -> TableIRWriteReceipt:
        """Persist one canonical integrity-valid table without replacement."""

    def read(self, artifact_sha256: str) -> TableIR:
        """Load one table after verifying its canonical bytes and nested hashes."""

    def contains(self, artifact_sha256: str) -> bool:
        """Return whether an integrity-valid table exists at this content address."""


class MemoryTableIRStore:
    """Thread-safe bounded store used by the first offline M10 slice."""

    def __init__(
        self,
        *,
        max_object_bytes: int = 256_000_000,
        max_total_bytes: int = 512_000_000,
    ) -> None:
        if not 1 <= max_object_bytes <= max_total_bytes <= 1_000_000_000:
            raise AppError(ErrorCode.CONFIGURATION_ERROR, "invalid M10 TableIR storage limits")
        self._max_object_bytes = max_object_bytes
        self._max_total_bytes = max_total_bytes
        self._stored_bytes = 0
        self._objects: dict[str, bytes] = {}
        self._lock = RLock()

    def put(self, table: TableIR) -> TableIRWriteReceipt:
        verify_table_ir_integrity(table)
        payload = serialize_table_ir(table)
        if len(payload) > self._max_object_bytes:
            raise AppError(ErrorCode.VALIDATION_FAILED, "TableIR exceeds the object size limit")
        reference = build_table_ir_ref(table)
        with self._lock:
            existing = self._objects.get(reference.artifact_sha256)
            if existing is not None and existing != payload:
                _integrity_error("M10 TableIR content address contains conflicting bytes")
            newly_stored = existing is None
            if newly_stored:
                if self._stored_bytes + len(payload) > self._max_total_bytes:
                    raise AppError(ErrorCode.BUDGET_EXCEEDED, "M10 TableIR store is full")
                self._objects[reference.artifact_sha256] = bytes(payload)
                self._stored_bytes += len(payload)
        if self.read(reference.artifact_sha256) != table:
            _integrity_error("stored M10 TableIR did not replay exactly")
        return TableIRWriteReceipt(ir_ref=reference, newly_stored=newly_stored)

    def read(self, artifact_sha256: str) -> TableIR:
        _require_hash(artifact_sha256)
        with self._lock:
            payload = self._objects.get(artifact_sha256)
        if payload is None:
            raise AppError(ErrorCode.INVALID_REQUEST, "TableIR artifact does not exist")
        if not 1 <= len(payload) <= self._max_object_bytes:
            _integrity_error("stored TableIR violates its size limit")
        if hashlib.sha256(payload).hexdigest() != artifact_sha256:
            _integrity_error("stored TableIR bytes do not match their content address")
        try:
            table = TableIR.model_validate_json(payload)
        except ValidationError as exc:
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "stored TableIR failed strict validation",
            ) from exc
        verify_table_ir_integrity(table)
        if serialize_table_ir(table) != payload:
            _integrity_error("stored TableIR is not canonical JSON")
        return table

    def contains(self, artifact_sha256: str) -> bool:
        try:
            self.read(artifact_sha256)
        except AppError as exc:
            if exc.code is ErrorCode.INVALID_REQUEST:
                return False
            raise
        return True


def _require_hash(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise AppError(ErrorCode.INVALID_REQUEST, "TableIR hash must be lowercase SHA-256")


def _integrity_error(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)

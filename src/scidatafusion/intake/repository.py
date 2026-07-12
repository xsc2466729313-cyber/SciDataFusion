"""Idempotent in-memory checkpoint repository for M00."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from scidatafusion.contracts.base import ContentHash
from scidatafusion.contracts.task import IdempotencyKey, TaskIntakeResult
from scidatafusion.errors import AppError, ErrorCode


class IdempotencyConflictError(AppError):
    """Raised when one idempotency key is reused for different request content."""

    def __init__(self, idempotency_key: str) -> None:
        super().__init__(
            ErrorCode.INVALID_REQUEST,
            "Idempotency key was already used for a different intake request",
            details={"problem_code": "INTAKE_IDEMPOTENCY_CONFLICT", "key": idempotency_key},
        )


@dataclass(frozen=True, slots=True)
class StoredIntakeResult:
    """Repository record binding request content to one immutable result."""

    request_hash: ContentHash
    result: TaskIntakeResult


class TaskIntakeRepository(Protocol):
    """Checkpoint boundary used by the async intake service."""

    async def execute_once(
        self,
        idempotency_key: IdempotencyKey,
        request_hash: ContentHash,
        factory: Callable[[], Awaitable[TaskIntakeResult]],
    ) -> tuple[TaskIntakeResult, bool]:
        """Execute a factory once per key and report whether the result was replayed."""


class InMemoryTaskIntakeRepository:
    """Concurrency-safe in-memory repository with a separate lock for each task key."""

    def __init__(self) -> None:
        self._records: dict[str, StoredIntakeResult] = {}
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def execute_once(
        self,
        idempotency_key: IdempotencyKey,
        request_hash: ContentHash,
        factory: Callable[[], Awaitable[TaskIntakeResult]],
    ) -> tuple[TaskIntakeResult, bool]:
        """Serialize equal keys while allowing unrelated task keys to run concurrently."""

        lock = await self._lock_for(idempotency_key)
        async with lock:
            existing = self._records.get(idempotency_key)
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise IdempotencyConflictError(idempotency_key)
                return existing.result, True
            result = await factory()
            self._records[idempotency_key] = StoredIntakeResult(
                request_hash=request_hash,
                result=result,
            )
            return result, False

    async def count(self) -> int:
        """Return the number of immutable checkpoints currently held."""

        async with self._locks_guard:
            return len(self._records)

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._key_locks[key] = lock
            return lock

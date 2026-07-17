"""Idempotent asynchronous research-job persistence and execution."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from scidatafusion.contracts.platform import (
    ResearchJobPage,
    ResearchJobRecord,
    ResearchJobResult,
    ResearchJobStatus,
    ResearchJobSubmission,
)

JobExecutor = Callable[[ResearchJobSubmission], Awaitable[ResearchJobResult]]


class ResearchJobRepository(Protocol):
    async def create(self, record: ResearchJobRecord) -> ResearchJobRecord: ...

    async def get(self, job_id: str) -> ResearchJobRecord | None: ...

    async def replace(self, record: ResearchJobRecord) -> None: ...

    async def list(self, limit: int) -> tuple[ResearchJobRecord, ...]: ...


class InMemoryResearchJobRepository:
    """Process-local repository used by the zero-infrastructure mode."""

    def __init__(self) -> None:
        self._records: dict[str, ResearchJobRecord] = {}
        self._idempotency: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def create(self, record: ResearchJobRecord) -> ResearchJobRecord:
        async with self._lock:
            key = record.submission.idempotency_key
            if key is not None and key in self._idempotency:
                return self._records[self._idempotency[key]]
            self._records[record.job_id] = record
            if key is not None:
                self._idempotency[key] = record.job_id
            return record

    async def get(self, job_id: str) -> ResearchJobRecord | None:
        async with self._lock:
            return self._records.get(job_id)

    async def replace(self, record: ResearchJobRecord) -> None:
        async with self._lock:
            if record.job_id not in self._records:
                raise KeyError(record.job_id)
            self._records[record.job_id] = record

    async def list(self, limit: int) -> tuple[ResearchJobRecord, ...]:
        async with self._lock:
            records = sorted(
                self._records.values(), key=lambda item: item.submitted_at, reverse=True
            )
            return tuple(records[:limit])


class PostgresResearchJobRepository:
    """Minimal asyncpg repository storing complete validated records as JSONB."""

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS research_jobs (
            job_id TEXT PRIMARY KEY,
            idempotency_key TEXT,
            submitted_at TIMESTAMPTZ NOT NULL,
            record JSONB NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS research_jobs_idempotency_idx
        ON research_jobs (idempotency_key) WHERE idempotency_key IS NOT NULL;
    """

    def __init__(self, dsn: str, *, timeout_seconds: float = 10.0) -> None:
        self._dsn = dsn
        self._timeout_seconds = timeout_seconds
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    async def _connect(self) -> Any:
        asyncpg = importlib.import_module("asyncpg")
        return await asyncpg.connect(dsn=self._dsn, timeout=self._timeout_seconds)

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            connection = await self._connect()
            try:
                await connection.execute(self._SCHEMA)
            finally:
                await connection.close()
            self._schema_ready = True

    async def create(self, record: ResearchJobRecord) -> ResearchJobRecord:
        await self._ensure_schema()
        connection = await self._connect()
        try:
            row = await connection.fetchrow(
                """INSERT INTO research_jobs(job_id, idempotency_key, submitted_at, record)
                   VALUES($1, $2, $3, $4::jsonb)
                   ON CONFLICT DO NOTHING RETURNING record::text""",
                record.job_id,
                record.submission.idempotency_key,
                record.submitted_at,
                record.model_dump_json(),
            )
            if row is None and record.submission.idempotency_key is not None:
                row = await connection.fetchrow(
                    "SELECT record::text FROM research_jobs WHERE idempotency_key = $1",
                    record.submission.idempotency_key,
                )
            if row is None:
                row = await connection.fetchrow(
                    "SELECT record::text FROM research_jobs WHERE job_id = $1", record.job_id
                )
            if row is None:
                raise RuntimeError("research job was not persisted")
            return ResearchJobRecord.model_validate_json(row[0])
        finally:
            await connection.close()

    async def get(self, job_id: str) -> ResearchJobRecord | None:
        await self._ensure_schema()
        connection = await self._connect()
        try:
            row = await connection.fetchrow(
                "SELECT record::text FROM research_jobs WHERE job_id = $1", job_id
            )
            return None if row is None else ResearchJobRecord.model_validate_json(row[0])
        finally:
            await connection.close()

    async def replace(self, record: ResearchJobRecord) -> None:
        await self._ensure_schema()
        connection = await self._connect()
        try:
            result = await connection.execute(
                "UPDATE research_jobs SET record = $2::jsonb WHERE job_id = $1",
                record.job_id,
                record.model_dump_json(),
            )
            if result != "UPDATE 1":
                raise KeyError(record.job_id)
        finally:
            await connection.close()

    async def list(self, limit: int) -> tuple[ResearchJobRecord, ...]:
        await self._ensure_schema()
        connection = await self._connect()
        try:
            rows = await connection.fetch(
                "SELECT record::text FROM research_jobs ORDER BY submitted_at DESC LIMIT $1", limit
            )
            return tuple(ResearchJobRecord.model_validate_json(row[0]) for row in rows)
        finally:
            await connection.close()


class CeleryJobDispatcher:
    """Publish only validated, secret-free job identifiers and submissions."""

    def __init__(self, redis_url: str) -> None:
        celery = importlib.import_module("celery")
        self._client = celery.Celery(broker=redis_url, backend=redis_url)

    async def dispatch(self, record: ResearchJobRecord) -> None:
        await asyncio.to_thread(
            self._client.send_task,
            "scidatafusion.execute_research_job",
            args=[record.job_id, record.submission.model_dump(mode="json")],
        )


class ResearchJobService:
    """Create idempotent jobs and execute them locally or through Celery."""

    def __init__(
        self,
        repository: ResearchJobRepository,
        executor: JobExecutor,
        *,
        dispatcher: CeleryJobDispatcher | None = None,
    ) -> None:
        self._repository = repository
        self._executor = executor
        self._dispatcher = dispatcher
        self._background: set[asyncio.Task[None]] = set()

    async def submit(self, submission: ResearchJobSubmission) -> ResearchJobRecord:
        proposed = ResearchJobRecord(submission=submission)
        record = await self._repository.create(proposed)
        if record.job_id != proposed.job_id or record.status is not ResearchJobStatus.QUEUED:
            return record
        if self._dispatcher is not None:
            await self._dispatcher.dispatch(record)
        else:
            task = asyncio.create_task(self.execute(record.job_id))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        return record

    async def execute(self, job_id: str) -> None:
        record = await self._repository.get(job_id)
        if record is None or record.status is not ResearchJobStatus.QUEUED:
            return
        running = record.model_copy(
            update={"status": ResearchJobStatus.RUNNING, "started_at": datetime.now(UTC)}
        )
        await self._repository.replace(running)
        try:
            result = await self._executor(running.submission)
        except Exception:
            failed = running.model_copy(
                update={
                    "status": ResearchJobStatus.FAILED,
                    "failure_code": "research_execution_failed",
                    "finished_at": datetime.now(UTC),
                }
            )
            await self._repository.replace(failed)
            return
        succeeded = running.model_copy(
            update={
                "status": ResearchJobStatus.SUCCEEDED,
                "result": result,
                "finished_at": datetime.now(UTC),
            }
        )
        await self._repository.replace(succeeded)

    async def get(self, job_id: str) -> ResearchJobRecord | None:
        return await self._repository.get(job_id)

    async def list(self, limit: int = 20) -> ResearchJobPage:
        records = await self._repository.list(limit)
        return ResearchJobPage(items=records, count=len(records))

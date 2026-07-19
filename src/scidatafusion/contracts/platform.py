"""Strict contracts for deployable research jobs and vector indexing."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, StringConstraints, field_validator

from scidatafusion.contracts.base import ContentHash, StrictContract, utc_now
from scidatafusion.contracts.workbench import WorkbenchSnapshot

JobId = Annotated[str, StringConstraints(pattern=r"^job_[0-9a-f]{32}$")]
IdempotencyKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=8, max_length=128, pattern=r"^[\w.-]+$"),
]
JobMessage = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]


class ResearchJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ResearchJobSubmission(StrictContract):
    execution_mode: Literal["offline", "online"] = "offline"
    research_goal: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=10, max_length=2_000)
    ]
    retrieval_query: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=3, max_length=512)
    ] = None
    idempotency_key: IdempotencyKey | None = None


class ResearchJobResult(StrictContract):
    task_id: str
    run_id: str
    quality_gate_passed: bool
    quality_score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    source_count: int = Field(ge=0)
    evidence_count: int = Field(ge=0)
    artifact_count: int = Field(ge=0)
    issue_count: int = Field(ge=0)
    formal_gold_record_count: int = Field(ge=0)
    package_filename: str
    workbench_snapshot: WorkbenchSnapshot | None = None


class ResearchJobRecord(StrictContract):
    job_id: JobId = Field(default_factory=lambda: f"job_{uuid4().hex}")
    status: ResearchJobStatus = ResearchJobStatus.QUEUED
    submission: ResearchJobSubmission
    result: ResearchJobResult | None = None
    failure_code: str | None = None
    failure_message: JobMessage | None = None
    recovery_action: JobMessage | None = None
    submitted_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @field_validator("submitted_at", "started_at", "finished_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("job timestamps must include a timezone")
        return value.astimezone(UTC)


class ResearchJobPage(StrictContract):
    items: tuple[ResearchJobRecord, ...]
    count: int = Field(ge=0)


class PlatformComponent(StrictContract):
    name: Literal[
        "fastapi",
        "postgresql",
        "redis_celery",
        "chroma",
        "langgraph",
        "langchain",
        "llamaindex",
        "sklearn",
        "pytorch",
    ]
    status: Literal["ready", "optional", "disabled", "unavailable"]
    detail: str


class PlatformStatus(StrictContract):
    mode: Literal["local", "celery"]
    components: tuple[PlatformComponent, ...]


class EvidenceVectorDocument(StrictContract):
    document_id: ContentHash
    evidence_id: str
    task_id: str
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8_192)]
    source_hash: ContentHash
    field_name: str
    location: str


class VectorIndexReport(StrictContract):
    indexed_count: int = Field(ge=0)
    dimensions: int = Field(ge=1)
    engine: Literal["python-hashing", "sklearn-hashing"]
    torch_validated: bool
    langchain_document_count: int = Field(ge=0)
    llamaindex_node_count: int = Field(ge=0)

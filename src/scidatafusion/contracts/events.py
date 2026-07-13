"""Immutable event envelope for workflow replay and audit."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import Field, field_validator

from scidatafusion.contracts.base import (
    EventId,
    NonEmptyStr,
    RunId,
    SemanticVersion,
    StrictContract,
    TaskId,
    generate_id,
    utc_now,
)


class EventType(StrEnum):
    """Event names reserved by the V4 state model."""

    TASK_CREATED = "task.created"
    TASK_ACCEPTED = "task.accepted"
    TASK_REJECTED = "task.rejected"
    PROBLEM_COMPILED = "problem.compiled"
    ROUTING_COMPLETED = "routing.completed"
    CONTRACT_COMPILED = "contract.compiled"
    CONTRACT_CONFIRMED = "contract.confirmed"
    SEARCH_PLAN_CREATED = "search.plan.created"
    CONNECTOR_BATCH_COMPLETED = "connector.batch.completed"
    SELECTION_COMPLETED = "selection.completed"
    SEARCH_COMPLETED = "search.completed"
    ARTIFACT_STORED = "artifact.stored"
    ARTIFACT_DOWNLOAD_COMPLETED = "artifact.download.completed"
    PARSE_PLAN_CREATED = "parse.plan.created"
    DOCUMENT_PARSED = "document.parsed"
    TABLE_PARSED = "table.parsed"
    FIELD_EXTRACTED = "field.extracted"
    FIELD_MAPPED = "field.mapped"
    RECORD_NORMALIZED = "record.normalized"
    ENTITY_RESOLVED = "entity.resolved"
    FUSION_COMPLETED = "fusion.completed"
    QUALITY_GATED = "quality.gated"
    KNOWLEDGE_UPDATED = "knowledge.updated"
    FIGURE_DIGITIZED = "figure.digitized"
    QUALITY_ISSUE_CREATED = "quality.issue.created"
    REVIEW_RESOLVED = "review.resolved"
    DELIVERY_COMPLETED = "delivery.completed"


class ProducerRef(StrictContract):
    """Versioned component that emitted an event."""

    component: NonEmptyStr
    version: SemanticVersion


PayloadT = TypeVar("PayloadT", bound=StrictContract)


class EventEnvelope(StrictContract, Generic[PayloadT]):
    """Typed event record; large payloads are represented by artifact references."""

    event_id: EventId = Field(default_factory=lambda: generate_id("evt"))
    event_type: EventType
    task_id: TaskId
    run_id: RunId
    occurred_at: datetime = Field(default_factory=utc_now)
    schema_version: SemanticVersion = "1.0.0"
    producer: ProducerRef
    payload: PayloadT
    correlation_id: NonEmptyStr | None = None
    causation_event_id: EventId | None = None

    @field_validator("occurred_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "occurred_at must include a timezone"
            raise ValueError(msg)
        return value.astimezone(UTC)

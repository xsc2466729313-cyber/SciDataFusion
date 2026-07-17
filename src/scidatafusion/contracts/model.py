"""Contracts for auditable structured model calls."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from scidatafusion.contracts.base import (
    ContentHash,
    ModelCallId,
    NonEmptyStr,
    SemanticVersion,
    StrictContract,
    generate_id,
    utc_now,
)


class ModelRole(StrEnum):
    PLANNER = "planner"
    FAST_CLASSIFIER = "fast_classifier"
    FIELD_MAPPER = "field_mapper"
    CRITIC = "critic"


class StructuredModelRequest(StrictContract):
    """Validated prompt request; prompt text is not retained in invocation records."""

    role: ModelRole
    model_id: NonEmptyStr
    system_prompt: NonEmptyStr
    user_prompt: NonEmptyStr
    prompt_version: SemanticVersion
    schema_name: NonEmptyStr
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    max_tokens: int = Field(default=2048, ge=1, le=32768)


class ModelUsage(StrictContract):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class ModelInvocationRecord(StrictContract):
    """Secret-free proof of a Bailian model invocation."""

    invocation_id: ModelCallId = Field(default_factory=lambda: generate_id("mdl"))
    provider: NonEmptyStr = "bailian"
    region: NonEmptyStr
    endpoint_host: NonEmptyStr
    requested_model: NonEmptyStr
    actual_model: NonEmptyStr
    role: ModelRole
    prompt_version: SemanticVersion
    schema_name: NonEmptyStr
    request_hash: ContentHash
    response_hash: ContentHash
    usage: ModelUsage
    latency_ms: float = Field(ge=0.0)
    attempt_count: int = Field(ge=1)
    cached: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class StructuredModelCompletion(StrictContract):
    content: NonEmptyStr
    invocation: ModelInvocationRecord

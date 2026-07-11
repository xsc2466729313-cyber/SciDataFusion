"""Primitive contracts shared by every workflow module."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SemanticVersion = Annotated[
    str,
    StringConstraints(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"),
]
TaskId = Annotated[str, StringConstraints(pattern=r"^tsk_[0-9a-f]{32}$")]
RunId = Annotated[str, StringConstraints(pattern=r"^run_[0-9a-f]{32}$")]
EventId = Annotated[str, StringConstraints(pattern=r"^evt_[0-9a-f]{32}$")]
ArtifactId = Annotated[str, StringConstraints(pattern=r"^art_[0-9a-f]{32}$")]
ContentHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

IdPrefix = Literal["tsk", "run", "evt", "art"]


def generate_id(prefix: IdPrefix) -> str:
    """Generate an opaque UUID4 identifier with a typed prefix."""

    return f"{prefix}_{uuid4().hex}"


def utc_now() -> datetime:
    """Return an aware UTC timestamp for contract defaults."""

    return datetime.now(UTC)


class StrictContract(BaseModel):
    """Base for validated, immutable cross-module contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class ArtifactReference(StrictContract):
    """Reference to immutable, content-addressed bytes outside workflow state."""

    artifact_id: ArtifactId = Field(default_factory=lambda: generate_id("art"))
    uri: NonEmptyStr
    sha256: ContentHash
    media_type: NonEmptyStr
    size_bytes: int = Field(ge=0)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "created_at must include a timezone"
            raise ValueError(msg)
        return value.astimezone(UTC)

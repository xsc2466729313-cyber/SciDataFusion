"""Immutable contracts for deterministic domain and task routing."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from scidatafusion.contracts.base import (
    ContentHash,
    NonEmptyStr,
    RunId,
    SemanticVersion,
    StrictContract,
    TaskId,
    generate_id,
    utc_now,
)

PackName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,63}$"),
]
CapabilityName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,63}$"),
]
RoutingEvidenceId = Annotated[
    str,
    StringConstraints(pattern=r"^rte_[0-9a-f]{32}$"),
]


class EvidenceKind(StrEnum):
    """Deterministic evidence categories used by the router."""

    KEYWORD = "keyword"
    RELATIONSHIP = "relationship"
    SAFETY_FILTER = "safety_filter"
    FALLBACK = "fallback"
    CAPABILITY = "capability"


class RoutingMode(StrEnum):
    """How selected packs may be used downstream."""

    FORMAL = "formal"
    PROVISIONAL = "provisional"
    GENERIC = "generic"
    UNSUPPORTED = "unsupported"


class RoutingStatus(StrEnum):
    """M02 business outcome, aligned with the module result vocabulary."""

    SUCCEEDED = "succeeded"
    NEEDS_REVIEW = "needs_review"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class RoutingEvidence(StrictContract):
    """One replayable observation supporting or constraining a route."""

    evidence_id: RoutingEvidenceId
    kind: EvidenceKind
    source: Literal[
        "research_goal",
        "domain_registry",
        "task_registry",
        "capability_registry",
        "router",
    ]
    target: NonEmptyStr
    signal: NonEmptyStr
    weight: float = Field(ge=0.0, le=10.0, allow_inf_nan=False)
    matched_text: str | None = Field(default=None, max_length=256)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)
    rationale: NonEmptyStr

    @model_validator(mode="after")
    def validate_span(self) -> Self:
        """Require complete, ordered source spans when a span is present."""

        if (self.start is None) != (self.end is None):
            msg = "start and end must either both be present or both be absent"
            raise ValueError(msg)
        if self.start is not None and self.end is not None and self.end <= self.start:
            msg = "evidence end must be greater than start"
            raise ValueError(msg)
        if self.matched_text is None and self.start is not None:
            msg = "source spans require matched_text"
            raise ValueError(msg)
        return self


class RankedDomain(StrictContract):
    """A deterministic candidate score retained for audit and calibration."""

    domain: PackName
    score: float = Field(ge=0.0, allow_inf_nan=False)
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    evidence_count: int = Field(ge=0)


class DomainProfile(StrictContract):
    """Ordered multi-domain classification with explicit uncertainty."""

    primary_domain: PackName
    secondary_domains: tuple[PackName, ...] = ()
    subdomains: tuple[PackName, ...] = ()
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    provisional: bool
    ranked_candidates: tuple[RankedDomain, ...]
    evidence: tuple[RoutingEvidence, ...]

    @model_validator(mode="after")
    def validate_domain_order(self) -> Self:
        """Ensure domain labels and ranks are unique and internally consistent."""

        domains = (self.primary_domain, *self.secondary_domains)
        if len(domains) != len(set(domains)):
            msg = "primary and secondary domains must be unique"
            raise ValueError(msg)
        ranked = tuple(item.domain for item in self.ranked_candidates)
        if len(ranked) != len(set(ranked)):
            msg = "ranked domain candidates must be unique"
            raise ValueError(msg)
        if not ranked or ranked[0] != self.primary_domain:
            msg = "primary_domain must be the highest-ranked candidate"
            raise ValueError(msg)
        return self


class RankedArchetype(StrictContract):
    """A scored task archetype retained for replay and evaluation."""

    archetype: PackName
    score: float = Field(ge=0.0, allow_inf_nan=False)
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    evidence_count: int = Field(ge=0)


class TaskArchetypeSet(StrictContract):
    """Ordered, multi-label task-archetype classification."""

    primary_archetype: PackName
    archetypes: tuple[PackName, ...]
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    provisional: bool
    ranked_candidates: tuple[RankedArchetype, ...]
    evidence: tuple[RoutingEvidence, ...]

    @model_validator(mode="after")
    def validate_archetypes(self) -> Self:
        """Keep primary and ranked ordering stable and duplicate-free."""

        if not self.archetypes or self.archetypes[0] != self.primary_archetype:
            msg = "primary_archetype must be the first selected archetype"
            raise ValueError(msg)
        if len(self.archetypes) != len(set(self.archetypes)):
            msg = "archetypes must be unique"
            raise ValueError(msg)
        ranked = tuple(item.archetype for item in self.ranked_candidates)
        if not ranked or ranked[0] != self.primary_archetype:
            msg = "primary_archetype must be the highest-ranked candidate"
            raise ValueError(msg)
        return self


class PackReference(StrictContract):
    """Content-addressed reference to a validated pack manifest."""

    name: PackName
    pack_type: Literal["domain", "task"]
    version: SemanticVersion
    content_hash: ContentHash


class PackSelection(StrictContract):
    """Packs that are enabled, proposed, or blocked by missing capabilities."""

    mode: RoutingMode
    domain_packs: tuple[PackReference, ...] = ()
    task_packs: tuple[PackReference, ...] = ()
    proposed_domain_packs: tuple[PackReference, ...] = ()
    proposed_task_packs: tuple[PackReference, ...] = ()
    missing_capabilities: tuple[CapabilityName, ...] = ()
    fallback_path: tuple[NonEmptyStr, ...]
    evidence: tuple[RoutingEvidence, ...]

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        """Prevent a proposed or unsupported specialist pack from being silently enabled."""

        enabled = {(item.pack_type, item.name) for item in (*self.domain_packs, *self.task_packs)}
        proposed = {
            (item.pack_type, item.name)
            for item in (*self.proposed_domain_packs, *self.proposed_task_packs)
        }
        if enabled & proposed:
            msg = "a pack cannot be both enabled and proposed"
            raise ValueError(msg)
        if self.mode == RoutingMode.UNSUPPORTED and enabled:
            msg = "unsupported routes cannot enable packs"
            raise ValueError(msg)
        if len(self.missing_capabilities) != len(set(self.missing_capabilities)):
            msg = "missing capabilities must be unique"
            raise ValueError(msg)
        return self


class RoutingRequest(StrictContract):
    """Validated M02 input independent of any model-generated routing suggestion."""

    task_id: TaskId = Field(default_factory=lambda: generate_id("tsk"))
    run_id: RunId = Field(default_factory=lambda: generate_id("run"))
    research_goal: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000)
    ]
    context: tuple[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)],
        ...,
    ] = ()
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        """Normalize the request timestamp so replay metadata is unambiguous."""

        if value.tzinfo is None or value.utcoffset() is None:
            msg = "created_at must include a timezone"
            raise ValueError(msg)
        return value.astimezone(UTC)


class RoutingDecision(StrictContract):
    """Complete M02 output, including evidence, confidence, and fallback route."""

    task_id: TaskId
    run_id: RunId
    module_id: Literal["M02"] = "M02"
    contract_version: SemanticVersion = "1.0.0"
    producer_version: SemanticVersion
    created_at: datetime
    status: RoutingStatus
    input_hash: ContentHash
    registry_hash: ContentHash
    replay_key: ContentHash
    decision_hash: ContentHash
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    domain_profile: DomainProfile
    task_archetypes: TaskArchetypeSet
    pack_selection: PackSelection
    evidence: tuple[RoutingEvidence, ...]
    fallback_path: tuple[NonEmptyStr, ...]
    warnings: tuple[NonEmptyStr, ...] = ()

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        """Normalize decision creation time to UTC."""

        if value.tzinfo is None or value.utcoffset() is None:
            msg = "created_at must include a timezone"
            raise ValueError(msg)
        return value.astimezone(UTC)


class RoutingMetrics(StrictContract):
    """Metrics calculated from labeled routing results, never hand-entered."""

    sample_count: int = Field(ge=0)
    domain_accuracy: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    archetype_macro_f1: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    unsupported_recall: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)

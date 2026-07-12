"""Immutable contracts for compiling a natural-language scientific problem."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, StringConstraints, field_validator, model_validator

from scidatafusion.contracts.base import (
    ContentHash,
    RunId,
    SemanticVersion,
    StrictContract,
    TaskId,
)
from scidatafusion.contracts.events import EventEnvelope, EventType
from scidatafusion.contracts.model import ModelInvocationRecord

ProblemText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000),
]
RawProblemText = Annotated[str, StringConstraints(min_length=1, max_length=50_000)]
ProblemId = Annotated[str, StringConstraints(pattern=r"^prb_[0-9a-f]{32}$")]
Confidence = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]


class ProblemContract(StrictContract):
    """Strict deeply-immutable base for M01 contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=False,
        validate_default=True,
    )


class SpanOrigin(StrEnum):
    """Trusted location from which a span was copied."""

    USER_INPUT = "user_input"
    USER_CONFIRMATION = "user_confirmation"
    DOMAIN_TERM = "domain_term"


class ExtractionMethod(StrEnum):
    """How an inferred candidate was obtained."""

    USER_EXPLICIT = "user_explicit"
    DETERMINISTIC_RULE = "deterministic_rule"
    EXTERNAL_CANDIDATE = "external_candidate"
    USER_CONFIRMED = "user_confirmed"


class SourceSpan(ProblemContract):
    """An exact, half-open character span copied from immutable source text."""

    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: RawProblemText
    origin: SpanOrigin = SpanOrigin.USER_INPUT

    @model_validator(mode="after")
    def require_ordered_bounds(self) -> Self:
        if self.end <= self.start:
            msg = "source span end must be greater than start"
            raise ValueError(msg)
        if self.end - self.start != len(self.text):
            msg = "source span length must equal end - start"
            raise ValueError(msg)
        return self

    def matches(self, source_text: str) -> bool:
        """Return whether this span exactly addresses ``source_text``."""

        return self.end <= len(source_text) and source_text[self.start : self.end] == self.text


class EvidenceBoundIntent(ProblemContract):
    """Common audit fields required for every inferred intent."""

    confidence: Confidence
    evidence: tuple[SourceSpan, ...] = Field(min_length=1)
    method: ExtractionMethod
    basis: ProblemText


class EntityIntent(EvidenceBoundIntent):
    """A research object or population explicitly grounded in user text."""

    name: ProblemText
    entity_type: ProblemText | None = None


class VariableRole(StrEnum):
    """Requested role of a scientific variable."""

    TARGET = "target"
    INDEPENDENT = "independent"
    DEPENDENT = "dependent"
    MEASUREMENT = "measurement"
    UNKNOWN = "unknown"


class VariableIntent(EvidenceBoundIntent):
    """A requested observable, measurement, or scientific variable."""

    name: ProblemText
    role: VariableRole = VariableRole.TARGET
    requested_unit: ProblemText | None = None


class ConditionKind(StrEnum):
    """Semantic role of an explicitly stated condition."""

    FILTER = "filter"
    OBSERVATION = "observation"
    EXPERIMENT = "experiment"
    EXCLUSION = "exclusion"
    UNKNOWN = "unknown"


class ConditionIntent(EvidenceBoundIntent):
    """An observation, experiment, filter, or exclusion condition."""

    expression: ProblemText
    kind: ConditionKind = ConditionKind.FILTER
    negated: bool = False


class ScopeDimension(StrEnum):
    """Supported scope dimensions at problem-compilation time."""

    TEMPORAL = "temporal"
    SPATIAL = "spatial"


class ScopeIntent(EvidenceBoundIntent):
    """A scope retained verbatim without interpreting scientific values."""

    dimension: ScopeDimension
    expression: ProblemText


class OutputFormat(StrEnum):
    """Output formats that a user can state explicitly."""

    CSV = "csv"
    PARQUET = "parquet"
    JSON = "json"
    NOTEBOOK = "notebook"


class OutputPreference(EvidenceBoundIntent):
    """An output preference explicitly present in the request."""

    format: OutputFormat


class ProblemUnit(EvidenceBoundIntent):
    """One independently stated research question."""

    unit_id: Annotated[str, StringConstraints(pattern=r"^unit_[0-9a-f]{16}$")]
    question: ProblemText


class AssumptionStatus(StrEnum):
    """Review state for an editable operational assumption."""

    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class Assumption(EvidenceBoundIntent):
    """An explicit, editable assumption; never a fabricated scientific value."""

    assumption_id: Annotated[str, StringConstraints(pattern=r"^asm_[0-9a-f]{16}$")]
    statement: ProblemText
    rationale: ProblemText
    editable: Literal[True] = True
    status: AssumptionStatus = AssumptionStatus.PROPOSED


class Ambiguity(ProblemContract):
    """A deterministic ambiguity finding with evidence and severity."""

    code: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{2,63}$")]
    message: ProblemText
    blocking: bool
    confidence: Confidence
    evidence: tuple[SourceSpan, ...] = Field(min_length=1)


class ClarificationQuestion(ProblemContract):
    """A minimal user question that resolves one or more blocking findings."""

    question_id: Annotated[str, StringConstraints(pattern=r"^clar_[0-9a-f]{16}$")]
    text: ProblemText
    resolves: tuple[Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{2,63}$")], ...] = (
        Field(min_length=1)
    )


class ProblemArtifact(ProblemContract):
    """Metadata required on every persisted M01 artifact."""

    task_id: TaskId
    run_id: RunId
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "created_at must include a timezone"
            raise ValueError(msg)
        return value.astimezone(UTC)


class ScientificProblemSpec(ProblemArtifact):
    """Machine-consumable, evidence-grounded compilation of a research goal."""

    problem_id: ProblemId
    raw_text: RawProblemText
    research_goal: RawProblemText
    research_questions: tuple[ProblemText, ...] = Field(min_length=1)
    problem_units: tuple[ProblemUnit, ...] = Field(min_length=1)
    target_entities: tuple[EntityIntent, ...] = ()
    target_variables: tuple[VariableIntent, ...] = ()
    conditions: tuple[ConditionIntent, ...] = ()
    temporal_scope: ScopeIntent | None = None
    spatial_scope: ScopeIntent | None = None
    output_preferences: tuple[OutputPreference, ...] = ()
    assumptions: tuple[Assumption, ...] = ()
    source_spans: tuple[SourceSpan, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence_graph(self) -> Self:
        if self.raw_text != self.research_goal:
            msg = "research_goal must preserve raw_text exactly"
            raise ValueError(msg)
        expected_questions = tuple(unit.question for unit in self.problem_units)
        if self.research_questions != expected_questions:
            msg = "research_questions must match problem_units in order"
            raise ValueError(msg)

        declared_spans = set(self.source_spans)
        evidence_groups = [
            *(item.evidence for item in self.problem_units),
            *(item.evidence for item in self.target_entities),
            *(item.evidence for item in self.target_variables),
            *(item.evidence for item in self.conditions),
            *(item.evidence for item in self.output_preferences),
            *(item.evidence for item in self.assumptions),
        ]
        if self.temporal_scope is not None:
            evidence_groups.append(self.temporal_scope.evidence)
        if self.spatial_scope is not None:
            evidence_groups.append(self.spatial_scope.evidence)

        if any(not span.matches(self.raw_text) for span in self.source_spans):
            msg = "every source span must exactly match raw_text"
            raise ValueError(msg)
        for group in evidence_groups:
            if any(span not in declared_spans for span in group):
                msg = "every evidence span must be declared in source_spans"
                raise ValueError(msg)
        return self


class AmbiguityReport(ProblemArtifact):
    """All detected ambiguities and the minimal blocking clarification set."""

    problem_id: ProblemId
    requires_clarification: bool
    ambiguities: tuple[Ambiguity, ...] = ()
    questions: tuple[ClarificationQuestion, ...] = ()

    @model_validator(mode="after")
    def validate_clarification_state(self) -> Self:
        has_blocker = any(item.blocking for item in self.ambiguities)
        if self.requires_clarification != has_blocker:
            msg = "requires_clarification must match blocking ambiguities"
            raise ValueError(msg)
        if has_blocker != bool(self.questions):
            msg = "blocking ambiguities require clarification questions only"
            raise ValueError(msg)
        return self


class AssumptionRegister(ProblemArtifact):
    """Editable assumptions produced by deterministic compilation rules."""

    problem_id: ProblemId
    assumptions: tuple[Assumption, ...] = ()


class CandidateBatch(ProblemContract):
    """Untrusted extractor output accepted only after full validation."""

    problem_units: tuple[ProblemUnit, ...] = Field(min_length=1)
    entities: tuple[EntityIntent, ...] = ()
    variables: tuple[VariableIntent, ...] = ()
    conditions: tuple[ConditionIntent, ...] = ()
    temporal_scope: ScopeIntent | None = None
    spatial_scope: ScopeIntent | None = None
    output_preferences: tuple[OutputPreference, ...] = ()


class CompilationStatus(StrEnum):
    """M01 result state."""

    SUCCEEDED = "succeeded"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class ProblemCompiledPayload(ProblemContract):
    """Small event payload referring to a compiled problem artifact."""

    problem_id: ProblemId
    status: CompilationStatus
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    used_fallback: bool


class ProblemCompilationResult(ProblemArtifact):
    """Complete M01 return value including artifacts and immutable event."""

    module_id: Literal["M01"] = "M01"
    status: CompilationStatus
    problem_spec: ScientificProblemSpec
    ambiguity_report: AmbiguityReport
    assumption_register: AssumptionRegister
    event: EventEnvelope[ProblemCompiledPayload]
    model_invocations: tuple[ModelInvocationRecord, ...] = ()
    used_fallback: bool = False
    warnings: tuple[ProblemText, ...] = ()
    metrics: tuple[tuple[ProblemText, float], ...] = ()

    @model_validator(mode="after")
    def validate_linked_artifacts(self) -> Self:
        artifacts = (self.problem_spec, self.ambiguity_report, self.assumption_register)
        if any(
            artifact.task_id != self.task_id
            or artifact.run_id != self.run_id
            or artifact.contract_version != self.contract_version
            or artifact.producer_version != self.producer_version
            for artifact in artifacts
        ):
            msg = "linked M01 artifacts must share result metadata"
            raise ValueError(msg)
        if any(artifact.problem_id != self.problem_spec.problem_id for artifact in artifacts):
            msg = "linked M01 artifacts must share problem_id"
            raise ValueError(msg)
        if self.problem_spec.assumptions != self.assumption_register.assumptions:
            msg = "problem spec and assumption register must agree"
            raise ValueError(msg)
        if (
            self.event.event_type is not EventType.PROBLEM_COMPILED
            or self.event.task_id != self.task_id
            or self.event.run_id != self.run_id
            or self.event.payload.problem_id != self.problem_spec.problem_id
            or self.event.payload.status is not self.status
        ):
            msg = "problem.compiled event must refer to this result"
            raise ValueError(msg)
        return self

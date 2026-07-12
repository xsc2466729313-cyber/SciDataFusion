"""Evidence-grounded scientific problem compiler."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.model import ModelInvocationRecord
from scidatafusion.contracts.problem import (
    Ambiguity,
    AmbiguityReport,
    Assumption,
    AssumptionRegister,
    CandidateBatch,
    ClarificationQuestion,
    CompilationStatus,
    ExtractionMethod,
    ProblemCompilationResult,
    ProblemCompiledPayload,
    ScientificProblemSpec,
    SourceSpan,
)
from scidatafusion.contracts.task import TaskEnvelope
from scidatafusion.errors import AppError
from scidatafusion.problem.fallback import DeterministicCandidateExtractor

_CONTRACT_VERSION = "1.0.0"
_PRODUCER_VERSION = "0.1.0"
_AMBIGUOUS_SCOPE = re.compile(
    r"\b(?:recent|nearby|high|low|large|small|soon)\b|最近|近期|附近|较高|较低|大量|少量",
    re.IGNORECASE,
)


class CandidateExtractor(Protocol):
    """Replaceable candidate provider; all returned values remain untrusted."""

    async def extract(self, text: str) -> object:
        """Return a candidate payload for deterministic validation."""
        ...


@runtime_checkable
class AuditedCandidateExtractor(CandidateExtractor, Protocol):
    """Optional extension exposing secret-free model invocation records."""

    @property
    def invocations(self) -> tuple[ModelInvocationRecord, ...]:
        """Return invocation records for the current async execution context."""
        ...


class ProblemCompilerInputError(ValueError):
    """Structured precondition failure for an invalid upstream task."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _stable_id(prefix: str, value: str, *, size: int = 16) -> str:
    return f"{prefix}_{_sha256(value)[:size]}"


def _whole_span(text: str) -> SourceSpan:
    return SourceSpan(start=0, end=len(text), text=text)


def _all_evidence(batch: CandidateBatch) -> tuple[SourceSpan, ...]:
    groups = [
        *(item.evidence for item in batch.problem_units),
        *(item.evidence for item in batch.entities),
        *(item.evidence for item in batch.variables),
        *(item.evidence for item in batch.conditions),
        *(item.evidence for item in batch.output_preferences),
    ]
    if batch.temporal_scope is not None:
        groups.append(batch.temporal_scope.evidence)
    if batch.spatial_scope is not None:
        groups.append(batch.spatial_scope.evidence)
    return tuple(span for group in groups for span in group)


def _is_grounded(value: str, evidence: tuple[SourceSpan, ...]) -> bool:
    normalized = " ".join(value.casefold().split())
    return any(normalized in " ".join(span.text.casefold().split()) for span in evidence)


def _mark_external(batch: CandidateBatch) -> CandidateBatch:
    audit_update = {
        "method": ExtractionMethod.EXTERNAL_CANDIDATE,
        "basis": "External candidate validated against exact accepted-task source spans.",
    }
    return batch.model_copy(
        update={
            "problem_units": tuple(
                item.model_copy(update=audit_update) for item in batch.problem_units
            ),
            "entities": tuple(item.model_copy(update=audit_update) for item in batch.entities),
            "variables": tuple(item.model_copy(update=audit_update) for item in batch.variables),
            "conditions": tuple(item.model_copy(update=audit_update) for item in batch.conditions),
            "temporal_scope": batch.temporal_scope.model_copy(update=audit_update)
            if batch.temporal_scope is not None
            else None,
            "spatial_scope": batch.spatial_scope.model_copy(update=audit_update)
            if batch.spatial_scope is not None
            else None,
            "output_preferences": tuple(
                item.model_copy(update=audit_update) for item in batch.output_preferences
            ),
        }
    )


class ProblemSpecValidator:
    """Validate untrusted extractor output and its complete source grounding."""

    def validate(
        self,
        raw: object,
        source_text: str,
        *,
        mark_external: bool = True,
    ) -> CandidateBatch:
        """Parse a candidate payload and reject invented or invalid spans."""

        if isinstance(raw, CandidateBatch):
            candidate = CandidateBatch.model_validate_json(raw.model_dump_json())
        elif isinstance(raw, (str, bytes, bytearray)):
            candidate = CandidateBatch.model_validate_json(raw)
        else:
            serialized = json.dumps(raw, ensure_ascii=False, allow_nan=False)
            candidate = CandidateBatch.model_validate_json(serialized)

        if any(not span.matches(source_text) for span in _all_evidence(candidate)):
            msg = "candidate evidence contains a span outside the accepted task text"
            raise ValueError(msg)

        grounded_values: list[tuple[str, tuple[SourceSpan, ...]]] = [
            *((item.question, item.evidence) for item in candidate.problem_units),
            *((item.name, item.evidence) for item in candidate.entities),
            *((item.name, item.evidence) for item in candidate.variables),
            *((item.expression, item.evidence) for item in candidate.conditions),
            *((item.format.value, item.evidence) for item in candidate.output_preferences),
        ]
        if candidate.temporal_scope is not None:
            grounded_values.append(
                (candidate.temporal_scope.expression, candidate.temporal_scope.evidence)
            )
        if candidate.spatial_scope is not None:
            grounded_values.append(
                (candidate.spatial_scope.expression, candidate.spatial_scope.evidence)
            )
        if any(not _is_grounded(value, evidence) for value, evidence in grounded_values):
            msg = "candidate value is not grounded in its declared source span"
            raise ValueError(msg)
        return _mark_external(candidate) if mark_external else candidate


class AmbiguityDetector:
    """Detect blockers and emit one combined minimal clarification question."""

    def detect(
        self,
        text: str,
        candidates: CandidateBatch,
    ) -> tuple[tuple[Ambiguity, ...], tuple[ClarificationQuestion, ...]]:
        """Return deterministic ambiguity findings and at most one question."""

        whole = _whole_span(text)
        findings: list[Ambiguity] = []
        if not candidates.entities:
            findings.append(
                Ambiguity(
                    code="missing_entity",
                    message="No research object could be grounded in the request.",
                    blocking=True,
                    confidence=1.0,
                    evidence=(whole,),
                )
            )
        if not candidates.variables:
            findings.append(
                Ambiguity(
                    code="missing_variable",
                    message="No target variable or data product could be grounded in the request.",
                    blocking=True,
                    confidence=1.0,
                    evidence=(whole,),
                )
            )
        if len(candidates.problem_units) > 1:
            findings.append(
                Ambiguity(
                    code="multiple_independent_questions",
                    message="The request contains multiple independently stated research questions.",
                    blocking=True,
                    confidence=0.98,
                    evidence=tuple(unit.evidence[0] for unit in candidates.problem_units),
                )
            )
        for match in _AMBIGUOUS_SCOPE.finditer(text):
            span = SourceSpan(start=match.start(), end=match.end(), text=match.group())
            findings.append(
                Ambiguity(
                    code="ambiguous_scope",
                    message="A qualitative scope needs an explicit boundary before filtering.",
                    blocking=True,
                    confidence=0.95,
                    evidence=(span,),
                )
            )

        blocking_codes = tuple(dict.fromkeys(item.code for item in findings if item.blocking))
        if not blocking_codes:
            return tuple(findings), ()
        has_chinese = bool(re.search(r"[\u4e00-\u9fff]", text))
        if has_chinese:
            question_text = (
                "请在一次回复中确认独立问题的处理方式, 并补充缺失的研究对象、目标变量或精确范围。"
            )
        else:
            question_text = (
                "In one reply, confirm how to handle independent questions and provide any missing "
                "research object, target variable, or exact scope."
            )
        question = ClarificationQuestion(
            question_id=_stable_id("clar", "|".join(blocking_codes)),
            text=question_text,
            resolves=blocking_codes,
        )
        return tuple(findings), (question,)


class AssumptionRegistry:
    """Create explicit operational assumptions for non-blocking omissions."""

    def build(self, text: str, candidates: CandidateBatch) -> tuple[Assumption, ...]:
        """Return editable assumptions without inventing scientific values."""

        whole = _whole_span(text)
        statements: list[tuple[str, str]] = []
        if candidates.temporal_scope is None:
            statements.append(
                (
                    "No temporal filter will be imposed.",
                    "The accepted task does not state a temporal boundary; temporal scope remains unknown.",
                )
            )
        if candidates.spatial_scope is None:
            statements.append(
                (
                    "No spatial filter will be imposed.",
                    "The accepted task does not state a spatial boundary; spatial scope remains unknown.",
                )
            )
        if not candidates.output_preferences:
            statements.append(
                (
                    "Output format selection remains open for the downstream data contract.",
                    "The accepted task does not explicitly name an output format.",
                )
            )

        return tuple(
            Assumption(
                assumption_id=_stable_id("asm", statement),
                statement=statement,
                rationale=rationale,
                confidence=1.0,
                evidence=(whole,),
                method=ExtractionMethod.DETERMINISTIC_RULE,
                basis="Deterministic absence check against the complete accepted task text.",
            )
            for statement, rationale in statements
        )


def _unique_spans(*groups: tuple[SourceSpan, ...]) -> tuple[SourceSpan, ...]:
    unique = {span for group in groups for span in group}
    return tuple(
        sorted(unique, key=lambda span: (span.start, span.end, span.text, span.origin.value))
    )


class ProblemCompilerAgent:
    """Compile an accepted M00 task into immutable, auditable M01 artifacts."""

    def __init__(
        self,
        extractor: CandidateExtractor | None = None,
        *,
        fallback: CandidateExtractor | None = None,
        clock: Callable[[], datetime] = utc_now,
        contract_version: str = _CONTRACT_VERSION,
        producer_version: str = _PRODUCER_VERSION,
    ) -> None:
        self._extractor = extractor
        self._fallback = fallback or DeterministicCandidateExtractor()
        self._clock = clock
        self._contract_version = contract_version
        self._producer_version = producer_version
        self._validator = ProblemSpecValidator()
        self._ambiguity_detector = AmbiguityDetector()
        self._assumption_registry = AssumptionRegistry()
        self._cache: dict[str, ProblemCompilationResult] = {}

    async def execute(
        self,
        task: TaskEnvelope,
        *,
        force_recompute: bool = False,
    ) -> ProblemCompilationResult:
        """Compile one accepted task, returning a cached result for the same idempotency key."""

        self._require_accepted_task(task)
        input_hash = _sha256(task.model_dump_json())
        idempotency_key = _sha256(
            f"{task.task_id}:M01:{self._contract_version}:{input_hash}:{self._producer_version}"
        )
        if not force_recompute and idempotency_key in self._cache:
            return self._cache[idempotency_key]

        candidates, used_fallback, warnings, model_invocations = await self._extract_candidates(
            task.research_goal
        )
        findings, questions = self._ambiguity_detector.detect(task.research_goal, candidates)
        assumptions = self._assumption_registry.build(task.research_goal, candidates)
        created_at = self._clock()
        problem_id = _stable_id("prb", f"{task.task_id}:{input_hash}", size=32)
        candidate_spans = _all_evidence(candidates)
        assumption_spans = tuple(span for item in assumptions for span in item.evidence)
        source_spans = _unique_spans(candidate_spans, assumption_spans)
        spec = ScientificProblemSpec(
            task_id=task.task_id,
            run_id=task.run_id,
            contract_version=self._contract_version,
            created_at=created_at,
            producer_version=self._producer_version,
            problem_id=problem_id,
            raw_text=task.research_goal,
            research_goal=task.research_goal,
            research_questions=tuple(unit.question for unit in candidates.problem_units),
            problem_units=candidates.problem_units,
            target_entities=candidates.entities,
            target_variables=candidates.variables,
            conditions=candidates.conditions,
            temporal_scope=candidates.temporal_scope,
            spatial_scope=candidates.spatial_scope,
            output_preferences=candidates.output_preferences,
            assumptions=assumptions,
            source_spans=source_spans,
        )
        status = (
            CompilationStatus.NEEDS_REVIEW
            if any(item.blocking for item in findings)
            else CompilationStatus.SUCCEEDED
        )
        ambiguity_report = AmbiguityReport(
            task_id=task.task_id,
            run_id=task.run_id,
            contract_version=self._contract_version,
            created_at=created_at,
            producer_version=self._producer_version,
            problem_id=problem_id,
            requires_clarification=status is CompilationStatus.NEEDS_REVIEW,
            ambiguities=findings,
            questions=questions,
        )
        assumption_register = AssumptionRegister(
            task_id=task.task_id,
            run_id=task.run_id,
            contract_version=self._contract_version,
            created_at=created_at,
            producer_version=self._producer_version,
            problem_id=problem_id,
            assumptions=assumptions,
        )
        output_hash = _sha256(spec.model_dump_json())
        payload = ProblemCompiledPayload(
            problem_id=problem_id,
            status=status,
            input_hash=input_hash,
            output_hash=output_hash,
            idempotency_key=idempotency_key,
            used_fallback=used_fallback,
        )
        event = EventEnvelope[ProblemCompiledPayload](
            event_type=EventType.PROBLEM_COMPILED,
            task_id=task.task_id,
            run_id=task.run_id,
            occurred_at=created_at,
            producer=ProducerRef(component="problem-compiler", version=self._producer_version),
            payload=payload,
        )
        metrics = (
            ("problem_units", float(len(candidates.problem_units))),
            ("entities", float(len(candidates.entities))),
            ("variables", float(len(candidates.variables))),
            ("conditions", float(len(candidates.conditions))),
            ("blocking_ambiguities", float(sum(item.blocking for item in findings))),
            ("clarification_questions", float(len(questions))),
            ("assumptions", float(len(assumptions))),
        )
        result = ProblemCompilationResult(
            task_id=task.task_id,
            run_id=task.run_id,
            contract_version=self._contract_version,
            created_at=created_at,
            producer_version=self._producer_version,
            status=status,
            problem_spec=spec,
            ambiguity_report=ambiguity_report,
            assumption_register=assumption_register,
            event=event,
            model_invocations=model_invocations,
            used_fallback=used_fallback,
            warnings=warnings,
            metrics=metrics,
        )
        self._cache[idempotency_key] = result
        return result

    async def compile(
        self,
        task: TaskEnvelope,
        *,
        force_recompute: bool = False,
    ) -> ProblemCompilationResult:
        """Alias for :meth:`execute` used by direct M01 consumers."""

        return await self.execute(task, force_recompute=force_recompute)

    async def _extract_candidates(
        self,
        text: str,
    ) -> tuple[
        CandidateBatch,
        bool,
        tuple[str, ...],
        tuple[ModelInvocationRecord, ...],
    ]:
        if self._extractor is None:
            raw = await self._fallback.extract(text)
            return self._validator.validate(raw, text, mark_external=False), False, (), ()
        try:
            raw = await self._extractor.extract(text)
            return (
                self._validator.validate(raw, text),
                False,
                (),
                self._model_invocations(),
            )
        except (AppError, TypeError, ValueError, ValidationError, json.JSONDecodeError):
            raw = await self._fallback.extract(text)
            candidates = self._validator.validate(raw, text, mark_external=False)
            return (
                candidates,
                True,
                ("M01_EXTERNAL_CANDIDATE_REJECTED",),
                self._model_invocations(),
            )

    def _model_invocations(self) -> tuple[ModelInvocationRecord, ...]:
        if isinstance(self._extractor, AuditedCandidateExtractor):
            return self._extractor.invocations
        return ()

    @staticmethod
    def _require_accepted_task(task: TaskEnvelope) -> None:
        if not isinstance(task, TaskEnvelope):
            raise ProblemCompilerInputError(
                "M01_INVALID_TASK_CONTRACT",
                "ProblemCompilerAgent accepts only a validated TaskEnvelope instance.",
            )
        if task.accepted is not True or task.security_decision.outcome != "accepted":
            raise ProblemCompilerInputError(
                "M01_TASK_NOT_ACCEPTED",
                "ProblemCompilerAgent accepts only a task approved by M00 security preflight.",
            )

"""Deterministic M09 quality gates over validated document IR candidates."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from itertools import pairwise

from scidatafusion.contracts.documents import (
    ByteSpanSourceAnchor,
    DocumentCandidateId,
    DocumentIR,
    DocumentPageKind,
    DocumentQualityCheckResult,
)
from scidatafusion.contracts.parsing import (
    ParserRoute,
    QualityCheckKind,
    QualityCheckSpec,
)
from scidatafusion.documents.integrity import (
    calculate_document_quality_result_hash,
    calculate_document_quality_result_id,
    verify_document_ir_integrity,
)
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode

_ZERO_HASH = "0" * 64
_ZERO_QUALITY_ID = "dqr_" + "0" * 16
_ALGORITHM_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class _QualityAlgorithm:
    algorithm_id: str
    scorer: Callable[[DocumentIR], float]

    @property
    def algorithm_hash(self) -> str:
        return canonical_hash(
            {
                "algorithm_id": self.algorithm_id,
                "algorithm_version": _ALGORITHM_VERSION,
            }
        )


_ALGORITHMS = {
    QualityCheckKind.OUTPUT_SCHEMA: _QualityAlgorithm(
        algorithm_id="document.schema-integrity",
        scorer=lambda _document: 1.0,
    ),
    QualityCheckKind.TEXT_COVERAGE: _QualityAlgorithm(
        algorithm_id="document.page-text-coverage",
        scorer=lambda document: _text_coverage_score(document),
    ),
    QualityCheckKind.READING_ORDER: _QualityAlgorithm(
        algorithm_id="document.reading-order-structure",
        scorer=lambda document: _reading_order_score(document),
    ),
}


def evaluate_document_quality(
    document: DocumentIR,
    route: ParserRoute,
    *,
    candidate_id: DocumentCandidateId,
) -> tuple[DocumentQualityCheckResult, ...]:
    """Evaluate exactly the quality checks declared by one immutable M08 route.

    These scores establish structural validity and deterministic coverage only. They do not
    claim benchmark-backed content fidelity, semantic reading-order accuracy, or heading F1.
    """

    if (
        document.object_id != route.object_id
        or document.route_id != route.route_id
        or document.route_hash != route.route_hash
        or document.scope != route.scope
    ):
        raise AppError(
            ErrorCode.ARTIFACT_INTEGRITY_ERROR,
            "M09 quality evaluation requires the exact document route",
        )
    verify_document_ir_integrity(document)
    return tuple(
        _evaluate_check(
            document,
            check,
            route=route,
            candidate_id=candidate_id,
        )
        for check in route.quality_checks
    )


def failed_quality_check_ids(
    results: tuple[DocumentQualityCheckResult, ...],
) -> frozenset[str]:
    """Return failed M08 check IDs for exact escalation-rule matching."""

    return frozenset(item.check_id for item in results if not item.passed)


def _evaluate_check(
    document: DocumentIR,
    check: QualityCheckSpec,
    *,
    route: ParserRoute,
    candidate_id: DocumentCandidateId,
) -> DocumentQualityCheckResult:
    algorithm = _ALGORITHMS.get(check.kind)
    if algorithm is None:
        raise AppError(
            ErrorCode.QUALITY_GATE_FAILED,
            "M09 received a quality check owned by another parser module",
        )
    observed_score = _bounded_score(algorithm.scorer(document))
    draft = DocumentQualityCheckResult(
        quality_result_id=_ZERO_QUALITY_ID,
        route_id=route.route_id,
        parser_attempt_id=document.parser_attempt_id,
        candidate_id=candidate_id,
        check_id=check.check_id,
        kind=check.kind,
        minimum_score=check.minimum_score,
        observed_score=observed_score,
        passed=observed_score >= check.minimum_score,
        algorithm_id=algorithm.algorithm_id,
        algorithm_version=_ALGORITHM_VERSION,
        algorithm_hash=algorithm.algorithm_hash,
        input_document_hash=document.document_hash,
        measured_page_count=document.page_count,
        measured_block_count=document.block_count,
        result_hash=_ZERO_HASH,
    )
    result_hash = calculate_document_quality_result_hash(draft)
    result_id = calculate_document_quality_result_id(draft)
    return DocumentQualityCheckResult.model_validate(
        draft.model_copy(
            update={
                "quality_result_id": result_id,
                "result_hash": result_hash,
            }
        ).model_dump()
    )


def _text_coverage_score(document: DocumentIR) -> float:
    covered_pages = sum(
        any(block.verbatim_text.strip() for block in page.blocks) for page in document.pages
    )
    return covered_pages / document.page_count


def _reading_order_score(document: DocumentIR) -> float:
    page_scores = tuple(
        _page_reading_order_score(document, index) for index in range(len(document.pages))
    )
    return sum(page_scores) / len(page_scores)


def _page_reading_order_score(document: DocumentIR, page_index: int) -> float:
    page = document.pages[page_index]
    if not page.blocks:
        return 0.0
    if page.page_kind is DocumentPageKind.FIXED:
        # Parser order is observable, while layout-order accuracy needs a labeled benchmark.
        return 1.0
    starts: list[int] = []
    for block in page.blocks:
        byte_anchors = tuple(
            anchor for anchor in block.anchors if isinstance(anchor, ByteSpanSourceAnchor)
        )
        if not byte_anchors:
            return 0.0
        starts.append(min(anchor.start_byte for anchor in byte_anchors))
    if len(starts) == 1:
        return 1.0
    ordered_pairs = sum(right > left for left, right in pairwise(starts))
    return ordered_pairs / (len(starts) - 1)


def _bounded_score(value: float) -> float:
    if not 0.0 <= value <= 1.0:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "M09 quality algorithm produced an out-of-range score",
        )
    return round(value, 6)

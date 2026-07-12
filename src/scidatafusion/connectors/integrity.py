"""Canonical M05 artifact hashes and downstream integrity verification."""

from __future__ import annotations

import hmac
from collections.abc import Sequence
from datetime import datetime

from scidatafusion.contracts.connectors import (
    ConnectorBatchStatus,
    ConnectorExecutionMetrics,
    ConnectorExecutionResult,
    ConnectorPageReference,
    ConnectorQueryRun,
    SearchEvidence,
    SourceCandidate,
)
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode


def calculate_source_candidate_hash(candidate: SourceCandidate) -> str:
    """Hash one normalized candidate without its self-referential hash field."""

    return canonical_hash(candidate.model_dump(mode="json", exclude={"candidate_hash"}))


def calculate_candidate_set_hash(
    candidates: Sequence[SourceCandidate],
    *,
    search_plan_hash: str,
    connector_registry_hash: str,
) -> str:
    """Hash the ordered candidates and exact upstream snapshots."""

    return canonical_hash(
        {
            "candidates": [item.model_dump(mode="json") for item in candidates],
            "connector_registry_hash": connector_registry_hash,
            "search_plan_hash": search_plan_hash,
        }
    )


def calculate_evidence_set_hash(
    evidence: Sequence[SearchEvidence],
    pages: Sequence[ConnectorPageReference],
    *,
    search_plan_hash: str,
    connector_registry_hash: str,
) -> str:
    """Hash evidence together with every retained raw response page."""

    return canonical_hash(
        {
            "connector_registry_hash": connector_registry_hash,
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "pages": [item.model_dump(mode="json") for item in pages],
            "search_plan_hash": search_plan_hash,
        }
    )


def calculate_run_log_hash(
    query_runs: Sequence[ConnectorQueryRun],
    *,
    search_plan_hash: str,
    connector_registry_hash: str,
) -> str:
    """Hash the ordered Connector query runs and their immutable references."""

    return canonical_hash(
        {
            "connector_registry_hash": connector_registry_hash,
            "query_runs": [item.model_dump(mode="json") for item in query_runs],
            "search_plan_hash": search_plan_hash,
        }
    )


def calculate_connector_output_hash(
    *,
    task_id: str,
    run_id: str,
    contract_version: str,
    created_at: datetime,
    producer_version: str,
    input_hash: str,
    idempotency_key: str,
    status: ConnectorBatchStatus,
    candidate_set_hash: str,
    evidence_set_hash: str,
    run_log_hash: str,
    metrics: ConnectorExecutionMetrics,
    warnings: Sequence[str],
) -> str:
    """Hash the complete semantic M05 output summary."""

    return canonical_hash(
        {
            "candidate_set_hash": candidate_set_hash,
            "contract_version": contract_version,
            "created_at": created_at.isoformat(),
            "evidence_set_hash": evidence_set_hash,
            "idempotency_key": idempotency_key,
            "input_hash": input_hash,
            "metrics": metrics.model_dump(mode="json"),
            "producer_version": producer_version,
            "run_log_hash": run_log_hash,
            "run_id": run_id,
            "status": status.value,
            "task_id": task_id,
            "warnings": list(warnings),
        }
    )


def verify_connector_execution_integrity(result: ConnectorExecutionResult) -> None:
    """Reject an M05 result whose semantic content no longer matches its hashes."""

    candidate_hashes_valid = all(
        hmac.compare_digest(
            candidate.candidate_hash,
            calculate_source_candidate_hash(candidate),
        )
        for candidate in result.candidate_set.candidates
    )
    candidate_set_hash = calculate_candidate_set_hash(
        result.candidate_set.candidates,
        search_plan_hash=result.candidate_set.search_plan_hash,
        connector_registry_hash=result.candidate_set.connector_registry_hash,
    )
    evidence_set_hash = calculate_evidence_set_hash(
        result.evidence_set.evidence,
        result.evidence_set.pages,
        search_plan_hash=result.evidence_set.search_plan_hash,
        connector_registry_hash=result.evidence_set.connector_registry_hash,
    )
    run_log_hash = calculate_run_log_hash(
        result.run_log.query_runs,
        search_plan_hash=result.run_log.search_plan_hash,
        connector_registry_hash=result.run_log.connector_registry_hash,
    )
    output_hash = calculate_connector_output_hash(
        task_id=result.task_id,
        run_id=result.run_id,
        contract_version=result.contract_version,
        created_at=result.created_at,
        producer_version=result.producer_version,
        input_hash=result.input_hash,
        idempotency_key=result.idempotency_key,
        status=result.status,
        candidate_set_hash=result.candidate_set.candidate_set_hash,
        evidence_set_hash=result.evidence_set.evidence_set_hash,
        run_log_hash=result.run_log.run_log_hash,
        metrics=result.metrics,
        warnings=result.warnings,
    )
    if not (
        candidate_hashes_valid
        and hmac.compare_digest(result.candidate_set.candidate_set_hash, candidate_set_hash)
        and hmac.compare_digest(result.evidence_set.evidence_set_hash, evidence_set_hash)
        and hmac.compare_digest(result.run_log.run_log_hash, run_log_hash)
        and hmac.compare_digest(result.output_hash, output_hash)
    ):
        raise AppError(
            ErrorCode.ARTIFACT_INTEGRITY_ERROR,
            "M05 result content does not match its immutable hashes",
        )

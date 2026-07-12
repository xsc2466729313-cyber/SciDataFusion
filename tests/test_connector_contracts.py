from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import NamedTuple

import pytest
from pydantic import ValidationError

from scidatafusion.cli import _build_search_planning, _execute_offline_connectors
from scidatafusion.connectors.fixtures import build_offline_ia_connector_bundle
from scidatafusion.connectors.registry import load_default_connector_registry
from scidatafusion.contracts.base import StrictContract
from scidatafusion.contracts.connectors import (
    AttemptStatus,
    AuthKind,
    ConnectorBatchStatus,
    ConnectorErrorCode,
    ConnectorExecutionPolicy,
    ConnectorExecutionResult,
    ConnectorPage,
    ConnectorRegistry,
    ExecutionMode,
    QueryRunStatus,
)

_GOAL = "Study Type Ia supernova light curves using multi-source integration into CSV."


class _Artifacts(NamedTuple):
    result: ConnectorExecutionResult
    page: ConnectorPage


@pytest.fixture(scope="module")
def artifacts() -> _Artifacts:
    _, planning = _build_search_planning(_GOAL, "m05-contract-reviewer")
    assert planning is not None
    result = asyncio.run(_execute_offline_connectors(planning))

    async def fetch_page() -> ConnectorPage:
        registry = load_default_connector_registry()
        bundle = build_offline_ia_connector_bundle(registry)
        try:
            query = next(
                query
                for family in planning.plan.query_family_set.families
                for query in family.queries
                if query.source_id == "openalex_literature" and query.language == "en"
            )
            runtime = next(
                item
                for item in bundle.runtime_snapshot.entries
                if item.source_id == query.source_id
            )
            outcome = await bundle.connectors[query.source_id].execute(
                query, runtime, ConnectorExecutionPolicy()
            )
            return outcome.pages[0]
        finally:
            await bundle.aclose()

    return _Artifacts(result=result, page=asyncio.run(fetch_page()))


def _reject(model: StrictContract, updates: dict[str, object], match: str) -> None:
    payload = model.model_dump(mode="python")
    payload.update(updates)
    model_type: type[StrictContract] = type(model)
    with pytest.raises(ValidationError, match=match):
        model_type.model_validate(payload)


def test_connector_descriptor_and_registry_fail_closed() -> None:
    registry = load_default_connector_registry()
    descriptor = registry.connectors[0]

    _reject(descriptor, {"endpoint": descriptor.endpoint.replace("https://", "http://")}, "HTTPS")
    _reject(descriptor, {"endpoint": f"{descriptor.endpoint}?target=evil"}, "query")
    _reject(
        descriptor,
        {"endpoint": descriptor.endpoint.replace(".fr/", ".fr:444/")},
        "ports",
    )
    _reject(
        descriptor,
        {"endpoint": "https://127.0.0.1/TAP", "allowed_hosts": ("127.0.0.1",)},
        "public host",
    )
    _reject(descriptor, {"allowed_hosts": descriptor.allowed_hosts * 2}, "unique")
    _reject(descriptor, {"allowed_hosts": ("example.org",)}, "allowlisted")

    openalex = registry.connectors[1]
    _reject(
        openalex,
        {"auth_kind": AuthKind.NONE, "api_key_parameter": None},
        "credential environment",
    )
    _reject(openalex, {"auth_kind": AuthKind.BEARER}, "API-key")
    _reject(
        registry,
        {"connectors": (descriptor, descriptor)},
        "connector ids",
    )
    duplicate_source = registry.connectors[1].model_copy(update={"source_id": descriptor.source_id})
    _reject(
        registry,
        {"connectors": (descriptor, duplicate_source)},
        "source ids",
    )


def test_runtime_policy_record_and_page_invariants(artifacts: _Artifacts) -> None:
    result = artifacts.result
    registry = load_default_connector_registry()
    bundle = build_offline_ia_connector_bundle(registry)
    try:
        runtime = bundle.runtime_snapshot.entries[0]
        _reject(
            runtime,
            {"checked_at": datetime(2026, 7, 12)},  # noqa: DTZ001 - invalid fixture
            "timezone",
        )
        _reject(
            bundle.runtime_snapshot,
            {"entries": (runtime, runtime)},
            "connector ids",
        )
    finally:
        asyncio.run(bundle.aclose())
    with pytest.raises(ValidationError, match="backoff"):
        ConnectorExecutionPolicy(base_backoff_seconds=5, max_backoff_seconds=4)
    _reject(
        result.candidate_set,
        {"created_at": datetime(2026, 7, 12)},  # noqa: DTZ001 - invalid fixture
        "timezone",
    )

    page = artifacts.page
    record = page.records[0]
    _reject(record, {"landing_url": "http://example.org/data"}, "HTTPS")
    _reject(record, {"landing_url": "https://example.org:8443/data"}, "custom ports")
    _reject(record, {"landing_url": "https://example.org:bad/data"}, "valid HTTPS port")
    _reject(record, {"file_formats": ("csv", "csv")}, "unique")
    _reject(page, {"raw_response_hash": "a" * 64}, "artifact hash")
    _reject(page, {"response_bytes": page.response_bytes + 1}, "artifact size")
    _reject(page, {"media_type": "text/plain"}, "media type")
    _reject(
        page,
        {"execution_mode": ExecutionMode.LIVE_NETWORK, "network_performed": False},
        "live-network",
    )
    _reject(page, {"records": (record, record)}, "record hashes")


def test_evidence_candidate_and_set_invariants(artifacts: _Artifacts) -> None:
    result = artifacts.result
    evidence = result.evidence_set.evidence[0]
    _reject(
        evidence,
        {"retrieved_at": datetime(2026, 7, 12)},  # noqa: DTZ001 - invalid fixture
        "timezone",
    )

    candidate = next(item for item in result.candidate_set.candidates if item.conflicts)
    observation = candidate.observations[0]
    _reject(
        observation,
        {"evidence_ids": observation.evidence_ids * 2},
        "evidence ids",
    )
    _reject(
        observation,
        {"retrieved_at": datetime(2026, 7, 12)},  # noqa: DTZ001 - invalid fixture
        "timezone",
    )
    claim = candidate.coverage_claims[0]
    _reject(claim, {"evidence_ids": claim.evidence_ids * 2}, "evidence ids")

    assessment = candidate.assessment
    _reject(
        assessment,
        {"components": (assessment.components[0], assessment.components[0])},
        "component names",
    )
    changed_weight = assessment.components[0].model_copy(update={"weight": 0.9})
    _reject(
        assessment,
        {"components": (changed_weight, *assessment.components[1:])},
        "sum to one",
    )
    _reject(assessment, {"total_score": 0.123456}, "weighted component")

    conflict = candidate.conflicts[0]
    _reject(conflict, {"values": (conflict.values[0],) * 2}, "values")
    _reject(
        conflict,
        {"evidence_ids": (conflict.evidence_ids[0],) * 2},
        "evidence ids",
    )
    _reject(candidate, {"identifiers": candidate.identifiers * 2}, "identifiers")
    _reject(candidate, {"observations": candidate.observations * 2}, "observations")
    _reject(candidate, {"source_ids": ("unrelated_source",)}, "declared sources")
    invalid_claim = claim.model_copy(update={"evidence_ids": ("sev_aaaaaaaaaaaaaaaa",)})
    _reject(candidate, {"coverage_claims": (invalid_claim,)}, "observation evidence")
    _reject(candidate, {"conflicts": (conflict, conflict)}, "conflict ids")

    _reject(
        result.candidate_set,
        {"candidates": (candidate, candidate)},
        "candidate ids",
    )
    _reject(
        result.evidence_set,
        {"evidence": (evidence, evidence)},
        "evidence ids",
    )


def test_attempt_query_run_and_result_links_are_derived(artifacts: _Artifacts) -> None:
    result = artifacts.result
    run = result.run_log.query_runs[0]
    attempt = run.attempts[0]

    _reject(
        attempt,
        {"finished_at": attempt.started_at - timedelta(seconds=1)},
        "finish before",
    )
    _reject(attempt, {"network_performed": True}, "live-network")
    _reject(attempt, {"cache_hit": True}, "cache-hit")
    _reject(attempt, {"error_code": ConnectorErrorCode.TIMEOUT}, "errors")
    _reject(attempt, {"retryable": True}, "retryable")
    _reject(attempt, {"raw_response_hash": None}, "response hash")

    mismatched_attempt = attempt.model_copy(update={"query_id": "qry_aaaaaaaaaaaaaaaa"})
    _reject(run, {"attempts": (mismatched_attempt,)}, "refer to their query")
    _reject(run, {"attempts": (attempt, attempt)}, "attempt ids")
    _reject(run, {"retry_count": run.retry_count + 1}, "retry count")
    _reject(run, {"cache_hit": not run.cache_hit}, "cache flag")
    _reject(
        run,
        {"error_code": ConnectorErrorCode.TIMEOUT},
        "successful query runs",
    )

    _reject(
        result.run_log,
        {"query_runs": (run, run)},
        "run ids",
    )
    _reject(
        result,
        {
            "candidate_set": result.candidate_set.model_copy(
                update={"task_id": "tsk_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
            )
        },
        "share result metadata",
    )
    _reject(
        result,
        {"evidence_set": result.evidence_set.model_copy(update={"search_plan_hash": "a" * 64})},
        "share plan",
    )
    _reject(
        result,
        {
            "metrics": result.metrics.model_copy(
                update={"candidate_count": result.metrics.candidate_count + 1}
            )
        },
        "metrics",
    )
    _reject(
        result,
        {
            "event": result.event.model_copy(
                update={
                    "payload": result.event.payload.model_copy(
                        update={"status": ConnectorBatchStatus.PARTIAL}
                    )
                }
            )
        },
        "event",
    )
    assert run.status in {QueryRunStatus.SUCCEEDED, QueryRunStatus.CACHED}
    assert attempt.status in {AttemptStatus.SUCCEEDED, AttemptStatus.CACHE_HIT}
    assert result.status is ConnectorBatchStatus.SUCCEEDED


def test_connector_registry_contract_rejects_empty_payload() -> None:
    with pytest.raises(ValidationError):
        ConnectorRegistry(
            registry_version="1.0.0",
            content_hash="a" * 64,
            connectors=(),
        )

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple, cast

import pytest
from pydantic import ValidationError

from scidatafusion.contracts.routing import RoutingDecision
from scidatafusion.contracts.scientific import ContractStatus, ScientificDataContract
from scidatafusion.contracts.search import (
    CoverageState,
    QueryDialect,
    QueryFamilyState,
    SearchCapabilityMode,
    SearchGap,
    SearchGapCode,
    SearchHistorySnapshot,
    SearchPlanningRequest,
    SearchPlanningStatus,
    SearchProgressSnapshot,
    SearchStopDecision,
    SearchStopOutcome,
    SearchStopPolicySpec,
    SearchStopReason,
    SourceCapabilityRegistry,
    SourceCategory,
    SourceProtocol,
)
from scidatafusion.contracts.task import BudgetPolicy, TaskIntakeRequest
from scidatafusion.domain.registry import (
    RegistryErrorCode,
    RegistryLoadError,
    canonical_hash,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.search import (
    SearchPlanner,
    SearchStopPolicy,
    SourceCapabilityRegistryLoader,
    calculate_search_plan_hash,
    deduplicate_queries,
    normalize_query,
    verify_search_plan_integrity,
)
from scidatafusion.workflow import build_offline_demo_workflow

_IA_GOAL = "Study Type Ia supernova light curves using multi-source data integration into CSV."
_CREATED_AT = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)


class _Phase1Bundle(NamedTuple):
    contract: ScientificDataContract
    routing: RoutingDecision
    budget_policy: BudgetPolicy


def _confirmed_phase1(goal: str = _IA_GOAL) -> _Phase1Bundle:
    workflow = build_offline_demo_workflow()
    draft = asyncio.run(
        workflow.execute(TaskIntakeRequest(research_goal=goal, allow_external_models=False))
    )
    assert draft.compilation is not None
    assert draft.routing is not None
    assert draft.intake.envelope is not None
    confirmed = workflow.confirm(
        contract_id=draft.compilation.contract.contract_id,
        expected_contract_hash=draft.compilation.contract.contract_hash,
        confirmed_by="authenticated-search-reviewer",
    )
    assert confirmed.confirmation is not None
    return _Phase1Bundle(
        contract=confirmed.confirmation.contract,
        routing=draft.routing,
        budget_policy=draft.intake.envelope.budget_policy,
    )


@pytest.fixture(scope="module")
def ia_bundle() -> _Phase1Bundle:
    return _confirmed_phase1()


@pytest.fixture(scope="module")
def registry() -> SourceCapabilityRegistry:
    return SourceCapabilityRegistryLoader.load_default()


def _source_ids(registry: SourceCapabilityRegistry) -> tuple[str, ...]:
    return tuple(item.source_id for item in registry.capabilities)


def _request(
    bundle: _Phase1Bundle,
    *,
    budget_policy: BudgetPolicy | None = None,
    capability_mode: SearchCapabilityMode = SearchCapabilityMode.SIMULATED_DEMO,
) -> SearchPlanningRequest:
    return SearchPlanningRequest(
        contract=bundle.contract,
        routing=bundle.routing,
        budget_policy=budget_policy or bundle.budget_policy,
        capability_mode=capability_mode,
    )


def test_confirmed_ia_search_plan_is_executable_multisource_and_evidence_grounded(
    ia_bundle: _Phase1Bundle,
    registry: SourceCapabilityRegistry,
) -> None:
    planner = SearchPlanner(
        registry=registry,
        available_source_ids=_source_ids(registry),
        clock=lambda: _CREATED_AT,
    )

    result = planner.plan(_request(ia_bundle))

    assert result.status is SearchPlanningStatus.SUCCEEDED
    assert result.plan.capability_mode is SearchCapabilityMode.SIMULATED_DEMO
    assert result.plan.contract_id == ia_bundle.contract.contract_id
    assert result.plan.contract_hash == ia_bundle.contract.contract_hash
    assert result.plan.routing_ref == ia_bundle.routing.decision_hash
    assert result.plan.capability_registry_hash == registry.content_hash
    assert result.event.event_type.value == "search.plan.created"
    assert result.event.payload.output_hash == result.output_hash
    assert result.plan.plan_id == f"spl_{result.plan.plan_hash[:32]}"

    families = result.plan.query_family_set.families
    active = tuple(item for item in families if item.state is QueryFamilyState.ACTIVE)
    queries = tuple(query for family in active for query in family.queries)
    assert {item.category for item in active} == set(SourceCategory)
    assert {item.source_id for item in active} == set(_source_ids(registry))
    assert queries
    assert all(query.target_fields and query.rationale for query in queries)
    assert all(query.round_number == 1 for query in queries)
    assert all(
        query.query_id
        in {
            query_id
            for allocation in result.plan.budget_allocation.source_allocations
            for query_id in allocation.query_ids
        }
        for query in queries
    )
    capabilities = {item.source_id: item for item in registry.capabilities}
    assert all(
        any(
            operation.operation_id == query.operation_id and operation.dialect is query.dialect
            for operation in capabilities[query.source_id].operations
        )
        and capabilities[query.source_id].protocol is query.protocol
        and query.category is capabilities[query.source_id].category
        for query in queries
    )

    vizier = next(
        item for item in registry.capabilities if item.protocol is SourceProtocol.TAP_ADQL
    )
    vizier_query = next(query for query in queries if query.source_id == vizier.source_id)
    assert vizier_query.dialect is QueryDialect.TAP_ADQL_DISCOVERY
    assert vizier_query.protocol is SourceProtocol.TAP_ADQL
    assert "type ia" in vizier_query.normalized_query or "sn ia" in vizier_query.normalized_query

    assert [(item.kind.value, item.term) for item in ia_bundle.contract.research_concepts] == [
        ("entity", "Type Ia supernova"),
        ("variable", "light curves"),
    ]
    assert all(item.evidence_refs for item in ia_bundle.contract.research_concepts)


def test_coverage_matrix_is_the_contract_field_by_source_preference_product(
    ia_bundle: _Phase1Bundle,
    registry: SourceCapabilityRegistry,
) -> None:
    result = SearchPlanner(
        registry=registry,
        available_source_ids=_source_ids(registry),
        clock=lambda: _CREATED_AT,
    ).plan(_request(ia_bundle))

    expected = {
        (field.name, source_type)
        for field in ia_bundle.contract.fields
        for source_type in field.source_preference
    }
    cells = result.plan.coverage_matrix.cells

    assert {(item.field_name, item.contract_source_type) for item in cells} == expected
    assert all(item.state is CoverageState.PLANNED for item in cells)
    assert all(item.source_ids and item.planned_query_ids for item in cells)
    assert all(item.observed_candidate_count == 0 for item in cells)
    assert {item.gate_id for item in result.plan.coverage_matrix.gate_targets} == {
        item.gate_id for item in ia_bundle.contract.quality_gates
    }


def test_planner_rejects_unconfirmed_contract_and_broken_upstream_linkage(
    ia_bundle: _Phase1Bundle,
    registry: SourceCapabilityRegistry,
) -> None:
    unconfirmed = ScientificDataContract.model_validate(
        {
            **ia_bundle.contract.model_dump(),
            "status": ContractStatus.DRAFT,
            "confirmed_at": None,
            "confirmed_by": None,
        }
    )
    planner = SearchPlanner(
        registry=registry,
        available_source_ids=_source_ids(registry),
        clock=lambda: _CREATED_AT,
    )

    with pytest.raises(AppError) as not_confirmed:
        planner.plan(
            SearchPlanningRequest(
                contract=unconfirmed,
                routing=ia_bundle.routing,
                budget_policy=ia_bundle.budget_policy,
            )
        )
    assert not_confirmed.value.code is ErrorCode.QUALITY_GATE_FAILED

    other = _confirmed_phase1("Integrate Type Ia supernova light curves from papers into CSV.")
    with pytest.raises(AppError) as broken_chain:
        planner.plan(
            SearchPlanningRequest(
                contract=ia_bundle.contract,
                routing=other.routing,
                budget_policy=ia_bundle.budget_policy,
            )
        )
    assert broken_chain.value.code is ErrorCode.VALIDATION_FAILED


def test_default_runtime_capabilities_fail_closed(
    ia_bundle: _Phase1Bundle,
    registry: SourceCapabilityRegistry,
) -> None:
    result = SearchPlanner(registry=registry, clock=lambda: _CREATED_AT).plan(
        _request(ia_bundle, capability_mode=SearchCapabilityMode.RUNTIME)
    )

    assert result.status is SearchPlanningStatus.UNSUPPORTED
    assert result.plan.status is SearchPlanningStatus.UNSUPPORTED
    assert result.metrics.query_count == 0
    assert result.plan.budget_allocation.allocated_query_count == 0
    assert all(
        family.state is QueryFamilyState.CAPABILITY_UNAVAILABLE
        for family in result.plan.query_family_set.families
    )
    assert result.plan.gaps
    assert all(item.code is SearchGapCode.CAPABILITY_UNAVAILABLE for item in result.plan.gaps)


def test_missing_vizier_is_a_visible_partial_capability_gap(
    ia_bundle: _Phase1Bundle,
    registry: SourceCapabilityRegistry,
) -> None:
    vizier = next(
        item for item in registry.capabilities if item.protocol is SourceProtocol.TAP_ADQL
    )
    available = tuple(
        item.source_id for item in registry.capabilities if item.source_id != vizier.source_id
    )

    result = SearchPlanner(
        registry=registry,
        available_source_ids=available,
        clock=lambda: _CREATED_AT,
    ).plan(_request(ia_bundle))

    assert result.status is SearchPlanningStatus.PARTIAL
    gap = next(item for item in result.plan.gaps if item.source_id == vizier.source_id)
    assert gap.code is SearchGapCode.CAPABILITY_UNAVAILABLE
    assert gap.category is SourceCategory.DOMAIN_DATABASE
    assert gap.blocking is True
    family = next(
        item for item in result.plan.query_family_set.families if item.source_id == vizier.source_id
    )
    assert family.state is QueryFamilyState.CAPABILITY_UNAVAILABLE
    assert family.queries == ()


def test_tight_duration_budget_defers_queries_without_overspending(
    ia_bundle: _Phase1Bundle,
    registry: SourceCapabilityRegistry,
) -> None:
    smallest_query_duration = min(
        item.estimated_query_duration_seconds for item in registry.capabilities
    )
    policy_payload = ia_bundle.budget_policy.model_dump()
    allocation = cast(dict[str, Any], policy_payload["allocation"])
    allocation["max_duration_seconds"] = smallest_query_duration
    limited_policy = BudgetPolicy.model_validate(policy_payload)

    result = SearchPlanner(
        registry=registry,
        available_source_ids=_source_ids(registry),
        clock=lambda: _CREATED_AT,
    ).plan(_request(ia_bundle, budget_policy=limited_policy))

    budget = result.plan.budget_allocation
    assert result.status is SearchPlanningStatus.PARTIAL
    assert budget.allocated_duration_seconds <= budget.available_duration_seconds
    assert budget.deferred_query_ids
    assert any(item.code is SearchGapCode.BUDGET_DEFERRED for item in result.plan.gaps)
    assert any(
        item.state is QueryFamilyState.BUDGET_DEFERRED
        for item in result.plan.query_family_set.families
    )
    active_query_ids = {
        query.query_id
        for family in result.plan.query_family_set.families
        for query in family.queries
    }
    assert active_query_ids.isdisjoint(budget.deferred_query_ids)


def test_query_normalization_and_deduplication_are_unicode_stable() -> None:
    assert (
        normalize_query("  \uff34\uff59\uff50\uff45\u3000\uff29\uff41\nSUPERNOVA  ")
        == "type ia supernova"
    )
    assert deduplicate_queries(
        (
            "Type Ia supernova",
            "\uff34\uff59\uff50\uff45\u3000\uff29\uff41\u3000supernova",
            "  type   ia supernova ",
            "SN Ia light curve",
        )
    ) == ("Type Ia supernova", "SN Ia light curve")


@pytest.mark.parametrize(
    ("updates", "reason", "outcome"),
    (
        (
            {"cancelled": True, "critical_gap_count": 1},
            SearchStopReason.CANCELLED,
            SearchStopOutcome.PARTIAL,
        ),
        (
            {"consumed_cost_micro_usd": 1_000, "critical_gap_count": 1},
            SearchStopReason.COST_LIMIT,
            SearchStopOutcome.PARTIAL,
        ),
        (
            {"elapsed_seconds": 100, "critical_gap_count": 1},
            SearchStopReason.DURATION_LIMIT,
            SearchStopOutcome.PARTIAL,
        ),
        (
            {"downloaded_bytes": 10_000, "critical_gap_count": 1},
            SearchStopReason.DOWNLOAD_LIMIT,
            SearchStopOutcome.PARTIAL,
        ),
        (
            {"model_tokens": 2_000, "critical_gap_count": 1},
            SearchStopReason.MODEL_USAGE_LIMIT,
            SearchStopOutcome.PARTIAL,
        ),
        (
            {"completed_rounds": 3, "critical_gap_count": 1},
            SearchStopReason.SEARCH_ROUND_LIMIT,
            SearchStopOutcome.PARTIAL,
        ),
        ({}, SearchStopReason.COVERAGE_SATURATED, SearchStopOutcome.SUCCEEDED),
        (
            {"required_field_coverage": 0.94},
            SearchStopReason.CONTINUE_SEARCH,
            SearchStopOutcome.CONTINUE,
        ),
        (
            {"recent_marginal_gains": (0.02, 0.0)},
            SearchStopReason.CONTINUE_SEARCH,
            SearchStopOutcome.CONTINUE,
        ),
        (
            {
                "recent_marginal_gains": (0.01,),
                "recent_new_source_counts": (0,),
            },
            SearchStopReason.CONTINUE_SEARCH,
            SearchStopOutcome.CONTINUE,
        ),
        (
            {"critical_gap_count": 1},
            SearchStopReason.CONTINUE_SEARCH,
            SearchStopOutcome.CONTINUE,
        ),
        (
            {"required_field_coverage": 0.9499},
            SearchStopReason.CONTINUE_SEARCH,
            SearchStopOutcome.CONTINUE,
        ),
        (
            {"source_category_coverage": 0.9999},
            SearchStopReason.CONTINUE_SEARCH,
            SearchStopOutcome.CONTINUE,
        ),
        (
            {"has_primary_source": False},
            SearchStopReason.CONTINUE_SEARCH,
            SearchStopOutcome.CONTINUE,
        ),
    ),
)
def test_stop_policy_has_deterministic_boundary_precedence(
    updates: dict[str, object],
    reason: SearchStopReason,
    outcome: SearchStopOutcome,
) -> None:
    spec = SearchStopPolicySpec(
        max_cost_micro_usd=1_000,
        max_duration_seconds=100,
        max_search_rounds=3,
        max_download_bytes=10_000,
        max_model_tokens=2_000,
    )
    progress_payload: dict[str, object] = {
        "completed_rounds": 2,
        "consumed_cost_micro_usd": 999,
        "elapsed_seconds": 99,
        "downloaded_bytes": 9_999,
        "model_tokens": 1_999,
        "required_field_coverage": 0.95,
        "source_category_coverage": 1.0,
        "has_primary_source": True,
        "critical_gap_count": 0,
        "recent_marginal_gains": (0.01, 0.0),
        "recent_new_source_counts": (1, 0),
    }
    progress_payload.update(updates)

    decision = SearchStopPolicy.evaluate(
        spec,
        SearchProgressSnapshot.model_validate(progress_payload),
    )

    assert decision.reason is reason
    assert decision.outcome is outcome
    assert decision.should_stop is (outcome is not SearchStopOutcome.CONTINUE)


def test_planning_replay_returns_the_same_immutable_result(
    ia_bundle: _Phase1Bundle,
    registry: SourceCapabilityRegistry,
) -> None:
    planner = SearchPlanner(
        registry=registry,
        available_source_ids=_source_ids(registry),
        clock=lambda: _CREATED_AT,
    )
    request = _request(ia_bundle)

    first = planner.plan(request)
    replay = planner.plan(request)

    assert replay is first
    assert replay.idempotency_key == first.idempotency_key
    assert replay.input_hash == first.input_hash
    assert replay.output_hash == first.output_hash
    assert replay.event.event_id == first.event.event_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        concurrent = tuple(executor.map(lambda _: planner.plan(request), range(16)))
    assert all(item is first for item in concurrent)


def test_historical_queries_are_suppressed_with_explicit_gaps(
    ia_bundle: _Phase1Bundle,
    registry: SourceCapabilityRegistry,
) -> None:
    planner = SearchPlanner(
        registry=registry,
        available_source_ids=_source_ids(registry),
        clock=lambda: _CREATED_AT,
    )
    first = planner.plan(_request(ia_bundle))
    normalized = tuple(
        query.normalized_query
        for family in first.plan.query_family_set.families
        for query in family.queries
    )
    request = SearchPlanningRequest(
        contract=ia_bundle.contract,
        routing=ia_bundle.routing,
        budget_policy=ia_bundle.budget_policy,
        capability_mode=SearchCapabilityMode.SIMULATED_DEMO,
        history=SearchHistorySnapshot(completed_rounds=1, normalized_queries=normalized),
    )

    result = planner.plan(request)

    assert result.status is SearchPlanningStatus.UNSUPPORTED
    assert result.metrics.query_count == 0
    assert result.metrics.deduplicated_query_count == len(normalized)
    assert result.plan.gaps
    assert all(item.code is SearchGapCode.QUERY_REPLAY_SUPPRESSED for item in result.plan.gaps)


def test_search_plan_hash_and_upstream_hashes_reject_semantic_tampering(
    ia_bundle: _Phase1Bundle,
    registry: SourceCapabilityRegistry,
) -> None:
    planner = SearchPlanner(
        registry=registry,
        available_source_ids=_source_ids(registry),
        clock=lambda: _CREATED_AT,
    )
    result = planner.plan(_request(ia_bundle))
    plan_payload = result.plan.model_dump()
    families = cast(dict[str, Any], plan_payload["query_family_set"])["families"]
    cast(list[dict[str, Any]], families)[0]["rationale"] = "tampered rationale"
    tampered_plan = type(result.plan).model_validate(plan_payload)
    assert calculate_search_plan_hash(tampered_plan) != result.plan.plan_hash
    with pytest.raises(AppError) as plan_error:
        verify_search_plan_integrity(tampered_plan)
    assert plan_error.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR

    contract_payload = ia_bundle.contract.model_dump()
    concepts = cast(list[dict[str, Any]], contract_payload["research_concepts"])
    concepts[0]["term"] = "Type II supernova"
    tampered_contract = ScientificDataContract.model_validate(contract_payload)
    with pytest.raises(AppError) as contract_error:
        planner.plan(
            SearchPlanningRequest(
                contract=tampered_contract,
                routing=ia_bundle.routing,
                budget_policy=ia_bundle.budget_policy,
            )
        )
    assert contract_error.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR

    routing_payload = ia_bundle.routing.model_dump()
    routing_payload["warnings"] = ("tampered warning",)
    tampered_routing = RoutingDecision.model_validate(routing_payload)
    with pytest.raises(AppError) as routing_error:
        planner.plan(
            SearchPlanningRequest(
                contract=ia_bundle.contract,
                routing=tampered_routing,
                budget_policy=ia_bundle.budget_policy,
            )
        )
    assert routing_error.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR

    registry_payload = registry.model_dump()
    registry_capabilities = cast(list[dict[str, Any]], registry_payload["capabilities"])
    registry_capabilities[0]["display_name"] = "tampered registry source"
    tampered_registry = SourceCapabilityRegistry.model_validate(registry_payload)
    tampered_planner = SearchPlanner(
        registry=tampered_registry,
        available_source_ids=_source_ids(tampered_registry),
        clock=lambda: _CREATED_AT,
    )
    with pytest.raises(AppError) as registry_error:
        tampered_planner.plan(_request(ia_bundle))
    assert registry_error.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_progress_rejects_nonfinite_marginal_gain() -> None:
    with pytest.raises(ValidationError, match="finite"):
        SearchProgressSnapshot(
            completed_rounds=1,
            consumed_cost_micro_usd=0,
            elapsed_seconds=0,
            downloaded_bytes=0,
            model_tokens=0,
            required_field_coverage=0.0,
            source_category_coverage=0.0,
            has_primary_source=False,
            critical_gap_count=1,
            recent_marginal_gains=(float("nan"),),
        )


def test_result_linkage_rejects_tampered_metrics(
    ia_bundle: _Phase1Bundle,
    registry: SourceCapabilityRegistry,
) -> None:
    result = SearchPlanner(
        registry=registry,
        available_source_ids=_source_ids(registry),
        clock=lambda: _CREATED_AT,
    ).plan(_request(ia_bundle))
    payload = result.model_dump()
    metrics = cast(dict[str, Any], payload["metrics"])
    metrics["query_count"] += 1

    with pytest.raises(ValidationError, match="metrics must be derived"):
        type(result).model_validate(payload)


def test_source_capability_registry_is_content_addressed_and_strict(tmp_path: Path) -> None:
    path = Path("search_capabilities/registry.json")
    raw = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    capabilities = cast(list[dict[str, Any]], raw["capabilities"])
    capabilities[0]["display_name"] = "tampered"
    tampered_path = tmp_path / "tampered.json"
    tampered_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(RegistryLoadError) as mismatch:
        SourceCapabilityRegistryLoader.from_file(tampered_path)
    assert mismatch.value.code is RegistryErrorCode.HASH_MISMATCH

    raw = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    capabilities = cast(list[dict[str, Any]], raw["capabilities"])
    capabilities[0]["unexpected"] = True
    raw["content_hash"] = canonical_hash(
        {
            "registry_version": raw["registry_version"],
            "capabilities": raw["capabilities"],
            "term_expansions": raw["term_expansions"],
        }
    )
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(RegistryLoadError) as invalid:
        SourceCapabilityRegistryLoader.from_file(invalid_path)
    assert invalid.value.code is RegistryErrorCode.INVALID_SCHEMA


def test_search_registry_and_query_contract_negative_invariants(
    ia_bundle: _Phase1Bundle,
    registry: SourceCapabilityRegistry,
) -> None:
    capability = registry.capabilities[0]
    capability_base = capability.model_dump()
    capability_updates: tuple[tuple[dict[str, object], str], ...] = (
        ({"domains": (capability.domains[0],) * 2}, "domains must be unique"),
        ({"operations": (capability.operations[0],) * 2}, "operation ids must be unique"),
        ({"query_hints": (capability.query_hints[0],) * 2}, "query hints must be unique"),
    )
    for update, message in capability_updates:
        with pytest.raises(ValidationError, match=message):
            type(capability).model_validate({**capability_base, **update})

    expansion = registry.term_expansions[0]
    expansion_base = expansion.model_dump()
    with pytest.raises(ValidationError, match="domains must be unique"):
        type(expansion).model_validate({**expansion_base, "domains": (expansion.domains[0],) * 2})
    with pytest.raises(ValidationError, match="terms must be unique"):
        type(expansion).model_validate({**expansion_base, "terms": (expansion.terms[0],) * 2})
    with pytest.raises(ValidationError, match="capability ids must be unique"):
        SourceCapabilityRegistry.model_validate(
            {**registry.model_dump(), "capabilities": (capability, capability)}
        )
    with pytest.raises(ValidationError, match="expansion ids must be unique"):
        SourceCapabilityRegistry.model_validate(
            {**registry.model_dump(), "term_expansions": (expansion, expansion)}
        )
    with pytest.raises(ValidationError, match="historical normalized queries must be unique"):
        SearchHistorySnapshot(normalized_queries=("same query", "same query"))

    result = SearchPlanner(
        registry=registry,
        available_source_ids=_source_ids(registry),
        clock=lambda: _CREATED_AT,
    ).plan(_request(ia_bundle))
    family = result.plan.query_family_set.families[0]
    query = family.queries[0]
    parameter = query.parameters[0]
    with pytest.raises(ValidationError, match="parameter values must be unique"):
        type(parameter).model_validate(
            {**parameter.model_dump(), "values": (parameter.values[0],) * 2}
        )

    query_base = query.model_dump()
    query_updates: tuple[tuple[dict[str, object], str], ...] = (
        ({"target_fields": (query.target_fields[0],) * 2}, "target fields must be unique"),
        (
            {"expected_artifact_types": (query.expected_artifact_types[0],) * 2},
            "artifact types must be unique",
        ),
        ({"parameters": (parameter, parameter)}, "parameter names must be unique"),
    )
    for update, message in query_updates:
        with pytest.raises(ValidationError, match=message):
            type(query).model_validate({**query_base, **update})

    family_base = family.model_dump()
    family_updates: tuple[tuple[dict[str, object], str], ...] = (
        ({"state": QueryFamilyState.BUDGET_DEFERRED}, "only active query families"),
        ({"target_fields": (family.target_fields[0],) * 2}, "target fields must be unique"),
        ({"queries": (query, query)}, "query ids must be unique"),
        (
            {
                "queries": (
                    type(query).model_validate({**query_base, "family_id": "qfm_aaaaaaaaaaaaaaaa"}),
                )
            },
            "must reference their family",
        ),
    )
    for update, message in family_updates:
        with pytest.raises(ValidationError, match=message):
            type(family).model_validate({**family_base, **update})

    family_set = result.plan.query_family_set
    with pytest.raises(ValidationError, match="family ids must be unique"):
        type(family_set).model_validate({**family_set.model_dump(), "families": (family, family)})


def test_coverage_budget_and_plan_contract_negative_invariants(
    ia_bundle: _Phase1Bundle,
    registry: SourceCapabilityRegistry,
) -> None:
    result = SearchPlanner(
        registry=registry,
        available_source_ids=_source_ids(registry),
        clock=lambda: _CREATED_AT,
    ).plan(_request(ia_bundle))
    plan = result.plan
    cell = plan.coverage_matrix.cells[0]
    cell_base = cell.model_dump()
    cell_updates: tuple[tuple[dict[str, object], str], ...] = (
        ({"source_ids": (cell.source_ids[0],) * 2}, "source ids must be unique"),
        (
            {"planned_query_ids": (cell.planned_query_ids[0],) * 2},
            "query ids must be unique",
        ),
        ({"state": CoverageState.DEFERRED}, "planned coverage requires a query"),
        (
            {
                "state": CoverageState.UNAVAILABLE,
                "planned_query_ids": (),
            },
            "cannot claim an available source",
        ),
    )
    for update, message in cell_updates:
        with pytest.raises(ValidationError, match=message):
            type(cell).model_validate({**cell_base, **update})

    gate = plan.coverage_matrix.gate_targets[0]
    gate_base = gate.model_dump()
    gate_updates: tuple[tuple[dict[str, object], str], ...] = (
        ({"fields": (gate.fields[0],) * 2}, "gate fields must be unique"),
        (
            {"planned_query_ids": (gate.planned_query_ids[0],) * 2},
            "gate query ids must be unique",
        ),
        ({"state": CoverageState.DEFERRED}, "gate coverage requires a query"),
    )
    for update, message in gate_updates:
        with pytest.raises(ValidationError, match=message):
            type(gate).model_validate({**gate_base, **update})

    matrix = plan.coverage_matrix
    with pytest.raises(ValidationError, match="coverage cells must have unique"):
        type(matrix).model_validate({**matrix.model_dump(), "cells": (cell, cell)})
    with pytest.raises(ValidationError, match="gate targets must be unique"):
        type(matrix).model_validate({**matrix.model_dump(), "gate_targets": (gate, gate)})

    budget = plan.budget_allocation
    budget_base = budget.model_dump()
    budget_updates: tuple[tuple[dict[str, object], str], ...] = (
        (
            {"available_cost_micro_usd": budget.max_cost_micro_usd + 1},
            "available search cost cannot exceed",
        ),
        (
            {"available_duration_seconds": budget.max_duration_seconds + 1},
            "available search duration cannot exceed",
        ),
        (
            {"allocated_cost_micro_usd": budget.available_cost_micro_usd + 1},
            "allocated search cost cannot exceed",
        ),
        (
            {"allocated_duration_seconds": budget.available_duration_seconds + 1},
            "allocated search duration cannot exceed",
        ),
        (
            {"remaining_search_rounds": budget.max_search_rounds + 1},
            "remaining search rounds cannot exceed",
        ),
        (
            {"source_allocations": (budget.source_allocations[0],) * 2},
            "source budget allocations must be unique",
        ),
        (
            {"deferred_query_ids": (budget.source_allocations[0].query_ids[0],)},
            "cannot be both allocated and deferred",
        ),
        ({"allocated_query_count": 0}, "query count must match"),
        ({"allocated_cost_micro_usd": 0}, "cost must match"),
        ({"allocated_duration_seconds": 0}, "duration must match"),
    )
    for update, message in budget_updates:
        with pytest.raises(ValidationError, match=message):
            type(budget).model_validate({**budget_base, **update})

    with pytest.raises(ValidationError, match="search gap fields must be unique"):
        SearchGap(
            gap_id="gap_aaaaaaaaaaaaaaaa",
            code=SearchGapCode.BUDGET_DEFERRED,
            detail="duplicate fields",
            blocking=True,
            target_fields=(cell.field_name, cell.field_name),
        )
    with pytest.raises(ValidationError, match="new source counts cannot be negative"):
        SearchProgressSnapshot(
            completed_rounds=1,
            consumed_cost_micro_usd=0,
            elapsed_seconds=0,
            downloaded_bytes=0,
            model_tokens=0,
            required_field_coverage=0.0,
            source_category_coverage=0.0,
            has_primary_source=False,
            critical_gap_count=1,
            recent_new_source_counts=(-1,),
        )
    with pytest.raises(ValidationError, match="stop decision and outcome must agree"):
        SearchStopDecision(
            should_stop=True,
            reason=SearchStopReason.CONTINUE_SEARCH,
            outcome=SearchStopOutcome.CONTINUE,
            detail="invalid",
        )
    with pytest.raises(ValidationError, match="continue reason must be used"):
        SearchStopDecision(
            should_stop=True,
            reason=SearchStopReason.CONTINUE_SEARCH,
            outcome=SearchStopOutcome.PARTIAL,
            detail="invalid",
        )

    plan_base = plan.model_dump()
    wrong_family_set = type(plan.query_family_set).model_validate(
        {
            **plan.query_family_set.model_dump(),
            "task_id": "tsk_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        }
    )
    with pytest.raises(ValidationError, match="artifacts must share plan metadata"):
        type(plan).model_validate({**plan_base, "query_family_set": wrong_family_set})
    wrong_ref_family_set = type(plan.query_family_set).model_validate(
        {**plan.query_family_set.model_dump(), "contract_hash": "b" * 64}
    )
    with pytest.raises(ValidationError, match="must reference the plan inputs"):
        type(plan).model_validate({**plan_base, "query_family_set": wrong_ref_family_set})
    blocking_gap = SearchGap(
        gap_id="gap_bbbbbbbbbbbbbbbb",
        code=SearchGapCode.BUDGET_DEFERRED,
        detail="blocking",
        blocking=True,
    )
    with pytest.raises(ValidationError, match="succeeded search plan requires"):
        type(plan).model_validate({**plan_base, "gaps": (blocking_gap,)})
    with pytest.raises(ValidationError, match="unsupported search plan cannot contain"):
        type(plan).model_validate({**plan_base, "status": SearchPlanningStatus.UNSUPPORTED})

    result_base = result.model_dump()
    with pytest.raises(ValidationError, match="search plan must share result metadata"):
        type(result).model_validate(
            {
                **result_base,
                "task_id": "tsk_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            }
        )
    event = result.event.model_dump()
    payload = cast(dict[str, Any], event["payload"])
    payload["gap_count"] += 1
    with pytest.raises(ValidationError, match="event must refer"):
        type(result).model_validate({**result_base, "event": event})

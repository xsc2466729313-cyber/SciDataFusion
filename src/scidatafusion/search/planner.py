"""Capability-backed, replayable M04 scientific search planner."""

from __future__ import annotations

import hmac
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from threading import RLock

from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.routing import RoutingDecision, RoutingMode, RoutingStatus
from scidatafusion.contracts.scientific import (
    ContractStatus,
    FieldRequirement,
    ScientificDataContract,
)
from scidatafusion.contracts.search import (
    CoverageMatrixTemplate,
    ExecutableQuery,
    QueryFamily,
    QueryFamilyKind,
    QueryFamilySet,
    QueryFamilyState,
    QueryParameter,
    SearchBudgetAllocation,
    SearchCapabilityMode,
    SearchGap,
    SearchGapCode,
    SearchPlan,
    SearchPlanCreatedPayload,
    SearchPlanningMetrics,
    SearchPlanningRequest,
    SearchPlanningResult,
    SearchPlanningStatus,
    SearchStopPolicySpec,
    SourceBudgetAllocation,
    SourceCapability,
    SourceCapabilityRegistry,
    SourceCategory,
)
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.schema.compiler import ContractCompiler
from scidatafusion.search.coverage import CoveragePlanner
from scidatafusion.search.query_expansion import QueryExpander, normalize_query
from scidatafusion.search.registry import SourceCapabilityRegistryLoader


def _stable_id(prefix: str, value: object, *, length: int) -> str:
    return f"{prefix}_{canonical_hash(value)[:length]}"


_FAMILY_KIND: dict[SourceCategory, QueryFamilyKind] = {
    SourceCategory.LITERATURE_METADATA: QueryFamilyKind.LITERATURE_EVIDENCE,
    SourceCategory.DATA_REPOSITORY: QueryFamilyKind.REPOSITORY_DATASET,
    SourceCategory.DOMAIN_DATABASE: QueryFamilyKind.DOMAIN_DATABASE,
    SourceCategory.SUPPLEMENT_WEB: QueryFamilyKind.SUPPLEMENT_DISCOVERY,
}


@dataclass(frozen=True, slots=True)
class _FamilyCandidate:
    capability: SourceCapability
    family_id: str
    target_fields: tuple[str, ...]
    target_gate_ids: tuple[str, ...]
    queries: tuple[ExecutableQuery, ...]
    query_limit_exceeded: bool
    replay_suppressed: bool


def _semantic_plan_payload(
    *,
    allocation: SearchBudgetAllocation,
    budget_policy_hash: str,
    capability_mode: SearchCapabilityMode,
    capability_registry_hash: str,
    contract_hash: str,
    contract_id: str,
    coverage: CoverageMatrixTemplate,
    gaps: tuple[SearchGap, ...],
    family_set: QueryFamilySet,
    routing_ref: str,
    status: SearchPlanningStatus,
    stop_policy: SearchStopPolicySpec,
) -> dict[str, object]:
    return {
        "budget_allocation": allocation.model_dump(mode="json", exclude={"created_at"}),
        "budget_policy_hash": budget_policy_hash,
        "capability_mode": capability_mode.value,
        "capability_registry_hash": capability_registry_hash,
        "contract_hash": contract_hash,
        "contract_id": contract_id,
        "coverage_matrix": coverage.model_dump(mode="json", exclude={"created_at"}),
        "gaps": [item.model_dump(mode="json") for item in gaps],
        "query_family_set": family_set.model_dump(mode="json", exclude={"created_at"}),
        "routing_ref": routing_ref,
        "status": status.value,
        "stop_policy": stop_policy.model_dump(mode="json"),
    }


def calculate_search_plan_hash(plan: SearchPlan) -> str:
    """Recalculate the semantic hash that every M05 consumer must verify."""

    return canonical_hash(
        _semantic_plan_payload(
            allocation=plan.budget_allocation,
            budget_policy_hash=plan.budget_policy_hash,
            capability_mode=plan.capability_mode,
            capability_registry_hash=plan.capability_registry_hash,
            contract_hash=plan.contract_hash,
            contract_id=plan.contract_id,
            coverage=plan.coverage_matrix,
            gaps=plan.gaps,
            family_set=plan.query_family_set,
            routing_ref=plan.routing_ref,
            status=plan.status,
            stop_policy=plan.stop_policy,
        )
    )


def verify_search_plan_integrity(plan: SearchPlan) -> None:
    """Reject a plan whose content or identifier no longer matches its semantic hash."""

    actual_hash = calculate_search_plan_hash(plan)
    expected_id = f"spl_{actual_hash[:32]}"
    if not (
        hmac.compare_digest(plan.plan_hash, actual_hash)
        and hmac.compare_digest(plan.plan_id, expected_id)
    ):
        raise AppError(
            ErrorCode.ARTIFACT_INTEGRITY_ERROR,
            "search plan content does not match its immutable identifiers",
        )


class SearchPlanner:
    """Create an executable plan without treating static capabilities as runtime health."""

    def __init__(
        self,
        registry: SourceCapabilityRegistry | None = None,
        *,
        available_source_ids: Iterable[str] = (),
        clock: Callable[[], datetime] = utc_now,
        producer_version: str = "1.0.0",
        query_expander: QueryExpander | None = None,
    ) -> None:
        self._registry = registry or SourceCapabilityRegistryLoader.load_default()
        known_ids = {item.source_id for item in self._registry.capabilities}
        available = frozenset(available_source_ids)
        unknown = sorted(available - known_ids)
        if unknown:
            msg = f"unregistered runtime source ids: {unknown!r}"
            raise ValueError(msg)
        self._available_source_ids = available
        self._clock = clock
        self._producer_version = producer_version
        self._query_expander = query_expander or QueryExpander()
        self._cache: dict[str, SearchPlanningResult] = {}
        self._cache_lock = RLock()

    def plan(
        self,
        request: SearchPlanningRequest,
        *,
        force_recompute: bool = False,
    ) -> SearchPlanningResult:
        """Validate all upstream links, then return one content-addressed M04 plan."""

        self._validate_request(request)
        input_hash = canonical_hash(
            {
                "available_source_ids": sorted(self._available_source_ids),
                "budget_policy": request.budget_policy.model_dump(mode="json"),
                "capability_mode": request.capability_mode.value,
                "contract": request.contract.model_dump(mode="json"),
                "history": request.history.model_dump(mode="json"),
                "registry_hash": self._registry.content_hash,
                "routing": request.routing.model_dump(mode="json"),
            }
        )
        idempotency_key = canonical_hash(
            {
                "contract_version": request.contract.version,
                "input_hash": input_hash,
                "module_id": "M04",
                "producer_version": self._producer_version,
                "task_id": request.contract.task_id,
            }
        )
        with self._cache_lock:
            cached = self._cache.get(idempotency_key)
            if cached is not None and not force_recompute:
                return cached

        result = self._build_result(request, input_hash, idempotency_key)
        with self._cache_lock:
            existing = self._cache.get(idempotency_key)
            if existing is not None and not force_recompute:
                return existing
            self._cache[idempotency_key] = result
        return result

    def _validate_request(self, request: SearchPlanningRequest) -> None:
        contract = request.contract
        routing = request.routing
        budget = request.budget_policy
        ContractCompiler.verify_integrity(contract)
        self._verify_routing_integrity(routing)
        self._verify_registry_integrity()
        if contract.status is not ContractStatus.CONFIRMED:
            raise AppError(
                ErrorCode.QUALITY_GATE_FAILED,
                "M04 requires an explicitly confirmed scientific data contract",
            )
        if (
            contract.task_id != routing.task_id
            or contract.run_id != routing.run_id
            or contract.version != routing.contract_version
            or contract.task_id != budget.task_id
            or contract.run_id != budget.run_id
            or contract.version != budget.contract_version
        ):
            raise AppError(
                ErrorCode.VALIDATION_FAILED,
                "contract, routing decision, and budget policy must share task/run/version",
            )
        if not hmac.compare_digest(contract.routing_ref, routing.decision_hash):
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "confirmed contract does not reference this routing decision",
            )
        if (
            routing.status is not RoutingStatus.SUCCEEDED
            or routing.pack_selection.mode is not RoutingMode.FORMAL
            or routing.pack_selection.missing_capabilities
        ):
            raise AppError(
                ErrorCode.QUALITY_GATE_FAILED,
                "M04 requires a succeeded formal route with no capability gaps",
            )
        allocation = budget.allocation
        max_cost_micro_usd = round(allocation.max_cost_usd * 1_000_000)
        history = request.history
        if (
            history.consumed_cost_micro_usd > max_cost_micro_usd
            or history.elapsed_seconds > allocation.max_duration_seconds
            or history.completed_rounds > allocation.max_search_rounds
            or history.downloaded_bytes > allocation.max_download_bytes
            or history.model_tokens > allocation.max_model_tokens
        ):
            raise AppError(
                ErrorCode.BUDGET_EXCEEDED,
                "search history exceeds the immutable M00 budget allocation",
            )

    def _verify_registry_integrity(self) -> None:
        actual_hash = canonical_hash(
            {
                "capabilities": [
                    item.model_dump(mode="json") for item in self._registry.capabilities
                ],
                "registry_version": self._registry.registry_version,
                "term_expansions": [
                    item.model_dump(mode="json") for item in self._registry.term_expansions
                ],
            }
        )
        if not hmac.compare_digest(self._registry.content_hash, actual_hash):
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "source capability registry content hash does not match its entries",
            )

    @staticmethod
    def _verify_routing_integrity(routing: RoutingDecision) -> None:
        semantic_payload = {
            "confidence": routing.confidence,
            "contract_version": routing.contract_version,
            "domain_profile": routing.domain_profile.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in routing.evidence],
            "fallback_path": list(routing.fallback_path),
            "input_hash": routing.input_hash,
            "pack_selection": routing.pack_selection.model_dump(mode="json"),
            "producer_version": routing.producer_version,
            "registry_hash": routing.registry_hash,
            "status": routing.status.value,
            "task_archetypes": routing.task_archetypes.model_dump(mode="json"),
            "warnings": list(routing.warnings),
        }
        actual_hash = canonical_hash(semantic_payload)
        if not hmac.compare_digest(routing.decision_hash, actual_hash):
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "routing decision content does not match decision_hash",
            )

    def _build_result(
        self,
        request: SearchPlanningRequest,
        input_hash: str,
        idempotency_key: str,
    ) -> SearchPlanningResult:
        contract = request.contract
        created_at = self._clock()
        applicable = self._applicable_capabilities(contract)
        family_candidates, deduplicated_count = self._build_candidates(
            contract,
            applicable,
            round_number=request.history.completed_rounds + 1,
            historical_queries=frozenset(request.history.normalized_queries),
        )
        budget_policy_hash = canonical_hash(request.budget_policy.model_dump(mode="json"))
        allocation, allocated_query_ids = self._allocate(
            request,
            tuple(query for item in family_candidates for query in item.queries),
            created_at=created_at,
            budget_policy_hash=budget_policy_hash,
        )
        families = self._materialize_families(family_candidates, allocated_query_ids)
        gaps = self._build_gaps(contract, applicable, family_candidates, families, allocation)
        if not contract.research_concepts:
            status = SearchPlanningStatus.NEEDS_REVIEW
        elif not allocated_query_ids:
            status = SearchPlanningStatus.UNSUPPORTED
        elif any(item.blocking for item in gaps):
            status = SearchPlanningStatus.PARTIAL
        else:
            status = SearchPlanningStatus.SUCCEEDED

        family_set = QueryFamilySet(
            task_id=contract.task_id,
            run_id=contract.run_id,
            contract_version=contract.version,
            created_at=created_at,
            producer_version=self._producer_version,
            contract_hash=contract.contract_hash,
            families=families,
        )
        coverage = CoveragePlanner.build(
            contract,
            families,
            applicable,
            created_at=created_at,
            producer_version=self._producer_version,
        )
        resource_budget = request.budget_policy.allocation
        stop_policy = SearchStopPolicySpec(
            max_cost_micro_usd=round(resource_budget.max_cost_usd * 1_000_000),
            max_duration_seconds=resource_budget.max_duration_seconds,
            max_search_rounds=resource_budget.max_search_rounds,
            max_download_bytes=resource_budget.max_download_bytes,
            max_model_tokens=resource_budget.max_model_tokens,
        )
        plan_seed = _semantic_plan_payload(
            allocation=allocation,
            budget_policy_hash=budget_policy_hash,
            capability_mode=request.capability_mode,
            capability_registry_hash=self._registry.content_hash,
            contract_hash=contract.contract_hash,
            contract_id=contract.contract_id,
            coverage=coverage,
            gaps=gaps,
            family_set=family_set,
            routing_ref=contract.routing_ref,
            status=status,
            stop_policy=stop_policy,
        )
        plan_hash = canonical_hash(plan_seed)
        plan = SearchPlan(
            task_id=contract.task_id,
            run_id=contract.run_id,
            contract_version=contract.version,
            created_at=created_at,
            producer_version=self._producer_version,
            plan_id=f"spl_{plan_hash[:32]}",
            status=status,
            capability_mode=request.capability_mode,
            contract_id=contract.contract_id,
            contract_hash=contract.contract_hash,
            routing_ref=contract.routing_ref,
            capability_registry_hash=self._registry.content_hash,
            budget_policy_hash=budget_policy_hash,
            query_family_set=family_set,
            coverage_matrix=coverage,
            budget_allocation=allocation,
            stop_policy=stop_policy,
            gaps=gaps,
            plan_hash=plan_hash,
        )
        verify_search_plan_integrity(plan)
        metrics = self._metrics(plan, deduplicated_count)
        warnings = tuple(item.detail for item in gaps)
        output_hash = canonical_hash(
            {
                "metrics": metrics.model_dump(mode="json"),
                "plan": plan_seed,
                "plan_hash": plan_hash,
                "warnings": warnings,
            }
        )
        payload = SearchPlanCreatedPayload(
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            status=status,
            input_hash=input_hash,
            output_hash=output_hash,
            idempotency_key=idempotency_key,
            query_count=metrics.query_count,
            family_count=metrics.family_count,
            gap_count=metrics.gap_count,
            capability_registry_hash=self._registry.content_hash,
        )
        event = EventEnvelope[SearchPlanCreatedPayload](
            event_type=EventType.SEARCH_PLAN_CREATED,
            task_id=contract.task_id,
            run_id=contract.run_id,
            occurred_at=created_at,
            producer=ProducerRef(component="search_planner", version=self._producer_version),
            payload=payload,
        )
        return SearchPlanningResult(
            task_id=contract.task_id,
            run_id=contract.run_id,
            contract_version=contract.version,
            created_at=created_at,
            producer_version=self._producer_version,
            status=status,
            input_hash=input_hash,
            output_hash=output_hash,
            idempotency_key=idempotency_key,
            plan=plan,
            warnings=warnings,
            metrics=metrics,
            event=event,
        )

    def _applicable_capabilities(
        self, contract: ScientificDataContract
    ) -> tuple[SourceCapability, ...]:
        domains = {item.casefold() for item in contract.domain_profile}
        source_types = set(contract.acceptable_source_types)
        return tuple(
            capability
            for capability in self._registry.capabilities
            if (
                "generic" in {item.casefold() for item in capability.domains}
                or domains.intersection(item.casefold() for item in capability.domains)
            )
            and source_types.intersection(capability.contract_source_types)
            and any(
                source_types.intersection(field.source_preference).intersection(
                    capability.contract_source_types
                )
                for field in contract.fields
            )
        )

    def _build_candidates(
        self,
        contract: ScientificDataContract,
        capabilities: tuple[SourceCapability, ...],
        *,
        round_number: int,
        historical_queries: frozenset[str],
    ) -> tuple[tuple[_FamilyCandidate, ...], int]:
        candidates: list[_FamilyCandidate] = []
        duplicate_count = 0
        for capability in capabilities:
            fields = tuple(
                field.name
                for field in contract.fields
                if set(field.source_preference).intersection(capability.contract_source_types)
            )
            gates = tuple(
                gate.gate_id
                for gate in contract.quality_gates
                if set(gate.fields).intersection(fields)
            )
            family_id = _stable_id("qfm", (contract.contract_hash, capability.source_id), length=16)
            operation = capability.operations[0]
            expanded = self._query_expander.expand(
                contract.research_concepts,
                contract.domain_profile,
                self._registry,
                capability.query_hints,
            )
            queries: list[ExecutableQuery] = []
            seen: set[tuple[str, str, str]] = set()
            limit_exceeded = False
            replay_suppressed = False
            for item in expanded:
                normalized = normalize_query(item.text)
                key = (capability.source_id, operation.dialect.value, normalized)
                if key in seen or normalized in historical_queries:
                    duplicate_count += 1
                    replay_suppressed = replay_suppressed or normalized in historical_queries
                    continue
                seen.add(key)
                if len(item.text) > operation.max_query_length:
                    limit_exceeded = True
                    continue
                query_id = _stable_id(
                    "qry",
                    (
                        contract.contract_hash,
                        capability.source_id,
                        operation.operation_id,
                        normalized,
                        round_number,
                    ),
                    length=16,
                )
                queries.append(
                    ExecutableQuery(
                        query_id=query_id,
                        family_id=family_id,
                        source_id=capability.source_id,
                        operation_id=operation.operation_id,
                        category=capability.category,
                        protocol=capability.protocol,
                        dialect=operation.dialect,
                        language=item.language,
                        round_number=round_number,
                        query_text=item.text,
                        normalized_query=normalized,
                        parameters=(
                            QueryParameter(name="terms", values=item.terms),
                            QueryParameter(name="target_fields", values=fields),
                        ),
                        result_limit=operation.default_result_limit,
                        target_fields=fields,
                        target_gate_ids=gates,
                        expected_artifact_types=capability.expected_artifact_types,
                        rationale=(
                            f"Use the registered {capability.display_name} operation to seek "
                            f"evidence for contract fields: {', '.join(fields)}."
                        ),
                        primary_source=capability.primary_source,
                        priority=capability.priority,
                        estimated_cost_micro_usd=(capability.estimated_query_cost_micro_usd),
                        estimated_duration_seconds=(capability.estimated_query_duration_seconds),
                    )
                )
            candidates.append(
                _FamilyCandidate(
                    capability=capability,
                    family_id=family_id,
                    target_fields=fields,
                    target_gate_ids=gates,
                    queries=tuple(queries),
                    query_limit_exceeded=limit_exceeded,
                    replay_suppressed=replay_suppressed,
                )
            )
        return tuple(candidates), duplicate_count

    def _allocate(
        self,
        request: SearchPlanningRequest,
        queries: tuple[ExecutableQuery, ...],
        *,
        created_at: datetime,
        budget_policy_hash: str,
    ) -> tuple[SearchBudgetAllocation, frozenset[str]]:
        contract = request.contract
        budget = request.budget_policy.allocation
        history = request.history
        max_cost = round(budget.max_cost_usd * 1_000_000)
        available_cost = max(0, max_cost - history.consumed_cost_micro_usd)
        available_duration = max(0, budget.max_duration_seconds - history.elapsed_seconds)
        remaining_rounds = max(0, budget.max_search_rounds - history.completed_rounds)
        fields = {item.name: item for item in contract.fields}
        blocking_gates = {item.gate_id for item in contract.quality_gates if item.blocking}

        def priority(query: ExecutableQuery) -> tuple[object, ...]:
            required_count = sum(
                fields[name].requirement is FieldRequirement.REQUIRED
                for name in query.target_fields
            )
            critical = bool(blocking_gates.intersection(query.target_gate_ids)) or bool(
                required_count
            )
            return (
                -int(critical),
                -required_count,
                -int(query.primary_source),
                -query.priority,
                query.normalized_query,
                query.source_id,
                query.query_id,
            )

        selected: list[ExecutableQuery] = []
        deferred: list[str] = []
        spent_cost = 0
        spent_duration = 0
        for query in sorted(queries, key=priority):
            fits = (
                remaining_rounds > 0
                and spent_cost + query.estimated_cost_micro_usd <= available_cost
                and spent_duration + query.estimated_duration_seconds <= available_duration
                and query.source_id in self._available_source_ids
            )
            if fits:
                selected.append(query)
                spent_cost += query.estimated_cost_micro_usd
                spent_duration += query.estimated_duration_seconds
            elif query.source_id in self._available_source_ids:
                deferred.append(query.query_id)

        by_source: dict[str, list[ExecutableQuery]] = defaultdict(list)
        for query in selected:
            by_source[query.source_id].append(query)
        source_allocations = tuple(
            SourceBudgetAllocation(
                source_id=source_id,
                query_ids=tuple(item.query_id for item in source_queries),
                allocated_cost_micro_usd=sum(
                    item.estimated_cost_micro_usd for item in source_queries
                ),
                allocated_duration_seconds=sum(
                    item.estimated_duration_seconds for item in source_queries
                ),
            )
            for source_id, source_queries in sorted(by_source.items())
        )
        allocation = SearchBudgetAllocation(
            task_id=contract.task_id,
            run_id=contract.run_id,
            contract_version=contract.version,
            created_at=created_at,
            producer_version=self._producer_version,
            budget_policy_hash=budget_policy_hash,
            max_cost_micro_usd=max_cost,
            available_cost_micro_usd=available_cost,
            allocated_cost_micro_usd=spent_cost,
            max_duration_seconds=budget.max_duration_seconds,
            available_duration_seconds=available_duration,
            allocated_duration_seconds=spent_duration,
            max_search_rounds=budget.max_search_rounds,
            remaining_search_rounds=remaining_rounds,
            allocated_query_count=len(selected),
            source_allocations=source_allocations,
            deferred_query_ids=tuple(deferred),
        )
        return allocation, frozenset(item.query_id for item in selected)

    def _materialize_families(
        self,
        candidates: tuple[_FamilyCandidate, ...],
        allocated_query_ids: frozenset[str],
    ) -> tuple[QueryFamily, ...]:
        families: list[QueryFamily] = []
        for candidate in candidates:
            active_queries = tuple(
                item for item in candidate.queries if item.query_id in allocated_query_ids
            )
            if candidate.capability.source_id not in self._available_source_ids:
                state = QueryFamilyState.CAPABILITY_UNAVAILABLE
            elif not active_queries:
                state = QueryFamilyState.BUDGET_DEFERRED
            else:
                state = QueryFamilyState.ACTIVE
            # Runtime availability is resolved independently from static registry membership.
            if (
                candidate.capability.source_id in self._available_source_ids
                and not candidate.queries
                and candidate.query_limit_exceeded
            ):
                state = QueryFamilyState.QUERY_INVALID
            families.append(
                QueryFamily(
                    family_id=candidate.family_id,
                    kind=_FAMILY_KIND[candidate.capability.category],
                    state=state,
                    source_id=candidate.capability.source_id,
                    category=candidate.capability.category,
                    target_fields=candidate.target_fields,
                    target_gate_ids=candidate.target_gate_ids,
                    queries=active_queries,
                    rationale=(
                        f"Plan the {candidate.capability.category.value} source category "
                        "against explicit contract fields."
                    ),
                )
            )
        return tuple(families)

    def _build_gaps(
        self,
        contract: ScientificDataContract,
        capabilities: tuple[SourceCapability, ...],
        candidates: tuple[_FamilyCandidate, ...],
        families: tuple[QueryFamily, ...],
        allocation: SearchBudgetAllocation,
    ) -> tuple[SearchGap, ...]:
        gaps: list[SearchGap] = []
        if not contract.research_concepts:
            gaps.append(
                self._gap(
                    SearchGapCode.RESEARCH_CONCEPT_MISSING,
                    "The confirmed contract has no evidence-grounded research concept.",
                    blocking=True,
                    target_fields=tuple(item.name for item in contract.fields),
                )
            )
        registered_types = {
            source_type
            for capability in capabilities
            for source_type in capability.contract_source_types
        }
        critical_fields = {
            field.name
            for field in contract.fields
            if field.requirement is FieldRequirement.REQUIRED
        } | {name for gate in contract.quality_gates if gate.blocking for name in gate.fields}
        for source_type in contract.acceptable_source_types:
            if source_type in registered_types:
                continue
            fields = tuple(
                field.name for field in contract.fields if source_type in field.source_preference
            )
            gaps.append(
                self._gap(
                    SearchGapCode.SOURCE_TYPE_UNREGISTERED,
                    f"No registered source capability supports '{source_type}'.",
                    blocking=bool(critical_fields.intersection(fields)),
                    target_fields=fields,
                    contract_source_type=source_type,
                )
            )
        families_by_source = {item.source_id: item for item in families}
        deferred_ids = set(allocation.deferred_query_ids)
        for candidate in candidates:
            source = candidate.capability
            family = families_by_source[source.source_id]
            if source.source_id not in self._available_source_ids:
                gaps.append(
                    self._gap(
                        SearchGapCode.CAPABILITY_UNAVAILABLE,
                        f"Runtime health did not mark source '{source.source_id}' available.",
                        blocking=True,
                        target_fields=candidate.target_fields,
                        source_id=source.source_id,
                        category=source.category,
                    )
                )
                continue
            if candidate.query_limit_exceeded:
                gaps.append(
                    self._gap(
                        SearchGapCode.QUERY_LIMIT_EXCEEDED,
                        f"A query exceeded the registered limit for '{source.source_id}'.",
                        blocking=not family.queries,
                        target_fields=candidate.target_fields,
                        source_id=source.source_id,
                        category=source.category,
                    )
                )
            if candidate.replay_suppressed:
                gaps.append(
                    self._gap(
                        SearchGapCode.QUERY_REPLAY_SUPPRESSED,
                        f"A previously executed query was suppressed for '{source.source_id}'.",
                        blocking=not family.queries,
                        target_fields=candidate.target_fields,
                        source_id=source.source_id,
                        category=source.category,
                    )
                )
            if deferred_ids.intersection(item.query_id for item in candidate.queries):
                gaps.append(
                    self._gap(
                        SearchGapCode.BUDGET_DEFERRED,
                        f"Budget limits deferred one or more queries for '{source.source_id}'.",
                        blocking=not family.queries,
                        target_fields=candidate.target_fields,
                        source_id=source.source_id,
                        category=source.category,
                    )
                )
        return tuple(gaps)

    @staticmethod
    def _gap(
        code: SearchGapCode,
        detail: str,
        *,
        blocking: bool,
        target_fields: tuple[str, ...],
        source_id: str | None = None,
        category: SourceCategory | None = None,
        contract_source_type: str | None = None,
    ) -> SearchGap:
        seed = (
            code.value,
            detail,
            target_fields,
            source_id,
            category.value if category is not None else None,
            contract_source_type,
        )
        return SearchGap(
            gap_id=_stable_id("gap", seed, length=16),
            code=code,
            detail=detail,
            blocking=blocking,
            target_fields=target_fields,
            source_id=source_id,
            category=category,
            contract_source_type=contract_source_type,
        )

    @staticmethod
    def _metrics(plan: SearchPlan, deduplicated_count: int) -> SearchPlanningMetrics:
        families = plan.query_family_set.families
        active = tuple(item for item in families if item.state is QueryFamilyState.ACTIVE)
        queries = tuple(query for item in active for query in item.queries)
        return SearchPlanningMetrics(
            family_count=len(families),
            active_family_count=len(active),
            query_count=len(queries),
            planned_source_category_count=len({item.category for item in active}),
            coverage_cell_count=len(plan.coverage_matrix.cells),
            gap_count=len(plan.gaps),
            deferred_query_count=len(plan.budget_allocation.deferred_query_ids),
            deduplicated_query_count=deduplicated_count,
        )

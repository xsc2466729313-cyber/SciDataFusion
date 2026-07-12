"""Idempotent M08 artifact classification and aggregate parse-plan service."""

from __future__ import annotations

import asyncio
import hashlib
from concurrent.futures import Future
from datetime import datetime
from threading import RLock

from pydantic import ValidationError

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.parsing import (
    ArtifactClassification,
    ArtifactPlanEntry,
    EscalationRule,
    ParsePlan,
    ParsePlanCreatedPayload,
    ParsePlanningMetrics,
    ParsePlanningRequest,
    ParsePlanningResult,
    ParsePlanningStatus,
    ParsePlanStatus,
    ParserRoute,
    ParseSourceObjectRef,
    ParsingGap,
    ParsingGapCode,
    QualityCheckSpec,
    RouteBlockerCode,
    RouteDisposition,
)
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.parsing.checkpoints import (
    MemoryParseCheckpointStore,
    ParseCheckpointStore,
)
from scidatafusion.parsing.classifier import (
    ArtifactClassifier,
    DeterministicArtifactClassifier,
)
from scidatafusion.parsing.integrity import (
    QUALITY_CHECK_THRESHOLDS,
    calculate_artifact_plan_entry_hash,
    calculate_classification_hash,
    calculate_escalation_rule_hash,
    calculate_parse_plan_hash,
    calculate_parse_planning_idempotency_key,
    calculate_parse_planning_input_hash,
    calculate_parse_planning_output_hash,
    calculate_parse_policy_hash,
    calculate_parser_route_hash,
    calculate_parsing_gap_id,
    calculate_quality_check_id,
    verify_parse_planning_integrity,
    verify_parse_planning_request_integrity,
)
from scidatafusion.parsing.router import ParseRouter, RegistryParseRouter, RouteDecision

_ZERO_HASH = "0" * 64
_GAP_BY_BLOCKER = {
    RouteBlockerCode.NEEDS_PASSWORD: ParsingGapCode.PASSWORD_REQUIRED,
    RouteBlockerCode.DAMAGED_FILE: ParsingGapCode.DAMAGED_INPUT,
    RouteBlockerCode.UNKNOWN_FORMAT: ParsingGapCode.FORMAT_GAP,
    RouteBlockerCode.PARSER_UNAVAILABLE: ParsingGapCode.CAPABILITY_GAP,
    RouteBlockerCode.CAPABILITY_MISSING: ParsingGapCode.CAPABILITY_GAP,
    RouteBlockerCode.BUDGET_EXHAUSTED: ParsingGapCode.BUDGET_GAP,
    RouteBlockerCode.POLICY_BLOCKED: ParsingGapCode.POLICY_GAP,
    RouteBlockerCode.CLASSIFICATION_REVIEW_REQUIRED: ParsingGapCode.CLASSIFICATION_GAP,
    RouteBlockerCode.INTERNAL_PLANNING_ERROR: ParsingGapCode.INTERNAL_ERROR,
}


class ParsePlanningService:
    """Classify unique Bronze objects and plan registered downstream parsers."""

    def __init__(
        self,
        *,
        store: BronzeByteStore,
        classifier: ArtifactClassifier | None = None,
        router: ParseRouter | None = None,
        checkpoints: ParseCheckpointStore | None = None,
        producer_version: str = "1.0.0",
    ) -> None:
        self._store = store
        self._classifier = classifier or DeterministicArtifactClassifier()
        self._router = router or RegistryParseRouter()
        self._checkpoints = checkpoints or MemoryParseCheckpointStore()
        self._producer_version = producer_version
        self._cache: dict[str, ParsePlanningResult] = {}
        self._inflight: dict[str, Future[ParsePlanningResult]] = {}
        self._lock = RLock()

    async def execute(self, request: ParsePlanningRequest) -> ParsePlanningResult:
        """Return one integrity-checked result and coalesce identical executions."""

        verify_parse_planning_request_integrity(request, self._store)
        input_hash = calculate_parse_planning_input_hash(request)
        idempotency_key = calculate_parse_planning_idempotency_key(
            request,
            self._producer_version,
        )
        if not request.force_recompute:
            with self._lock:
                cached = self._cache.get(idempotency_key)
                if cached is not None:
                    return cached
            checkpoint = self._checkpoints.load(idempotency_key)
            if checkpoint is not None:
                if checkpoint.producer_version != self._producer_version:
                    raise AppError(
                        ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                        "M08 checkpoint producer does not match this service",
                    )
                verify_parse_planning_integrity(checkpoint, request, self._store)
                with self._lock:
                    return self._cache.setdefault(idempotency_key, checkpoint)

        with self._lock:
            pending = self._inflight.get(idempotency_key)
            is_owner = pending is None
            if pending is None:
                pending = Future()
                self._inflight[idempotency_key] = pending
        if not is_owner:
            return await asyncio.shield(asyncio.wrap_future(pending))
        try:
            result = await self._execute_once(
                request,
                input_hash=input_hash,
                idempotency_key=idempotency_key,
            )
            result = self._checkpoints.save(result)
            verify_parse_planning_integrity(result, request, self._store)
        except BaseException as exc:
            with self._lock:
                self._inflight.pop(idempotency_key, None)
            pending.set_exception(exc)
            raise
        with self._lock:
            existing = self._cache.setdefault(idempotency_key, result)
            self._inflight.pop(idempotency_key, None)
        pending.set_result(existing)
        return existing

    async def _execute_once(
        self,
        request: ParsePlanningRequest,
        *,
        input_hash: str,
        idempotency_key: str,
    ) -> ParsePlanningResult:
        await asyncio.sleep(0)
        created_at = request.runtime.checked_at
        artifact_set = request.download_result.artifact_set
        manifest = request.download_result.manifest
        acquisitions_by_object = {
            obj.object_id: tuple(
                item for item in manifest.acquisitions if item.object_id == obj.object_id
            )
            for obj in artifact_set.objects
        }
        source_objects = tuple(
            ParseSourceObjectRef(
                object_id=obj.object_id,
                byte_sha256=obj.byte_sha256,
                object_metadata_hash=obj.object_metadata_hash,
                size_bytes=obj.size_bytes,
                acquisition_ids=tuple(
                    item.acquisition_id for item in acquisitions_by_object[obj.object_id]
                ),
                candidate_ids=tuple(
                    dict.fromkeys(
                        item.candidate_id for item in acquisitions_by_object[obj.object_id]
                    )
                ),
            )
            for obj in artifact_set.objects
        )

        classifications: list[ArtifactClassification] = []
        for obj, source in zip(artifact_set.objects, source_objects, strict=True):
            await asyncio.sleep(0)
            content = self._store.read(obj.byte_sha256)
            if len(content) != obj.size_bytes or hashlib.sha256(content).hexdigest() != (
                obj.byte_sha256
            ):
                raise AppError(
                    ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "M08 Bronze bytes differ from immutable M07 metadata",
                )
            try:
                classification_decision = self._classifier.classify(
                    obj,
                    content,
                    request.policy,
                )
                draft = ArtifactClassification(
                    task_id=request.contract.task_id,
                    run_id=request.contract.run_id,
                    contract_version=request.contract.version,
                    created_at=created_at,
                    producer_version=self._producer_version,
                    classification_id="cls_" + "0" * 32,
                    object_id=obj.object_id,
                    byte_sha256=obj.byte_sha256,
                    object_metadata_hash=obj.object_metadata_hash,
                    acquisition_ids=source.acquisition_ids,
                    artifact_set_hash=artifact_set.artifact_set_hash,
                    manifest_hash=manifest.manifest_hash,
                    classified_media_type=classification_decision.classified_media_type,
                    artifact_kind=classification_decision.artifact_kind,
                    format_family=classification_decision.format_family,
                    features=classification_decision.features,
                    basis=classification_decision.basis,
                    confidence=classification_decision.confidence,
                    source_media_type_mismatch=(classification_decision.source_media_type_mismatch),
                    requires_review=bool(classification_decision.review_codes),
                    review_codes=classification_decision.review_codes,
                    classification_hash=_ZERO_HASH,
                )
            except AppError:
                raise
            except (AttributeError, TypeError, ValueError, ValidationError) as exc:
                raise AppError(
                    ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "M08 classifier returned an invalid structural decision",
                ) from exc
            value = calculate_classification_hash(draft)
            classifications.append(
                ArtifactClassification.model_validate(
                    draft.model_copy(
                        update={
                            "classification_id": f"cls_{value[:32]}",
                            "classification_hash": value,
                        }
                    ).model_dump()
                )
            )

        routes: list[ParserRoute] = []
        entries: list[ArtifactPlanEntry] = []
        gaps: list[ParsingGap] = []
        remaining_cost = min(
            request.policy.max_total_planned_cost_micro_usd,
            request.runtime.remaining_cost_micro_usd,
        )
        for classification, source in zip(classifications, source_objects, strict=True):
            await asyncio.sleep(0)
            try:
                decisions = self._router.route(
                    classification,
                    size_bytes=source.size_bytes,
                    registry=request.capability_registry,
                    runtime=request.runtime,
                    policy=request.policy,
                    remaining_cost_micro_usd=remaining_cost,
                )
                if not decisions:
                    raise AppError(
                        ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                        "M08 router omitted an artifact disposition",
                    )
                object_routes: list[ParserRoute] = []
                object_gaps: list[ParsingGap] = []
                object_cost = 0
                for route_decision in decisions:
                    route = _build_route(
                        route_decision,
                        classification=classification,
                        registry_hash=request.capability_registry.registry_hash,
                        created_at=created_at,
                        producer_version=self._producer_version,
                        task_id=request.contract.task_id,
                        run_id=request.contract.run_id,
                        contract_version=request.contract.version,
                    )
                    object_routes.append(route)
                    object_cost += route.max_cost_micro_usd
                    for blocker in route.blockers:
                        gap_draft = ParsingGap(
                            gap_id="pgp_" + "0" * 16,
                            code=_GAP_BY_BLOCKER[blocker],
                            object_id=route.object_id,
                            classification_id=route.classification_id,
                            route_id=route.route_id,
                            detail=f"{blocker.value}_requires_downstream_resolution",
                        )
                        object_gaps.append(
                            gap_draft.model_copy(
                                update={"gap_id": calculate_parsing_gap_id(gap_draft)}
                            )
                        )
                entry_status = _derive_entry_status(
                    tuple(item.disposition for item in object_routes)
                )
                entry_draft = ArtifactPlanEntry(
                    task_id=request.contract.task_id,
                    run_id=request.contract.run_id,
                    contract_version=request.contract.version,
                    created_at=created_at,
                    producer_version=self._producer_version,
                    entry_id="ape_" + "0" * 32,
                    object_id=source.object_id,
                    byte_sha256=source.byte_sha256,
                    classification_id=classification.classification_id,
                    classification_hash=classification.classification_hash,
                    route_ids=tuple(item.route_id for item in object_routes),
                    route_hashes=tuple(item.route_hash for item in object_routes),
                    status=entry_status,
                    explanation="deterministic_registry_route_or_explicit_disposition",
                    entry_hash=_ZERO_HASH,
                )
                entry_hash = calculate_artifact_plan_entry_hash(entry_draft)
                entry = entry_draft.model_copy(
                    update={
                        "entry_id": f"ape_{entry_hash[:32]}",
                        "entry_hash": entry_hash,
                    }
                )
            except AppError:
                raise
            except (AttributeError, KeyError, TypeError, ValueError, ValidationError) as exc:
                raise AppError(
                    ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "M08 router returned an invalid parse decision",
                ) from exc
            routes.extend(object_routes)
            gaps.extend(object_gaps)
            entries.append(entry)
            remaining_cost = max(0, remaining_cost - object_cost)

        status = _derive_result_status(tuple(item.status for item in entries))
        upstream_event = next(
            item
            for item in request.download_result.events
            if item.event_type is EventType.ARTIFACT_DOWNLOAD_COMPLETED
        )
        try:
            plan_draft = ParsePlan(
                task_id=request.contract.task_id,
                run_id=request.contract.run_id,
                contract_version=request.contract.version,
                created_at=created_at,
                producer_version=self._producer_version,
                plan_id="ppl_" + "0" * 32,
                status=status,
                contract_id=request.contract.contract_id,
                contract_hash=request.contract.contract_hash,
                artifact_set_hash=artifact_set.artifact_set_hash,
                manifest_hash=manifest.manifest_hash,
                upstream_download_output_hash=request.download_result.output_hash,
                upstream_download_event_id=upstream_event.event_id,
                policy=request.policy,
                policy_hash=calculate_parse_policy_hash(request.policy),
                capability_registry=request.capability_registry,
                runtime=request.runtime,
                source_objects=source_objects,
                classifications=tuple(classifications),
                routes=tuple(routes),
                entries=tuple(entries),
                gaps=tuple(gaps),
                plan_hash=_ZERO_HASH,
            )
            plan_hash = calculate_parse_plan_hash(plan_draft)
            plan = ParsePlan.model_validate(
                plan_draft.model_copy(
                    update={
                        "plan_id": f"ppl_{plan_hash[:32]}",
                        "plan_hash": plan_hash,
                    }
                ).model_dump()
            )
        except AppError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError, ValidationError) as exc:
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "M08 router returned an invalid aggregate parse plan",
            ) from exc
        warnings = tuple(
            f"{code.value}:{item.classification_id}"
            for item in plan.classifications
            for code in item.review_codes
        ) + tuple(
            f"{code.value}:{route.route_id}" for route in plan.routes for code in route.blockers
        )
        metrics = _metrics(plan)
        event_id = f"evt_{canonical_hash((idempotency_key, 'parse-plan-created'))[:32]}"
        payload = ParsePlanCreatedPayload(
            status=status,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            contract_id=plan.contract_id,
            contract_hash=plan.contract_hash,
            artifact_set_hash=plan.artifact_set_hash,
            manifest_hash=plan.manifest_hash,
            upstream_download_output_hash=plan.upstream_download_output_hash,
            capability_registry_hash=plan.capability_registry.registry_hash,
            runtime_hash=plan.runtime.runtime_hash,
            policy_hash=plan.policy_hash,
            artifact_plan_count=len(plan.entries),
            classification_count=len(plan.classifications),
            route_count=len(plan.routes),
            gap_count=len(plan.gaps),
            input_hash=input_hash,
            output_hash=_ZERO_HASH,
            idempotency_key=idempotency_key,
        )
        event = EventEnvelope[ParsePlanCreatedPayload](
            event_id=event_id,
            event_type=EventType.PARSE_PLAN_CREATED,
            task_id=request.contract.task_id,
            run_id=request.contract.run_id,
            occurred_at=created_at,
            schema_version=request.contract.version,
            producer=ProducerRef(
                component="parse_planning_service",
                version=self._producer_version,
            ),
            payload=payload,
            correlation_id=input_hash,
            causation_event_id=upstream_event.event_id,
        )
        draft_result = ParsePlanningResult(
            task_id=request.contract.task_id,
            run_id=request.contract.run_id,
            contract_version=request.contract.version,
            created_at=created_at,
            producer_version=self._producer_version,
            status=status,
            input_hash=input_hash,
            output_hash=_ZERO_HASH,
            idempotency_key=idempotency_key,
            plan=plan,
            warnings=warnings,
            metrics=metrics,
            event=event,
        )
        output_hash = calculate_parse_planning_output_hash(draft_result)
        final_event = event.model_copy(
            update={"payload": payload.model_copy(update={"output_hash": output_hash})}
        )
        result = ParsePlanningResult.model_validate(
            draft_result.model_copy(
                update={"output_hash": output_hash, "event": final_event}
            ).model_dump()
        )
        verify_parse_planning_integrity(result, request, self._store)
        return result


def _build_route(
    decision: RouteDecision,
    *,
    classification: ArtifactClassification,
    registry_hash: str,
    created_at: datetime,
    producer_version: str,
    task_id: str,
    run_id: str,
    contract_version: str,
) -> ParserRoute:
    checks = tuple(
        QualityCheckSpec(
            check_id=calculate_quality_check_id(
                object_id=classification.object_id,
                scope=decision.scope,
                kind=kind,
                registry_hash=registry_hash,
            ),
            kind=kind,
            minimum_score=QUALITY_CHECK_THRESHOLDS[kind],
        )
        for kind in decision.quality_checks
    )
    check_by_kind = {item.kind: item for item in checks}
    rules: list[EscalationRule] = []
    for fallback in decision.fallbacks:
        draft = EscalationRule(
            rule_id="esc_" + "0" * 16,
            trigger_check_id=check_by_kind[fallback.trigger].check_id,
            fallback_parser_id=fallback.capability.parser_id,
            resource_tier=fallback.capability.resource_tier,
            additional_cost_micro_usd=fallback.capability.estimated_cost_micro_usd,
            rule_hash=_ZERO_HASH,
        )
        value = calculate_escalation_rule_hash(draft)
        rules.append(draft.model_copy(update={"rule_id": f"esc_{value[:16]}", "rule_hash": value}))
    draft_route = ParserRoute(
        task_id=task_id,
        run_id=run_id,
        contract_version=contract_version,
        created_at=created_at,
        producer_version=producer_version,
        route_id="prt_" + "0" * 32,
        object_id=classification.object_id,
        classification_id=classification.classification_id,
        classification_hash=classification.classification_hash,
        scope=decision.scope,
        disposition=decision.disposition,
        target_module=decision.target_module,
        primary_parser_id=(decision.primary.parser_id if decision.primary is not None else None),
        fallback_parser_ids=tuple(item.capability.parser_id for item in decision.fallbacks),
        resource_tier=(decision.primary.resource_tier if decision.primary is not None else None),
        quality_checks=checks,
        escalation_rules=tuple(rules),
        blockers=decision.blockers,
        max_cost_micro_usd=decision.max_cost_micro_usd,
        confidence=decision.confidence,
        rationale=decision.rationale,
        capability_registry_hash=registry_hash,
        route_hash=_ZERO_HASH,
    )
    value = calculate_parser_route_hash(draft_route)
    return ParserRoute.model_validate(
        draft_route.model_copy(
            update={"route_id": f"prt_{value[:32]}", "route_hash": value}
        ).model_dump()
    )


def _derive_entry_status(
    dispositions: tuple[RouteDisposition, ...],
) -> ParsePlanStatus:
    values = tuple(item.value for item in dispositions)
    if all(item in {"parse", "metadata_only"} for item in values):
        return ParsePlanStatus.SUCCEEDED
    if all(item == "needs_review" for item in values):
        return ParsePlanStatus.NEEDS_REVIEW
    if all(item == "unsupported" for item in values):
        return ParsePlanStatus.UNSUPPORTED
    if all(item == "failed" for item in values):
        return ParsePlanStatus.FAILED
    return ParsePlanStatus.PARTIAL


def _derive_result_status(statuses: tuple[ParsePlanStatus, ...]) -> ParsePlanningStatus:
    values = tuple(item.value for item in statuses)
    if all(item == "succeeded" for item in values):
        return ParsePlanningStatus.SUCCEEDED
    if all(item == "needs_review" for item in values):
        return ParsePlanningStatus.NEEDS_REVIEW
    if all(item == "unsupported" for item in values):
        return ParsePlanningStatus.UNSUPPORTED
    if all(item == "failed" for item in values):
        return ParsePlanningStatus.FAILED
    return ParsePlanningStatus.PARTIAL


def _metrics(plan: ParsePlan) -> ParsePlanningMetrics:
    return ParsePlanningMetrics(
        artifact_count=len(plan.source_objects),
        classification_count=len(plan.classifications),
        route_count=len(plan.routes),
        page_route_count=sum(route.scope.kind.value == "page_range" for route in plan.routes),
        succeeded_plan_count=sum(
            entry.status is ParsePlanStatus.SUCCEEDED for entry in plan.entries
        ),
        partial_plan_count=sum(entry.status is ParsePlanStatus.PARTIAL for entry in plan.entries),
        review_plan_count=sum(
            entry.status is ParsePlanStatus.NEEDS_REVIEW for entry in plan.entries
        ),
        unsupported_plan_count=sum(
            entry.status is ParsePlanStatus.UNSUPPORTED for entry in plan.entries
        ),
        failed_plan_count=sum(entry.status is ParsePlanStatus.FAILED for entry in plan.entries),
        gap_count=len(plan.gaps),
        format_gap_count=sum(gap.code is ParsingGapCode.FORMAT_GAP for gap in plan.gaps),
        capability_gap_count=sum(gap.code is ParsingGapCode.CAPABILITY_GAP for gap in plan.gaps),
        model_candidate_classification_count=sum(
            "model_candidate" in {item.value for item in classification.basis}
            for classification in plan.classifications
        ),
        high_resource_primary_route_count=sum(
            route.disposition is RouteDisposition.PARSE
            and route.resource_tier is not None
            and route.resource_tier.value == "high"
            for route in plan.routes
        ),
        planned_cost_micro_usd=sum(route.max_cost_micro_usd for route in plan.routes),
    )

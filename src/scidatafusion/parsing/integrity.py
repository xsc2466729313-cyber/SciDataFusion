"""Canonical hashes and end-to-end integrity checks for M08 parse planning."""

from __future__ import annotations

import hmac
from typing import NoReturn

from scidatafusion.artifacts.integrity import (
    calculate_artifact_download_input_hash,
    verify_artifact_download_integrity,
)
from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.artifacts import ArtifactAcquisition
from scidatafusion.contracts.events import EventType
from scidatafusion.contracts.parsing import (
    ArtifactClassification,
    ArtifactPlanEntry,
    EscalationRule,
    ParsePlan,
    ParsePlanningPolicy,
    ParsePlanningRequest,
    ParsePlanningResult,
    ParsePlanningRuntimeSnapshot,
    ParserRoute,
    ParseScope,
    ParsingGap,
    QualityCheckKind,
    RouteDisposition,
)
from scidatafusion.contracts.scientific import ContractStatus
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.parsing.registry import (
    calculate_parser_capability_hash,
    calculate_parser_registry_hash,
)
from scidatafusion.schema import ContractCompiler

QUALITY_CHECK_THRESHOLDS = {
    QualityCheckKind.OUTPUT_SCHEMA: 1.0,
    QualityCheckKind.TEXT_COVERAGE: 0.8,
    QualityCheckKind.READING_ORDER: 0.8,
    QualityCheckKind.TABLE_STRUCTURE: 0.9,
    QualityCheckKind.FIGURE_GEOMETRY: 0.9,
    QualityCheckKind.SCIENTIFIC_STRUCTURE: 0.95,
}


def calculate_parse_policy_hash(policy: ParsePlanningPolicy) -> str:
    """Hash the complete versioned M08 routing policy."""

    return canonical_hash(policy.model_dump(mode="json"))


def calculate_parse_runtime_hash(runtime: ParsePlanningRuntimeSnapshot) -> str:
    """Hash an immutable M08 runtime snapshot without its self-reference."""

    return canonical_hash(runtime.model_dump(mode="json", exclude={"runtime_hash"}))


def calculate_parse_planning_input_hash(request: ParsePlanningRequest) -> str:
    """Bind the exact scientific contract, M07 result, registry, runtime, and policy."""

    return canonical_hash(
        {
            "artifact_download_input_hash": calculate_artifact_download_input_hash(
                request.download_request
            ),
            "artifact_download_output_hash": request.download_result.output_hash,
            "artifact_set_hash": request.download_result.artifact_set.artifact_set_hash,
            "capability_registry_hash": request.capability_registry.registry_hash,
            "contract_hash": request.contract.contract_hash,
            "manifest_hash": request.download_result.manifest.manifest_hash,
            "policy_hash": calculate_parse_policy_hash(request.policy),
            "runtime_hash": request.runtime.runtime_hash,
        }
    )


def calculate_parse_planning_idempotency_key(
    request: ParsePlanningRequest,
    producer_version: str,
) -> str:
    """Return the producer-bound M08 idempotency key required by the V4 contract."""

    return canonical_hash(
        {
            "contract_version": request.contract.version,
            "input_hash": calculate_parse_planning_input_hash(request),
            "module_id": "M08",
            "producer_version": producer_version,
            "task_id": request.contract.task_id,
        }
    )


def calculate_classification_hash(classification: ArtifactClassification) -> str:
    """Recalculate one semantic classification hash."""

    return canonical_hash(
        classification.model_dump(
            mode="json",
            exclude={"classification_hash", "classification_id", "created_at"},
        )
    )


def calculate_quality_check_id(
    *,
    object_id: str,
    scope: ParseScope,
    kind: QualityCheckKind,
    registry_hash: str,
) -> str:
    """Derive a stable quality-check identifier from route inputs."""

    value = canonical_hash(
        {
            "kind": kind.value,
            "object_id": object_id,
            "registry_hash": registry_hash,
            "scope": scope.model_dump(mode="json"),
        }
    )
    return f"pqc_{value[:16]}"


def calculate_escalation_rule_hash(rule: EscalationRule) -> str:
    """Recalculate one conditional fallback rule hash."""

    return canonical_hash(rule.model_dump(mode="json", exclude={"rule_hash", "rule_id"}))


def calculate_parser_route_hash(route: ParserRoute) -> str:
    """Recalculate one parser route hash."""

    return canonical_hash(
        route.model_dump(mode="json", exclude={"route_hash", "route_id", "created_at"})
    )


def calculate_artifact_plan_entry_hash(entry: ArtifactPlanEntry) -> str:
    """Recalculate one per-object plan-entry hash."""

    return canonical_hash(
        entry.model_dump(mode="json", exclude={"entry_hash", "entry_id", "created_at"})
    )


def calculate_parsing_gap_id(gap: ParsingGap) -> str:
    """Derive a stable identifier for one explicit parsing gap."""

    value = canonical_hash(gap.model_dump(mode="json", exclude={"gap_id"}))
    return f"pgp_{value[:16]}"


def calculate_parse_plan_hash(plan: ParsePlan) -> str:
    """Recalculate the aggregate M08 plan hash."""

    return canonical_hash(
        plan.model_dump(mode="json", exclude={"plan_hash", "plan_id", "created_at"})
    )


def calculate_parse_planning_output_hash(result: ParsePlanningResult) -> str:
    """Hash every semantic M08 output while breaking the event output-hash cycle."""

    return canonical_hash(
        {
            "contract_version": result.contract_version,
            "created_at": result.created_at.isoformat(),
            "event": result.event.model_dump(
                mode="json",
                exclude={"payload": {"output_hash"}},
            ),
            "idempotency_key": result.idempotency_key,
            "input_hash": result.input_hash,
            "metrics": result.metrics.model_dump(mode="json"),
            "plan_hash": result.plan.plan_hash,
            "producer_version": result.producer_version,
            "run_id": result.run_id,
            "status": result.status.value,
            "task_id": result.task_id,
            "warnings": list(result.warnings),
        }
    )


def verify_parse_planning_request_integrity(
    request: ParsePlanningRequest,
    store: BronzeByteStore,
) -> None:
    """Fail closed on a tampered contract, M07 snapshot, registry, runtime, or bytes."""

    ContractCompiler.verify_integrity(request.contract)
    if request.contract.status is not ContractStatus.CONFIRMED:
        _fail("M08 requires an explicitly confirmed scientific data contract")
    verify_artifact_download_integrity(
        request.download_result,
        request.download_request,
        store,
    )
    selected = request.download_request.selected_source_set
    if not (
        selected.contract_id == request.contract.contract_id
        and selected.contract_hash == request.contract.contract_hash
    ):
        _fail("M08 M07 selection does not match the supplied scientific contract")
    registry = request.capability_registry
    if any(
        not hmac.compare_digest(
            item.capability_hash,
            calculate_parser_capability_hash(item),
        )
        for item in registry.parsers
    ) or not hmac.compare_digest(
        registry.registry_hash,
        calculate_parser_registry_hash(registry),
    ):
        _fail("M08 parser registry content does not match its immutable hashes")
    if not hmac.compare_digest(
        request.runtime.runtime_hash,
        calculate_parse_runtime_hash(request.runtime),
    ):
        _fail("M08 runtime snapshot does not match its immutable hash")


def verify_parse_planning_integrity(
    result: ParsePlanningResult,
    request: ParsePlanningRequest,
    store: BronzeByteStore,
) -> None:
    """Verify M08 lineage, hashes, routes, metrics, checkpoint replay, and event causality."""

    verify_parse_planning_request_integrity(request, store)
    expected_input = calculate_parse_planning_input_hash(request)
    expected_idempotency = calculate_parse_planning_idempotency_key(
        request,
        result.producer_version,
    )
    plan = result.plan
    completed_events = tuple(
        item
        for item in request.download_result.events
        if item.event_type is EventType.ARTIFACT_DOWNLOAD_COMPLETED
    )
    if len(completed_events) != 1:
        _fail("M08 requires exactly one upstream artifact completion event")
    completed_event = completed_events[0]
    if not (
        hmac.compare_digest(result.input_hash, expected_input)
        and hmac.compare_digest(result.idempotency_key, expected_idempotency)
        and result.task_id == request.contract.task_id
        and result.run_id == request.contract.run_id
        and result.contract_version == request.contract.version
        and result.created_at == request.runtime.checked_at
        and plan.contract_id == request.contract.contract_id
        and plan.contract_hash == request.contract.contract_hash
        and plan.artifact_set_hash == request.download_result.artifact_set.artifact_set_hash
        and plan.manifest_hash == request.download_result.manifest.manifest_hash
        and plan.upstream_download_output_hash == request.download_result.output_hash
        and plan.upstream_download_event_id == completed_event.event_id
        and plan.policy == request.policy
        and plan.policy_hash == calculate_parse_policy_hash(request.policy)
        and plan.capability_registry == request.capability_registry
        and plan.runtime == request.runtime
    ):
        _fail("M08 result does not match its immutable request snapshot")

    acquisitions_by_object: dict[str, list[ArtifactAcquisition]] = {}
    for acquisition in request.download_result.manifest.acquisitions:
        acquisitions_by_object.setdefault(acquisition.object_id, []).append(acquisition)
    objects = request.download_result.artifact_set.objects
    if tuple(item.object_id for item in plan.source_objects) != tuple(
        item.object_id for item in objects
    ):
        _fail("M08 source-object order must exactly project the M07 artifact set")
    for source, obj in zip(plan.source_objects, objects, strict=True):
        acquisitions = acquisitions_by_object.get(obj.object_id, [])
        expected_acquisition_ids = tuple(item.acquisition_id for item in acquisitions)
        expected_candidate_ids = tuple(dict.fromkeys(item.candidate_id for item in acquisitions))
        if not (
            source.object_id == obj.object_id
            and source.byte_sha256 == obj.byte_sha256
            and source.object_metadata_hash == obj.object_metadata_hash
            and source.size_bytes == obj.size_bytes
            and source.acquisition_ids == expected_acquisition_ids
            and source.candidate_ids == expected_candidate_ids
        ):
            _fail("M08 source-object lineage is not an exact M07 projection")

    for classification in plan.classifications:
        value = calculate_classification_hash(classification)
        if not (
            hmac.compare_digest(classification.classification_hash, value)
            and hmac.compare_digest(classification.classification_id, f"cls_{value[:32]}")
        ):
            _fail("M08 classification content does not match its immutable hash")
    parser_by_id = {item.parser_id: item for item in request.capability_registry.parsers}
    for route in plan.routes:
        for check in route.quality_checks:
            expected = calculate_quality_check_id(
                object_id=route.object_id,
                scope=route.scope,
                kind=check.kind,
                registry_hash=route.capability_registry_hash,
            )
            if (
                check.check_id != expected
                or check.minimum_score != QUALITY_CHECK_THRESHOLDS[check.kind]
            ):
                _fail("M08 route quality-check id is not deterministic")
        if route.disposition is RouteDisposition.PARSE:
            if route.primary_parser_id is None:
                _fail("M08 executable route lacks a primary parser")
            parser_ids = (route.primary_parser_id, *route.fallback_parser_ids)
            capabilities = tuple(parser_by_id[item] for item in parser_ids)
            primary = capabilities[0]
            rules_by_parser = {item.fallback_parser_id: item for item in route.escalation_rules}
            if (
                tuple(item.kind for item in route.quality_checks) != primary.quality_checks
                or route.max_cost_micro_usd
                != sum(item.estimated_cost_micro_usd for item in capabilities)
                or any(
                    rules_by_parser[item.parser_id].additional_cost_micro_usd
                    != item.estimated_cost_micro_usd
                    for item in capabilities[1:]
                )
            ):
                _fail("M08 route checks and costs must exactly follow the parser registry")
        for rule in route.escalation_rules:
            value = calculate_escalation_rule_hash(rule)
            if not (
                hmac.compare_digest(rule.rule_hash, value)
                and hmac.compare_digest(rule.rule_id, f"esc_{value[:16]}")
            ):
                _fail("M08 escalation rule does not match its immutable hash")
        value = calculate_parser_route_hash(route)
        if not (
            hmac.compare_digest(route.route_hash, value)
            and hmac.compare_digest(route.route_id, f"prt_{value[:32]}")
        ):
            _fail("M08 parser route does not match its immutable hash")
    for entry in plan.entries:
        value = calculate_artifact_plan_entry_hash(entry)
        if not (
            hmac.compare_digest(entry.entry_hash, value)
            and hmac.compare_digest(entry.entry_id, f"ape_{value[:32]}")
        ):
            _fail("M08 artifact-plan entry does not match its immutable hash")
    if any(gap.gap_id != calculate_parsing_gap_id(gap) for gap in plan.gaps):
        _fail("M08 parsing gap id is not content derived")
    plan_hash = calculate_parse_plan_hash(plan)
    output_hash = calculate_parse_planning_output_hash(result)
    expected_event_id = f"evt_{canonical_hash((result.idempotency_key, 'parse-plan-created'))[:32]}"
    if not (
        hmac.compare_digest(plan.plan_hash, plan_hash)
        and hmac.compare_digest(plan.plan_id, f"ppl_{plan_hash[:32]}")
        and hmac.compare_digest(result.output_hash, output_hash)
        and hmac.compare_digest(result.event.event_id, expected_event_id)
    ):
        _fail("M08 aggregate result does not match its immutable hashes")


def _fail(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)

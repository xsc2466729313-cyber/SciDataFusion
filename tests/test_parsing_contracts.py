from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from scidatafusion.artifacts.fixtures import build_offline_ia_artifact_bundle
from scidatafusion.artifacts.service import ArtifactDownloadService
from scidatafusion.cli import _build_search_planning, _execute_offline_connectors
from scidatafusion.contracts.artifacts import (
    ArtifactDownloadRequest,
    ArtifactDownloadResult,
    ArtifactKind,
)
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.parsing import (
    ArtifactClassification,
    ArtifactPlanEntry,
    ClassificationBasis,
    ClassificationReviewCode,
    EscalationRule,
    FormatFamily,
    PageStructuralFeature,
    ParsePlan,
    ParsePlanCreatedPayload,
    ParsePlanningExecutionMode,
    ParsePlanningMetrics,
    ParsePlanningPolicy,
    ParsePlanningRequest,
    ParsePlanningResult,
    ParsePlanningRuntimeSnapshot,
    ParsePlanningStatus,
    ParsePlanStatus,
    ParserCapability,
    ParserCapabilityRegistry,
    ParserRoute,
    ParserTargetModule,
    ParseScope,
    ParseScopeKind,
    ParseSourceObjectRef,
    ParsingGap,
    ParsingGapCode,
    QualityCheckKind,
    QualityCheckSpec,
    ResourceTier,
    RouteBlockerCode,
    RouteDisposition,
    StructuralFeatures,
    _derive_status,
    _validate_gap_codes,
    _validate_parser_route,
    _validate_scope_coverage,
)
from scidatafusion.contracts.scientific import ScientificDataContract
from scidatafusion.contracts.selection import SourceSelectionRequest
from scidatafusion.parsing.integrity import calculate_parse_runtime_hash
from scidatafusion.selection import SourceSelectionService

NOW = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
GOAL = "Study Type Ia supernova light curves using multi-source integration into CSV."


@dataclass(frozen=True)
class _Upstream:
    contract: ScientificDataContract
    download_request: ArtifactDownloadRequest
    download_result: ArtifactDownloadResult


def _hash(value: int) -> str:
    return f"{value:064x}"


@pytest.fixture(scope="module")
def upstream() -> _Upstream:
    phase1, planning = _build_search_planning(GOAL, "authenticated-m08-reviewer")
    assert planning is not None
    assert phase1.confirmation is not None
    contract = phase1.confirmation.contract
    connector_result = asyncio.run(_execute_offline_connectors(planning))
    selected = (
        SourceSelectionService(clock=lambda: NOW)
        .select(
            SourceSelectionRequest(
                contract=contract,
                search_plan=planning.plan,
                connector_result=connector_result,
            )
        )
        .selected_source_set
    )
    bundle = build_offline_ia_artifact_bundle(selected, clock=lambda: NOW)
    download_request = ArtifactDownloadRequest(
        selected_source_set=selected,
        policy=bundle.policy,
        runtime=bundle.runtime,
        approvals=bundle.approvals,
        requested_at=NOW,
    )
    download_result = asyncio.run(
        ArtifactDownloadService(
            transport=bundle.transport,
            clock=lambda: NOW,
        ).execute(download_request)
    )
    return _Upstream(
        contract=contract,
        download_request=download_request,
        download_result=download_result,
    )


def _registry() -> ParserCapabilityRegistry:
    document = ParserCapability(
        parser_id="generic.document",
        parser_version="1.0.0",
        target_modules=(ParserTargetModule.DOCUMENT,),
        artifact_kinds=(
            ArtifactKind.DOCUMENT,
            ArtifactKind.LANDING_PAGE,
        ),
        format_families=(
            FormatFamily.PDF,
            FormatFamily.HTML,
            FormatFamily.PLAIN_TEXT,
        ),
        media_types=("application/pdf", "text/html", "text/plain"),
        supports_page_scope=True,
        resource_tier=ResourceTier.LOW,
        primary_eligible=True,
        quality_checks=(QualityCheckKind.TEXT_COVERAGE, QualityCheckKind.OUTPUT_SCHEMA),
        deterministic=True,
        estimated_cost_micro_usd=10,
        max_input_bytes=10_000_000,
        capability_hash=_hash(10),
    )
    table = ParserCapability(
        parser_id="native.table",
        parser_version="1.0.0",
        target_modules=(ParserTargetModule.TABLE,),
        artifact_kinds=(ArtifactKind.TABLE,),
        format_families=(FormatFamily.CSV,),
        media_types=("text/csv",),
        supports_page_scope=False,
        resource_tier=ResourceTier.LOW,
        primary_eligible=True,
        quality_checks=(QualityCheckKind.OUTPUT_SCHEMA, QualityCheckKind.TABLE_STRUCTURE),
        deterministic=True,
        estimated_cost_micro_usd=5,
        max_input_bytes=10_000_000,
        capability_hash=_hash(11),
    )
    ocr = ParserCapability(
        parser_id="ocr.document",
        parser_version="1.0.0",
        target_modules=(ParserTargetModule.DOCUMENT,),
        artifact_kinds=(ArtifactKind.DOCUMENT,),
        format_families=(FormatFamily.PDF,),
        media_types=("application/pdf",),
        supports_page_scope=True,
        resource_tier=ResourceTier.HIGH,
        primary_eligible=False,
        quality_checks=(QualityCheckKind.TEXT_COVERAGE, QualityCheckKind.OUTPUT_SCHEMA),
        fallback_trigger_checks=(QualityCheckKind.TEXT_COVERAGE,),
        deterministic=False,
        requires_model=True,
        estimated_cost_micro_usd=100,
        max_input_bytes=10_000_000,
        capability_hash=_hash(12),
    )
    return ParserCapabilityRegistry(
        registry_version="1.0.0",
        parsers=(document, table, ocr),
        registry_hash=_hash(13),
    )


def _policy() -> ParsePlanningPolicy:
    return ParsePlanningPolicy(
        max_sample_bytes_per_artifact=1_048_576,
        max_sample_pages_per_artifact=8,
        max_routes_per_artifact=8,
        max_route_cost_micro_usd=200,
        max_total_planned_cost_micro_usd=1_000,
    )


def _runtime(
    registry: ParserCapabilityRegistry, checked_at: datetime
) -> ParsePlanningRuntimeSnapshot:
    return ParsePlanningRuntimeSnapshot(
        execution_mode=ParsePlanningExecutionMode.OFFLINE,
        capability_registry_hash=registry.registry_hash,
        available_parser_ids=tuple(item.parser_id for item in registry.parsers),
        remaining_cost_micro_usd=1_000,
        checked_at=checked_at,
        runtime_hash=_hash(14),
    )


def _format_family(media_type: str) -> FormatFamily:
    return {
        "application/pdf": FormatFamily.PDF,
        "application/zip": FormatFamily.ARCHIVE,
        "text/csv": FormatFamily.CSV,
        "text/html": FormatFamily.HTML,
        "text/plain": FormatFamily.PLAIN_TEXT,
    }[media_type]


def _features(media_type: str, size_bytes: int) -> StructuralFeatures:
    if media_type != "application/pdf":
        return StructuralFeatures(sampled_bytes=min(size_bytes, 1024))
    page = PageStructuralFeature(
        page_number=1,
        text_layer_density=0.8,
        scanned_probability=0.2,
        table_probability=0.0,
        figure_probability=0.1,
    )
    return StructuralFeatures(
        sampled_bytes=min(size_bytes, 1024),
        total_pages=1,
        sampled_pages=1,
        pages=(page,),
        text_layer_density=0.8,
        scanned_page_ratio=0.2,
        table_page_ratio=0.0,
        figure_page_ratio=0.1,
    )


def _plan(upstream: _Upstream) -> ParsePlan:
    result = upstream.download_result
    registry = _registry()
    policy = _policy()
    created_at = max(upstream.contract.created_at, result.created_at) + timedelta(seconds=1)
    runtime = _runtime(registry, created_at)
    acquisitions_by_object = {
        obj.object_id: tuple(
            item for item in result.manifest.acquisitions if item.object_id == obj.object_id
        )
        for obj in result.artifact_set.objects
    }
    source_objects: list[ParseSourceObjectRef] = []
    classifications: list[ArtifactClassification] = []
    routes: list[ParserRoute] = []
    entries: list[ArtifactPlanEntry] = []
    for index, obj in enumerate(result.artifact_set.objects, start=1):
        acquisitions = acquisitions_by_object[obj.object_id]
        source_objects.append(
            ParseSourceObjectRef(
                object_id=obj.object_id,
                byte_sha256=obj.byte_sha256,
                object_metadata_hash=obj.object_metadata_hash,
                size_bytes=obj.size_bytes,
                acquisition_ids=tuple(item.acquisition_id for item in acquisitions),
                candidate_ids=tuple(dict.fromkeys(item.candidate_id for item in acquisitions)),
            )
        )
        classification = ArtifactClassification(
            task_id=result.task_id,
            run_id=result.run_id,
            contract_version=result.contract_version,
            created_at=created_at,
            producer_version="1.0.0",
            classification_id=f"cls_{index:032x}",
            object_id=obj.object_id,
            byte_sha256=obj.byte_sha256,
            object_metadata_hash=obj.object_metadata_hash,
            acquisition_ids=tuple(item.acquisition_id for item in acquisitions),
            artifact_set_hash=result.artifact_set.artifact_set_hash,
            manifest_hash=result.manifest.manifest_hash,
            classified_media_type=obj.media.detected_media_type,
            artifact_kind=obj.media.artifact_kind,
            format_family=_format_family(obj.media.detected_media_type),
            features=_features(obj.media.detected_media_type, obj.size_bytes),
            basis=(ClassificationBasis.M07_INSPECTION, ClassificationBasis.MAGIC_BYTES),
            confidence=1.0,
            source_media_type_mismatch=False,
            requires_review=False,
            classification_hash=_hash(100 + index),
        )
        classifications.append(classification)
        route_id = f"prt_{index:032x}"
        route_hash = _hash(200 + index)
        common = {
            "task_id": result.task_id,
            "run_id": result.run_id,
            "contract_version": result.contract_version,
            "created_at": created_at,
            "producer_version": "1.0.0",
            "route_id": route_id,
            "object_id": obj.object_id,
            "classification_id": classification.classification_id,
            "classification_hash": classification.classification_hash,
            "scope": ParseScope(kind=ParseScopeKind.ARTIFACT),
            "capability_registry_hash": registry.registry_hash,
            "route_hash": route_hash,
        }
        if classification.format_family is FormatFamily.ARCHIVE:
            route = ParserRoute.model_validate(
                {
                    **common,
                    "disposition": RouteDisposition.METADATA_ONLY,
                    "max_cost_micro_usd": 0,
                    "confidence": 0.0,
                    "rationale": (
                        "M07 already materialized safe archive members; retain container metadata."
                    ),
                }
            )
        elif classification.format_family is FormatFamily.CSV:
            route = ParserRoute.model_validate(
                {
                    **common,
                    "disposition": RouteDisposition.PARSE,
                    "target_module": ParserTargetModule.TABLE,
                    "primary_parser_id": "native.table",
                    "resource_tier": ResourceTier.LOW,
                    "quality_checks": (
                        QualityCheckSpec(
                            check_id=f"pqc_{index:016x}",
                            kind=QualityCheckKind.OUTPUT_SCHEMA,
                            minimum_score=1.0,
                        ),
                    ),
                    "max_cost_micro_usd": 5,
                    "confidence": 1.0,
                    "rationale": (
                        "Native CSV structure is routed to deterministic table recovery."
                    ),
                }
            )
        else:
            check = QualityCheckSpec(
                check_id=f"pqc_{index:016x}",
                kind=QualityCheckKind.TEXT_COVERAGE,
                minimum_score=0.9,
            )
            pdf = classification.format_family is FormatFamily.PDF
            route = ParserRoute.model_validate(
                {
                    **common,
                    "disposition": RouteDisposition.PARSE,
                    "target_module": ParserTargetModule.DOCUMENT,
                    "primary_parser_id": "generic.document",
                    "fallback_parser_ids": ("ocr.document",) if pdf else (),
                    "resource_tier": ResourceTier.LOW,
                    "quality_checks": (check,),
                    "escalation_rules": (
                        EscalationRule(
                            rule_id=f"esc_{index:016x}",
                            trigger_check_id=check.check_id,
                            fallback_parser_id="ocr.document",
                            resource_tier=ResourceTier.HIGH,
                            additional_cost_micro_usd=90,
                            rule_hash=_hash(300 + index),
                        ),
                    )
                    if pdf
                    else (),
                    "max_cost_micro_usd": 100 if pdf else 10,
                    "confidence": 1.0,
                    "rationale": (
                        "Use deterministic document parsing before any quality-gated OCR fallback."
                    ),
                }
            )
        routes.append(route)
        entries.append(
            ArtifactPlanEntry(
                task_id=result.task_id,
                run_id=result.run_id,
                contract_version=result.contract_version,
                created_at=created_at,
                producer_version="1.0.0",
                entry_id=f"ape_{index:032x}",
                object_id=obj.object_id,
                byte_sha256=obj.byte_sha256,
                classification_id=classification.classification_id,
                classification_hash=classification.classification_hash,
                route_ids=(route.route_id,),
                route_hashes=(route.route_hash,),
                status=ParsePlanStatus.SUCCEEDED,
                explanation="The immutable artifact has one complete, explainable route disposition.",
                entry_hash=_hash(400 + index),
            )
        )
    completed = next(
        event
        for event in result.events
        if event.event_type is EventType.ARTIFACT_DOWNLOAD_COMPLETED
    )
    return ParsePlan(
        task_id=result.task_id,
        run_id=result.run_id,
        contract_version=result.contract_version,
        created_at=created_at,
        producer_version="1.0.0",
        plan_id="ppl_00000000000000000000000000000001",
        status=ParsePlanningStatus.SUCCEEDED,
        contract_id=upstream.contract.contract_id,
        contract_hash=upstream.contract.contract_hash,
        artifact_set_hash=result.artifact_set.artifact_set_hash,
        manifest_hash=result.manifest.manifest_hash,
        upstream_download_output_hash=result.output_hash,
        upstream_download_event_id=completed.event_id,
        policy=policy,
        policy_hash=_hash(15),
        capability_registry=registry,
        runtime=runtime,
        source_objects=tuple(source_objects),
        classifications=tuple(classifications),
        routes=tuple(routes),
        entries=tuple(entries),
        plan_hash=_hash(500),
    )


def _metrics(plan: ParsePlan) -> ParsePlanningMetrics:
    return ParsePlanningMetrics(
        artifact_count=len(plan.source_objects),
        classification_count=len(plan.classifications),
        route_count=len(plan.routes),
        page_route_count=0,
        succeeded_plan_count=len(plan.entries),
        partial_plan_count=0,
        review_plan_count=0,
        unsupported_plan_count=0,
        failed_plan_count=0,
        gap_count=0,
        format_gap_count=0,
        capability_gap_count=0,
        model_candidate_classification_count=0,
        high_resource_primary_route_count=0,
        planned_cost_micro_usd=sum(item.max_cost_micro_usd for item in plan.routes),
    )


def _result(upstream: _Upstream) -> ParsePlanningResult:
    plan = _plan(upstream)
    input_hash = _hash(501)
    output_hash = _hash(502)
    idempotency_key = _hash(503)
    payload = ParsePlanCreatedPayload(
        status=plan.status,
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
        output_hash=output_hash,
        idempotency_key=idempotency_key,
    )
    return ParsePlanningResult(
        task_id=plan.task_id,
        run_id=plan.run_id,
        contract_version=plan.contract_version,
        created_at=plan.created_at,
        producer_version=plan.producer_version,
        status=plan.status,
        input_hash=input_hash,
        output_hash=output_hash,
        idempotency_key=idempotency_key,
        plan=plan,
        metrics=_metrics(plan),
        event=EventEnvelope[ParsePlanCreatedPayload](
            event_type=EventType.PARSE_PLAN_CREATED,
            task_id=plan.task_id,
            run_id=plan.run_id,
            occurred_at=plan.created_at,
            schema_version=plan.contract_version,
            producer=ProducerRef(component="parse_planning_service", version=plan.producer_version),
            payload=payload,
            correlation_id=input_hash,
            causation_event_id=plan.upstream_download_event_id,
        ),
    )


def test_m08_request_binds_exact_m07_and_scientific_contract(upstream: _Upstream) -> None:
    plan = _plan(upstream)
    request = ParsePlanningRequest(
        contract=upstream.contract,
        download_request=upstream.download_request,
        download_result=upstream.download_result,
        capability_registry=plan.capability_registry,
        policy=plan.policy,
        runtime=plan.runtime,
        requested_at=plan.created_at,
    )

    assert not request.force_recompute
    tampered = request.model_dump(mode="python")
    tampered["contract"]["contract_hash"] = _hash(900)
    with pytest.raises(ValidationError, match="scientific contract"):
        ParsePlanningRequest.model_validate(tampered)

    reduced_runtime_draft = request.runtime.model_copy(
        update={"remaining_cost_micro_usd": 1, "runtime_hash": "0" * 64}
    )
    reduced_runtime = reduced_runtime_draft.model_copy(
        update={"runtime_hash": calculate_parse_runtime_hash(reduced_runtime_draft)}
    )
    reduced_budget_request = ParsePlanningRequest.model_validate(
        request.model_copy(update={"runtime": reduced_runtime}).model_dump(mode="python")
    )
    assert (
        reduced_budget_request.policy.max_total_planned_cost_micro_usd
        > reduced_budget_request.runtime.remaining_cost_micro_usd
    )


def test_m08_result_is_one_aggregate_plan_with_no_scientific_values(
    upstream: _Upstream,
) -> None:
    result = _result(upstream)

    assert result.module_id == "M08"
    assert len(result.plan.entries) == len(result.plan.source_objects)
    assert result.event.event_type is EventType.PARSE_PLAN_CREATED
    assert result.event.causation_event_id == result.plan.upstream_download_event_id
    classification = result.plan.classifications[0].model_dump(mode="python")
    classification["raw_value"] = 12.3
    with pytest.raises(ValidationError, match="Extra inputs"):
        ArtifactClassification.model_validate(classification)


def test_page_observations_are_ordered_and_derive_aggregate_ratios() -> None:
    features = StructuralFeatures(
        sampled_bytes=100,
        total_pages=2,
        sampled_pages=2,
        pages=(
            PageStructuralFeature(
                page_number=1,
                text_layer_density=1.0,
                scanned_probability=0.0,
                table_probability=0.2,
                figure_probability=0.4,
            ),
            PageStructuralFeature(
                page_number=2,
                text_layer_density=0.0,
                scanned_probability=1.0,
                table_probability=0.8,
                figure_probability=0.6,
            ),
        ),
        text_layer_density=0.5,
        scanned_page_ratio=0.5,
        table_page_ratio=0.5,
        figure_page_ratio=0.5,
    )
    assert features.scanned_page_ratio == 0.5

    tampered = features.model_dump(mode="python")
    tampered["table_page_ratio"] = 0.4
    with pytest.raises(ValidationError, match="page-derived"):
        StructuralFeatures.model_validate(tampered)


def test_every_source_requires_exact_classification_entry_and_lineage(upstream: _Upstream) -> None:
    plan = _plan(upstream).model_dump(mode="python")
    plan["entries"] = plan["entries"][:-1]
    with pytest.raises(ValidationError, match="exactly one classification and plan entry"):
        ParsePlan.model_validate(plan)

    plan = _plan(upstream).model_dump(mode="python")
    plan["classifications"][0]["acquisition_ids"] = plan["source_objects"][1]["acquisition_ids"]
    with pytest.raises(ValidationError, match="immutable source objects"):
        ParsePlan.model_validate(plan)


def test_page_routes_must_cover_known_pages_once_and_in_order(upstream: _Upstream) -> None:
    plan = _plan(upstream).model_dump(mode="python")
    pdf_index = next(
        index
        for index, item in enumerate(plan["classifications"])
        if item["format_family"] is FormatFamily.PDF
    )
    plan["routes"][pdf_index]["scope"] = {
        "kind": ParseScopeKind.PAGE_RANGE,
        "start_page": 2,
        "end_page": 2,
    }
    with pytest.raises(ValidationError, match="ordered, disjoint, and contiguous"):
        ParsePlan.model_validate(plan)


def test_ocr_or_vlm_capability_cannot_be_primary(upstream: _Upstream) -> None:
    plan = _plan(upstream).model_dump(mode="python")
    pdf_index = next(
        index
        for index, item in enumerate(plan["classifications"])
        if item["format_family"] is FormatFamily.PDF
    )
    route = plan["routes"][pdf_index]
    route["primary_parser_id"] = "ocr.document"
    route["fallback_parser_ids"] = ()
    route["resource_tier"] = ResourceTier.HIGH
    route["escalation_rules"] = ()
    with pytest.raises(ValidationError, match="cannot be primary"):
        ParsePlan.model_validate(plan)


def test_fallback_trigger_must_be_declared_by_registry(upstream: _Upstream) -> None:
    plan = _plan(upstream).model_dump(mode="python")
    ocr = next(
        item
        for item in plan["capability_registry"]["parsers"]
        if item["parser_id"] == "ocr.document"
    )
    ocr["fallback_trigger_checks"] = (QualityCheckKind.OUTPUT_SCHEMA,)
    with pytest.raises(ValidationError, match="fallback triggers"):
        ParsePlan.model_validate(plan)


def test_blocked_route_requires_matching_format_or_capability_gap(upstream: _Upstream) -> None:
    plan = _plan(upstream).model_dump(mode="python")
    archive_index = next(
        index
        for index, item in enumerate(plan["classifications"])
        if item["format_family"] is FormatFamily.ARCHIVE
    )
    plan["routes"][archive_index].update(
        disposition=RouteDisposition.UNSUPPORTED,
        blockers=(RouteBlockerCode.UNKNOWN_FORMAT,),
    )
    plan["entries"][archive_index]["status"] = ParsePlanStatus.UNSUPPORTED
    plan["status"] = ParsePlanningStatus.PARTIAL
    with pytest.raises(ValidationError, match="blocked route requires gaps"):
        ParsePlan.model_validate(plan)


def test_result_metrics_and_event_causation_are_derived(upstream: _Upstream) -> None:
    result = _result(upstream).model_dump(mode="python")
    result["metrics"]["route_count"] += 1
    with pytest.raises(ValidationError, match="metrics"):
        ParsePlanningResult.model_validate(result)

    result = _result(upstream).model_dump(mode="python")
    result["event"]["causation_event_id"] = None
    with pytest.raises(ValidationError, match=r"parse\.plan\.created"):
        ParsePlanningResult.model_validate(result)


def test_unknown_classification_requires_explicit_review() -> None:
    with pytest.raises(ValidationError, match="structural uncertainty"):
        ArtifactClassification(
            task_id="tsk_11111111111111111111111111111111",
            run_id="run_11111111111111111111111111111111",
            contract_version="1.0.0",
            created_at=NOW,
            producer_version="1.0.0",
            classification_id="cls_11111111111111111111111111111111",
            object_id="brz_11111111111111111111111111111111",
            byte_sha256=_hash(1),
            object_metadata_hash=_hash(2),
            acquisition_ids=("acq_1111111111111111",),
            artifact_set_hash=_hash(3),
            manifest_hash=_hash(4),
            classified_media_type="application/octet-stream",
            artifact_kind=ArtifactKind.UNKNOWN,
            format_family=FormatFamily.UNKNOWN,
            features=StructuralFeatures(sampled_bytes=0),
            basis=(ClassificationBasis.M07_INSPECTION,),
            confidence=0.0,
            source_media_type_mismatch=False,
            requires_review=True,
            review_codes=(ClassificationReviewCode.LOW_CONFIDENCE,),
            classification_hash=_hash(5),
        )


def test_event_type_and_contracts_forbid_unknown_fields(upstream: _Upstream) -> None:
    assert EventType.PARSE_PLAN_CREATED.value == "parse.plan.created"
    result = _result(upstream).model_dump(mode="python")
    result["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        ParsePlanningResult.model_validate(result)

    assert ParsingGapCode.FORMAT_GAP.value == "format_gap"


def test_structural_feature_contract_rejects_inconsistent_page_evidence() -> None:
    pages = (
        PageStructuralFeature(
            page_number=1,
            text_layer_density=0.2,
            scanned_probability=0.8,
            table_probability=0.1,
            figure_probability=0.3,
        ),
        PageStructuralFeature(
            page_number=2,
            text_layer_density=0.8,
            scanned_probability=0.2,
            table_probability=0.3,
            figure_probability=0.1,
        ),
    )
    valid = StructuralFeatures(
        sampled_bytes=100,
        total_pages=2,
        sampled_pages=2,
        pages=pages,
        text_layer_density=0.5,
        scanned_page_ratio=0.5,
        table_page_ratio=0.2,
        figure_page_ratio=0.2,
    ).model_dump(mode="python")
    invalid_payloads = [
        {**valid, "pages": (pages[0], pages[0])},
        {**valid, "pages": tuple(reversed(pages))},
        {**valid, "sampled_pages": 1},
        {**valid, "total_pages": None},
        {**valid, "total_pages": 1},
        {
            **valid,
            "pages": (pages[0].model_copy(update={"page_number": 3}), pages[1]),
        },
        {**valid, "sampled_pages": 0, "pages": ()},
        {**valid, "text_layer_density": None},
        {**valid, "encrypted": True},
        {**valid, "text_layer_density": 0.4},
    ]

    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            StructuralFeatures.model_validate(payload)

    with pytest.raises(ValidationError):
        StructuralFeatures.model_validate(
            {
                "sampled_bytes": 1,
                "total_pages": 1,
                "sampled_pages": 1,
                "pages": (pages[0],),
                "text_layer_density": None,
                "scanned_page_ratio": None,
                "table_page_ratio": None,
                "figure_page_ratio": None,
                "encrypted": True,
                "damaged": False,
            }
        )


def test_classification_contract_rejects_ambiguous_or_forged_state(
    upstream: _Upstream,
) -> None:
    classification = _plan(upstream).classifications[1]
    valid = classification.model_dump(mode="python")
    invalid_payloads = [
        {**valid, "classified_media_type": "not-a-mime"},
        {**valid, "basis": (ClassificationBasis.MAGIC_BYTES,) * 2},
        {
            **valid,
            "requires_review": True,
            "review_codes": (ClassificationReviewCode.LOW_CONFIDENCE,) * 2,
        },
        {**valid, "acquisition_ids": (valid["acquisition_ids"][0],) * 2},
        {**valid, "requires_review": True, "review_codes": ()},
        {**valid, "source_media_type_mismatch": True},
        {**valid, "features": StructuralFeatures(sampled_bytes=1, encrypted=True)},
        {**valid, "features": StructuralFeatures(sampled_bytes=0)},
        {
            **valid,
            "format_family": FormatFamily.UNKNOWN,
            "artifact_kind": ArtifactKind.UNKNOWN,
            "confidence": 0.5,
            "requires_review": True,
            "review_codes": (ClassificationReviewCode.UNKNOWN_FORMAT,),
        },
        {**valid, "confidence": 0.0},
    ]

    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            ArtifactClassification.model_validate(payload)


def test_registry_policy_and_runtime_contracts_reject_unsafe_snapshots() -> None:
    registry = _registry()
    capability = registry.parsers[0]
    valid_capability = capability.model_dump(mode="python")
    duplicate_fields = {
        "target_modules": (capability.target_modules[0],) * 2,
        "artifact_kinds": (capability.artifact_kinds[0],) * 2,
        "format_families": (capability.format_families[0],) * 2,
        "media_types": (capability.media_types[0],) * 2,
        "quality_checks": (capability.quality_checks[0],) * 2,
        "fallback_trigger_checks": (QualityCheckKind.TEXT_COVERAGE,) * 2,
    }
    for field, value in duplicate_fields.items():
        with pytest.raises(ValidationError):
            ParserCapability.model_validate({**valid_capability, field: value})
    for capability_update in (
        {"media_types": ("not-a-mime",)},
        {"requires_model": True, "deterministic": True},
        {
            "requires_model": True,
            "deterministic": False,
            "primary_eligible": True,
        },
    ):
        with pytest.raises(ValidationError):
            ParserCapability.model_validate({**valid_capability, **capability_update})

    registry_payload = registry.model_dump(mode="python")
    second = registry.parsers[1]
    with pytest.raises(ValidationError):
        ParserCapabilityRegistry.model_validate(
            {**registry_payload, "parsers": (capability, capability)}
        )
    with pytest.raises(ValidationError):
        ParserCapabilityRegistry.model_validate(
            {
                **registry_payload,
                "parsers": (
                    capability,
                    second.model_copy(update={"capability_hash": capability.capability_hash}),
                ),
            }
        )

    policy = _policy().model_dump(mode="python")
    for policy_update in (
        {"allowed_resource_tiers": (ResourceTier.LOW,) * 2},
        {"allowed_resource_tiers": ()},
        {
            "max_route_cost_micro_usd": 101,
            "max_total_planned_cost_micro_usd": 100,
        },
        {
            "allow_external_classifier_network": True,
            "allow_model_classification": False,
        },
    ):
        with pytest.raises(ValidationError):
            ParsePlanningPolicy.model_validate({**policy, **policy_update})

    runtime = _runtime(registry, NOW + timedelta(hours=1)).model_dump(mode="python")
    for runtime_update in (
        {"checked_at": NOW.replace(tzinfo=None)},
        {"available_parser_ids": (capability.parser_id,) * 2},
        {
            "execution_mode": ParsePlanningExecutionMode.MOCK,
            "external_network_enabled": True,
        },
        {
            "execution_mode": ParsePlanningExecutionMode.OFFLINE,
            "model_classification_enabled": True,
        },
    ):
        with pytest.raises(ValidationError):
            ParsePlanningRuntimeSnapshot.model_validate({**runtime, **runtime_update})

    unavailable_runtime = ParsePlanningRuntimeSnapshot.model_validate(
        {**runtime, "available_parser_ids": ()}
    )
    assert unavailable_runtime.available_parser_ids == ()


def test_scope_route_entry_and_lineage_contracts_reject_invalid_shapes(
    upstream: _Upstream,
) -> None:
    plan = _plan(upstream)
    pdf_route = next(item for item in plan.routes if item.fallback_parser_ids)
    route = pdf_route.model_dump(mode="python")
    check = pdf_route.quality_checks[0]
    rule = pdf_route.escalation_rules[0]
    invalid_routes = [
        {**route, "fallback_parser_ids": (rule.fallback_parser_id,) * 2},
        {**route, "blockers": (RouteBlockerCode.POLICY_BLOCKED,) * 2},
        {**route, "quality_checks": (check, check)},
        {**route, "escalation_rules": (rule, rule)},
        {
            **route,
            "fallback_parser_ids": (pdf_route.primary_parser_id,),
            "escalation_rules": (),
        },
        {**route, "primary_parser_id": None},
        {**route, "blockers": (RouteBlockerCode.POLICY_BLOCKED,)},
        {
            **route,
            "disposition": RouteDisposition.NEEDS_REVIEW,
            "blockers": (RouteBlockerCode.POLICY_BLOCKED,),
        },
        {
            **route,
            "disposition": RouteDisposition.NEEDS_REVIEW,
            "target_module": None,
            "primary_parser_id": None,
            "fallback_parser_ids": (),
            "resource_tier": None,
            "quality_checks": (),
            "escalation_rules": (),
            "blockers": (),
            "max_cost_micro_usd": 0,
            "confidence": 0.0,
        },
        {
            **route,
            "disposition": RouteDisposition.METADATA_ONLY,
            "target_module": None,
            "primary_parser_id": None,
            "fallback_parser_ids": (),
            "resource_tier": None,
            "quality_checks": (),
            "escalation_rules": (),
            "blockers": (RouteBlockerCode.POLICY_BLOCKED,),
            "max_cost_micro_usd": 0,
            "confidence": 0.0,
        },
        {
            **route,
            "disposition": RouteDisposition.UNSUPPORTED,
            "target_module": None,
            "primary_parser_id": None,
            "fallback_parser_ids": (),
            "resource_tier": None,
            "quality_checks": (),
            "escalation_rules": (),
            "blockers": (RouteBlockerCode.UNKNOWN_FORMAT,),
            "max_cost_micro_usd": 1,
            "confidence": 0.0,
        },
        {
            **route,
            "escalation_rules": (
                rule.model_copy(update={"trigger_check_id": "pqc_ffffffffffffffff"}),
            ),
        },
        {**route, "escalation_rules": ()},
    ]
    for payload in invalid_routes:
        with pytest.raises(ValidationError):
            ParserRoute.model_validate(payload)

    for payload in (
        {"kind": ParseScopeKind.ARTIFACT, "start_page": 1},
        {"kind": ParseScopeKind.PAGE_RANGE, "start_page": 1},
        {"kind": ParseScopeKind.PAGE_RANGE, "start_page": 2, "end_page": 1},
    ):
        with pytest.raises(ValidationError):
            ParseScope.model_validate(payload)

    entry = plan.entries[0].model_dump(mode="python")
    for update in (
        {"route_ids": (entry["route_ids"][0],) * 2},
        {"route_hashes": (entry["route_hashes"][0],) * 2},
        {"route_hashes": (*entry["route_hashes"], _hash(999))},
    ):
        with pytest.raises(ValidationError):
            ArtifactPlanEntry.model_validate({**entry, **update})

    source = plan.source_objects[1].model_dump(mode="python")
    for update in (
        {"acquisition_ids": (source["acquisition_ids"][0],) * 2},
        {"candidate_ids": (source["candidate_ids"][0],) * 2},
    ):
        with pytest.raises(ValidationError):
            ParseSourceObjectRef.model_validate({**source, **update})


def test_aggregate_plan_rejects_cross_object_tampering_and_unbounded_work(
    upstream: _Upstream,
) -> None:
    plan = _plan(upstream)
    first_classification = plan.classifications[0]
    first_entry = plan.entries[0]
    first_route = plan.routes[0]
    second_source = plan.source_objects[1]
    completed_gap = ParsingGap(
        gap_id="pgp_1111111111111111",
        code=ParsingGapCode.POLICY_GAP,
        object_id=first_route.object_id,
        classification_id=first_route.classification_id,
        route_id="prt_ffffffffffffffffffffffffffffffff",
        detail="invalid route reference",
    )
    low_confidence = first_classification.model_copy(
        update={"confidence": 0.1, "review_codes": (), "requires_review": False}
    )
    model_candidate = first_classification.model_copy(
        update={"basis": (*first_classification.basis, ClassificationBasis.MODEL_CANDIDATE)}
    )
    invalid_plans = [
        plan.model_copy(
            update={
                "classifications": (
                    first_classification.model_copy(
                        update={
                            "created_at": first_classification.created_at + timedelta(seconds=1)
                        }
                    ),
                    *plan.classifications[1:],
                )
            }
        ),
        plan.model_copy(update={"classifications": plan.classifications[:-1]}),
        plan.model_copy(
            update={
                "classifications": (
                    first_classification.model_copy(
                        update={"byte_sha256": second_source.byte_sha256}
                    ),
                    *plan.classifications[1:],
                )
            }
        ),
        plan.model_copy(
            update={
                "classifications": (
                    first_classification.model_copy(
                        update={
                            "features": first_classification.features.model_copy(
                                update={
                                    "sampled_bytes": plan.policy.max_sample_bytes_per_artifact + 1
                                }
                            )
                        }
                    ),
                    *plan.classifications[1:],
                )
            }
        ),
        plan.model_copy(update={"classifications": (low_confidence, *plan.classifications[1:])}),
        plan.model_copy(update={"classifications": (model_candidate, *plan.classifications[1:])}),
        plan.model_copy(
            update={
                "runtime": plan.runtime.model_copy(update={"capability_registry_hash": "f" * 64})
            }
        ),
        plan.model_copy(update={"gaps": (completed_gap,)}),
        plan.model_copy(
            update={
                "entries": (
                    first_entry.model_copy(
                        update={"classification_id": "cls_ffffffffffffffffffffffffffffffff"}
                    ),
                    *plan.entries[1:],
                )
            }
        ),
        plan.model_copy(
            update={
                "entries": (
                    first_entry.model_copy(
                        update={"route_ids": ("prt_ffffffffffffffffffffffffffffffff",)}
                    ),
                    *plan.entries[1:],
                )
            }
        ),
        plan.model_copy(
            update={
                "entries": (
                    first_entry.model_copy(update={"route_hashes": ("f" * 64,)}),
                    *plan.entries[1:],
                )
            }
        ),
        plan.model_copy(
            update={
                "routes": (
                    first_route.model_copy(update={"object_id": second_source.object_id}),
                    *plan.routes[1:],
                )
            }
        ),
        plan.model_copy(
            update={
                "entries": (
                    first_entry.model_copy(update={"status": ParsePlanStatus.PARTIAL}),
                    *plan.entries[1:],
                )
            }
        ),
        plan.model_copy(update={"status": ParsePlanningStatus.PARTIAL}),
        plan.model_copy(
            update={
                "policy": plan.policy.model_copy(
                    update={
                        "max_route_cost_micro_usd": 100,
                        "max_total_planned_cost_micro_usd": 100,
                    }
                )
            }
        ),
    ]

    for invalid in invalid_plans:
        with pytest.raises(ValidationError):
            ParsePlan.model_validate(invalid.model_dump(mode="python"))


def test_scope_parser_gap_and_status_helpers_fail_closed(upstream: _Upstream) -> None:
    plan = _plan(upstream)
    pdf_classification = next(
        item for item in plan.classifications if item.format_family is FormatFamily.PDF
    )
    pdf_route = next(item for item in plan.routes if item.fallback_parser_ids)
    page = pdf_classification.features.pages[0]
    two_page_features = StructuralFeatures(
        sampled_bytes=pdf_classification.features.sampled_bytes,
        total_pages=2,
        sampled_pages=2,
        pages=(page, page.model_copy(update={"page_number": 2})),
        text_layer_density=page.text_layer_density,
        scanned_page_ratio=page.scanned_probability,
        table_page_ratio=page.table_probability,
        figure_page_ratio=page.figure_probability,
    )
    two_page_classification = pdf_classification.model_copy(update={"features": two_page_features})
    page_one = pdf_route.model_copy(
        update={"scope": ParseScope(kind=ParseScopeKind.PAGE_RANGE, start_page=1, end_page=1)}
    )
    page_two = pdf_route.model_copy(
        update={"scope": ParseScope(kind=ParseScopeKind.PAGE_RANGE, start_page=2, end_page=2)}
    )
    with pytest.raises(ValueError, match="blocked by policy"):
        _validate_scope_coverage(
            two_page_classification,
            (page_one, page_two),
            plan.policy.model_copy(update={"allow_page_level_routing": False}),
        )
    with pytest.raises(ValueError, match="cannot be mixed"):
        _validate_scope_coverage(
            two_page_classification,
            (pdf_route, page_two),
            plan.policy,
        )
    with pytest.raises(ValueError, match="known total"):
        _validate_scope_coverage(
            pdf_classification.model_copy(update={"features": StructuralFeatures(sampled_bytes=1)}),
            (page_one, page_two),
            plan.policy,
        )
    with pytest.raises(ValueError, match="contiguous"):
        _validate_scope_coverage(
            two_page_classification,
            (page_two,),
            plan.policy,
        )
    with pytest.raises(ValueError, match="cover every page"):
        _validate_scope_coverage(
            two_page_classification,
            (page_one,),
            plan.policy,
        )

    parser_by_id = {item.parser_id: item for item in plan.capability_registry.parsers}
    available = set(plan.runtime.available_parser_ids)
    assert pdf_route.primary_parser_id is not None
    primary = parser_by_id[pdf_route.primary_parser_id]
    fallback = parser_by_id[pdf_route.fallback_parser_ids[0]]
    parser_cases = [
        (pdf_route, parser_by_id, available - {primary.parser_id}, plan.policy),
        (
            pdf_route,
            {**parser_by_id, primary.parser_id: fallback},
            available,
            plan.policy,
        ),
        (
            pdf_route.model_copy(update={"resource_tier": ResourceTier.HIGH}),
            parser_by_id,
            available,
            plan.policy,
        ),
        (
            pdf_route,
            parser_by_id,
            available,
            plan.policy.model_copy(update={"allowed_resource_tiers": (ResourceTier.HIGH,)}),
        ),
        (
            pdf_route,
            parser_by_id,
            available,
            plan.policy.model_copy(update={"max_route_cost_micro_usd": 50}),
        ),
        (
            pdf_route.model_copy(
                update={
                    "quality_checks": (
                        QualityCheckSpec(
                            check_id="pqc_ffffffffffffffff",
                            kind=QualityCheckKind.FIGURE_GEOMETRY,
                            minimum_score=0.5,
                        ),
                    )
                }
            ),
            parser_by_id,
            available,
            plan.policy,
        ),
        (
            pdf_route.model_copy(update={"target_module": ParserTargetModule.TABLE}),
            parser_by_id,
            available,
            plan.policy,
        ),
        (
            pdf_route,
            parser_by_id,
            available,
            plan.policy.model_copy(update={"allowed_resource_tiers": (ResourceTier.LOW,)}),
        ),
        (
            pdf_route,
            {
                **parser_by_id,
                fallback.parser_id: fallback.model_copy(update={"estimated_cost_micro_usd": 500}),
            },
            available,
            plan.policy,
        ),
        (
            pdf_route.model_copy(
                update={
                    "escalation_rules": (
                        pdf_route.escalation_rules[0].model_copy(
                            update={"resource_tier": ResourceTier.LOW}
                        ),
                    )
                }
            ),
            parser_by_id,
            available,
            plan.policy,
        ),
    ]
    for route, parsers, runtime_ids, policy in parser_cases:
        with pytest.raises(ValueError):
            _validate_parser_route(
                pdf_classification,
                route,
                parsers,
                runtime_ids,
                policy,
            )

    blocked = pdf_route.model_copy(
        update={
            "disposition": RouteDisposition.NEEDS_REVIEW,
            "blockers": (RouteBlockerCode.POLICY_BLOCKED,),
        }
    )
    wrong_gap = ParsingGap(
        gap_id="pgp_2222222222222222",
        code=ParsingGapCode.FORMAT_GAP,
        object_id=blocked.object_id,
        classification_id=blocked.classification_id,
        route_id=blocked.route_id,
        detail="wrong gap",
    )
    with pytest.raises(ValueError, match="exactly explain"):
        _validate_gap_codes(blocked, (wrong_gap,))

    assert _derive_status(()) is ParsePlanStatus.UNSUPPORTED
    assert _derive_status((RouteDisposition.NEEDS_REVIEW,)) is ParsePlanStatus.NEEDS_REVIEW
    assert _derive_status((RouteDisposition.UNSUPPORTED,)) is ParsePlanStatus.UNSUPPORTED
    assert _derive_status((RouteDisposition.FAILED,)) is ParsePlanStatus.FAILED
    assert (
        _derive_status((RouteDisposition.PARSE, RouteDisposition.NEEDS_REVIEW))
        is ParsePlanStatus.PARTIAL
    )

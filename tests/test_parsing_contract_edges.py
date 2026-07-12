from __future__ import annotations

import asyncio
import importlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol, cast

import pytest
from pydantic import ValidationError

from scidatafusion.artifacts.storage import BronzeByteStore, BronzeWriteReceipt, MemoryBronzeStore
from scidatafusion.contracts.artifacts import (
    ArtifactDownloadRequest,
    ArtifactDownloadResult,
    ArtifactKind,
    BronzeObject,
)
from scidatafusion.contracts.events import EventType
from scidatafusion.contracts.parsing import (
    ArtifactClassification,
    ArtifactPlanEntry,
    ClassificationBasis,
    ClassificationReviewCode,
    FormatFamily,
    PageStructuralFeature,
    ParsePlan,
    ParsePlanningExecutionMode,
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
    ResourceTier,
    RouteBlockerCode,
    RouteDisposition,
    StructuralFeatures,
    _derive_status,
    _validate_aggregate_plan,
    _validate_gap_codes,
    _validate_parser_route,
)
from scidatafusion.contracts.scientific import ScientificDataContract
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.parsing import checkpoints as checkpoint_module
from scidatafusion.parsing import integrity as integrity_module
from scidatafusion.parsing.checkpoints import (
    FileSystemParseCheckpointStore,
    MemoryParseCheckpointStore,
)
from scidatafusion.parsing.classifier import ClassificationDecision
from scidatafusion.parsing.router import RouteDecision
from scidatafusion.parsing.service import ParsePlanningService

service_cases: Any = importlib.import_module("test_parse_planning_service")
contract_cases: Any = importlib.import_module("test_parsing_contracts")


class _IaChain(Protocol):
    contract: ScientificDataContract
    download_request: ArtifactDownloadRequest
    download_result: ArtifactDownloadResult
    store: MemoryBronzeStore
    parse_request: ParsePlanningRequest


@dataclass(frozen=True)
class _Upstream:
    contract: ScientificDataContract
    download_request: ArtifactDownloadRequest
    download_result: ArtifactDownloadResult


def _different(value: str) -> str:
    replacement = "0" if value[-1] != "0" else "1"
    return f"{value[:-1]}{replacement}"


@pytest.fixture(scope="module")
def chain() -> _IaChain:
    factory = cast(
        Callable[[], _IaChain],
        service_cases.ia_chain.__wrapped__,
    )
    return factory()


@pytest.fixture(scope="module")
def contract_upstream(chain: _IaChain) -> _Upstream:
    return _Upstream(
        contract=chain.contract,
        download_request=chain.download_request,
        download_result=chain.download_result,
    )


@pytest.fixture(scope="module")
def valid_plan(contract_upstream: _Upstream) -> ParsePlan:
    return cast(ParsePlan, contract_cases._plan(contract_upstream))


@pytest.fixture(scope="module")
def integrity_result(
    chain: _IaChain,
) -> ParsePlanningResult:
    return cast(ParsePlanningResult, service_cases._execute(chain))


def _page(number: int) -> PageStructuralFeature:
    return PageStructuralFeature(
        page_number=number,
        text_layer_density=0.8 if number == 1 else 0.4,
        scanned_probability=0.2 if number == 1 else 0.6,
        table_probability=0.1 if number == 1 else 0.3,
        figure_probability=0.0 if number == 1 else 0.2,
    )


def _feature_payload(
    pages: tuple[PageStructuralFeature, ...],
    *,
    total_pages: int | None,
    sampled_pages: int,
) -> dict[str, object]:
    divisor = len(pages) or 1
    return {
        "sampled_bytes": 128,
        "total_pages": total_pages,
        "sampled_pages": sampled_pages,
        "pages": pages,
        "text_layer_density": sum(item.text_layer_density for item in pages) / divisor,
        "scanned_page_ratio": sum(item.scanned_probability for item in pages) / divisor,
        "table_page_ratio": sum(item.table_probability for item in pages) / divisor,
        "figure_page_ratio": sum(item.figure_probability for item in pages) / divisor,
    }


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            _feature_payload((_page(1), _page(1)), total_pages=2, sampled_pages=2),
            "page numbers must be unique",
        ),
        (
            _feature_payload((_page(2), _page(1)), total_pages=2, sampled_pages=2),
            "ordered by page number",
        ),
        (
            _feature_payload((_page(1), _page(2)), total_pages=2, sampled_pages=1),
            "sampled page count",
        ),
        (
            _feature_payload((_page(1),), total_pages=None, sampled_pages=1),
            "known total page count",
        ),
        (
            _feature_payload((_page(1), _page(2)), total_pages=1, sampled_pages=2),
            "cannot exceed total pages",
        ),
        (
            _feature_payload((_page(2),), total_pages=1, sampled_pages=1),
            "page numbers cannot exceed",
        ),
        (
            {
                "sampled_bytes": 1,
                "total_pages": 1,
                "sampled_pages": 0,
                "pages": (),
                "text_layer_density": 0.5,
            },
            "ratios require at least one",
        ),
        (
            {
                **_feature_payload((_page(1),), total_pages=1, sampled_pages=1),
                "figure_page_ratio": None,
            },
            "require every aggregate",
        ),
        (
            {
                **_feature_payload((_page(1),), total_pages=1, sampled_pages=1),
                "encrypted": True,
            },
            "encrypted artifacts cannot claim page-content",
        ),
        (
            {
                **_feature_payload((_page(1),), total_pages=1, sampled_pages=1),
                "text_layer_density": 0.7,
            },
            "must be page-derived",
        ),
    ],
)
def test_structural_features_fail_closed_on_inconsistent_observations(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        StructuralFeatures.model_validate(payload)


def test_m08_timestamps_require_timezone(valid_plan: ParsePlan, chain: _IaChain) -> None:
    classification = valid_plan.classifications[0]
    classification_payload = classification.model_dump(mode="python")
    classification_payload["created_at"] = classification.created_at.replace(tzinfo=None)
    with pytest.raises(ValidationError, match="timestamp must include a timezone"):
        ArtifactClassification.model_validate(classification_payload)

    runtime_payload = chain.parse_request.runtime.model_dump(mode="python")
    runtime_payload["checked_at"] = chain.parse_request.runtime.checked_at.replace(tzinfo=None)
    with pytest.raises(ValidationError, match="runtime timestamp"):
        ParsePlanningRuntimeSnapshot.model_validate(runtime_payload)

    request_payload = chain.parse_request.model_dump(mode="python")
    request_payload["requested_at"] = chain.parse_request.requested_at.replace(tzinfo=None)
    with pytest.raises(ValidationError, match="request timestamp"):
        ParsePlanningRequest.model_validate(request_payload)


def test_classification_validators_reject_ambiguous_or_untracked_state(
    valid_plan: ParsePlan,
) -> None:
    base = valid_plan.classifications[0]

    invalid_cases: tuple[tuple[dict[str, object], str], ...] = (
        ({"classified_media_type": "application pdf"}, "normalized MIME"),
        ({"basis": (base.basis[0], base.basis[0])}, "basis values must be unique"),
        (
            {
                "requires_review": True,
                "review_codes": (
                    ClassificationReviewCode.LOW_CONFIDENCE,
                    ClassificationReviewCode.LOW_CONFIDENCE,
                ),
            },
            "review codes must be unique",
        ),
        (
            {"acquisition_ids": (base.acquisition_ids[0], base.acquisition_ids[0])},
            "acquisition ids must be unique",
        ),
        ({"requires_review": True, "review_codes": ()}, "review state must match"),
        (
            {
                "features": StructuralFeatures(
                    sampled_bytes=1,
                    encrypted=True,
                    damaged=True,
                ),
                "source_media_type_mismatch": True,
                "requires_review": False,
                "review_codes": (),
            },
            "expose every structural uncertainty",
        ),
        (
            {
                "artifact_kind": ArtifactKind.UNKNOWN,
                "format_family": FormatFamily.UNKNOWN,
                "classified_media_type": "application/octet-stream",
                "confidence": 0.5,
                "requires_review": True,
                "review_codes": (ClassificationReviewCode.UNKNOWN_FORMAT,),
            },
            "unknown classifications cannot claim confidence",
        ),
        ({"confidence": 0.0}, "known classifications require positive confidence"),
    )
    for changes, message in invalid_cases:
        payload = base.model_dump(mode="python")
        payload.update(changes)
        with pytest.raises(ValidationError, match=message):
            ArtifactClassification.model_validate(payload)


def test_parser_capability_and_registry_uniqueness_is_strict(valid_plan: ParsePlan) -> None:
    capability = valid_plan.capability_registry.parsers[0]
    duplicate_cases: tuple[tuple[str, object, str], ...] = (
        (
            "target_modules",
            (capability.target_modules[0], capability.target_modules[0]),
            "target modules",
        ),
        (
            "artifact_kinds",
            (capability.artifact_kinds[0], capability.artifact_kinds[0]),
            "artifact kinds",
        ),
        (
            "format_families",
            (capability.format_families[0], capability.format_families[0]),
            "format families",
        ),
        ("media_types", ("text/plain", "text/plain"), "media types"),
        (
            "quality_checks",
            (capability.quality_checks[0], capability.quality_checks[0]),
            "quality checks",
        ),
        (
            "fallback_trigger_checks",
            (QualityCheckKind.TEXT_COVERAGE, QualityCheckKind.TEXT_COVERAGE),
            "fallback trigger checks",
        ),
    )
    for field, value, message in duplicate_cases:
        payload = capability.model_dump(mode="python")
        payload[field] = value
        with pytest.raises(ValidationError, match=message):
            ParserCapability.model_validate(payload)

    payload = capability.model_dump(mode="python")
    payload["media_types"] = ("application pdf",)
    with pytest.raises(ValidationError, match="normalized MIME"):
        ParserCapability.model_validate(payload)

    payload = capability.model_dump(mode="python")
    payload.update({"requires_model": True, "deterministic": True})
    with pytest.raises(ValidationError, match="deterministic parser"):
        ParserCapability.model_validate(payload)

    payload["deterministic"] = False
    with pytest.raises(ValidationError, match="cannot be primary eligible"):
        ParserCapability.model_validate(payload)

    registry = valid_plan.capability_registry
    second = registry.parsers[1]
    duplicate_id = second.model_copy(update={"parser_id": registry.parsers[0].parser_id})
    with pytest.raises(ValidationError, match="registry ids must be unique"):
        ParserCapabilityRegistry.model_validate(
            registry.model_copy(
                update={"parsers": (registry.parsers[0], duplicate_id, *registry.parsers[2:])}
            ).model_dump(mode="python")
        )

    duplicate_hash = second.model_copy(
        update={"capability_hash": registry.parsers[0].capability_hash}
    )
    with pytest.raises(ValidationError, match="capability hashes must be unique"):
        ParserCapabilityRegistry.model_validate(
            registry.model_copy(
                update={"parsers": (registry.parsers[0], duplicate_hash, *registry.parsers[2:])}
            ).model_dump(mode="python")
        )


def test_policy_and_runtime_permissions_are_internally_consistent(valid_plan: ParsePlan) -> None:
    invalid_policies: tuple[tuple[dict[str, object], str], ...] = (
        (
            {"allowed_resource_tiers": (ResourceTier.LOW, ResourceTier.LOW)},
            "resource tiers must be unique",
        ),
        ({"allowed_resource_tiers": ()}, "at least one parser resource tier"),
        (
            {"max_route_cost_micro_usd": 11, "max_total_planned_cost_micro_usd": 10},
            "per-route cost cannot exceed",
        ),
        (
            {
                "allow_external_classifier_network": True,
                "allow_model_classification": False,
            },
            "requires model classification",
        ),
    )
    for changes, message in invalid_policies:
        with pytest.raises(ValidationError, match=message):
            ParsePlanningPolicy.model_validate(
                valid_plan.policy.model_copy(update=changes).model_dump(mode="python")
            )

    runtime = valid_plan.runtime
    invalid_runtimes: tuple[tuple[dict[str, object], str], ...] = (
        (
            {"available_parser_ids": (runtime.available_parser_ids[0],) * 2},
            "runtime parser ids must be unique",
        ),
        (
            {
                "execution_mode": ParsePlanningExecutionMode.MOCK,
                "external_network_enabled": True,
            },
            "only live M08 execution",
        ),
        (
            {
                "execution_mode": ParsePlanningExecutionMode.OFFLINE,
                "model_classification_enabled": True,
            },
            "offline M08 execution",
        ),
    )
    for changes, message in invalid_runtimes:
        with pytest.raises(ValidationError, match=message):
            ParsePlanningRuntimeSnapshot.model_validate(
                runtime.model_copy(update=changes).model_dump(mode="python")
            )


def _run_request_validator(request: ParsePlanningRequest) -> ParsePlanningRequest:
    validator = cast(Callable[[], ParsePlanningRequest], request.validate_request)
    return validator()


def test_request_validator_rejects_every_cross_snapshot_drift(chain: _IaChain) -> None:
    request = chain.parse_request
    result = request.download_result

    artifact_set = result.artifact_set.model_copy(
        update={"task_id": _different(result.artifact_set.task_id)}
    )
    with pytest.raises(ValueError, match="same task, run, and contract version"):
        _run_request_validator(
            request.model_copy(
                update={"download_result": result.model_copy(update={"artifact_set": artifact_set})}
            )
        )

    selected = request.download_request.selected_source_set.model_copy(
        update={"contract_hash": "f" * 64}
    )
    with pytest.raises(ValueError, match="download request must resolve"):
        _run_request_validator(
            request.model_copy(
                update={
                    "download_request": request.download_request.model_copy(
                        update={"selected_source_set": selected}
                    )
                }
            )
        )

    with pytest.raises(ValueError, match="download result must resolve"):
        _run_request_validator(
            request.model_copy(
                update={
                    "download_result": result.model_copy(
                        update={"run_id": _different(result.run_id)}
                    )
                }
            )
        )

    artifact_set = result.artifact_set.model_copy(update={"artifact_set_hash": "e" * 64})
    with pytest.raises(ValueError, match="share immutable M07 references"):
        _run_request_validator(
            request.model_copy(
                update={"download_result": result.model_copy(update={"artifact_set": artifact_set})}
            )
        )

    run_log = result.run_log.model_copy(update={"runtime_hash": "d" * 64})
    with pytest.raises(ValueError, match="bind the exact M07 request"):
        _run_request_validator(
            request.model_copy(
                update={"download_result": result.model_copy(update={"run_log": run_log})}
            )
        )

    omitted_object_id = result.artifact_set.objects[0].object_id
    manifest = result.manifest.model_copy(
        update={
            "acquisitions": tuple(
                item for item in result.manifest.acquisitions if item.object_id != omitted_object_id
            )
        }
    )
    with pytest.raises(ValueError, match="cover every supplied Bronze object"):
        _run_request_validator(
            request.model_copy(
                update={"download_result": result.model_copy(update={"manifest": manifest})}
            )
        )

    runtime = request.runtime.model_copy(update={"capability_registry_hash": "c" * 64})
    with pytest.raises(ValueError, match="bind the supplied parser registry"):
        _run_request_validator(request.model_copy(update={"runtime": runtime}))

    runtime = request.runtime.model_copy(update={"available_parser_ids": ("missing.parser",)})
    with pytest.raises(ValueError, match="runtime parser ids must resolve"):
        _run_request_validator(request.model_copy(update={"runtime": runtime}))

    runtime = request.runtime.model_copy(
        update={
            "execution_mode": ParsePlanningExecutionMode.LIVE,
            "model_classification_enabled": True,
        }
    )
    with pytest.raises(ValueError, match="model classification is blocked"):
        _run_request_validator(request.model_copy(update={"runtime": runtime}))

    runtime = request.runtime.model_copy(
        update={
            "execution_mode": ParsePlanningExecutionMode.LIVE,
            "external_network_enabled": True,
        }
    )
    with pytest.raises(ValueError, match="external network is blocked"):
        _run_request_validator(request.model_copy(update={"runtime": runtime}))

    runtime = request.runtime.model_copy(update={"remaining_cost_micro_usd": 0})
    validated = _run_request_validator(request.model_copy(update={"runtime": runtime}))
    assert validated.runtime.remaining_cost_micro_usd == 0

    with pytest.raises(ValueError, match="cannot predate"):
        _run_request_validator(
            request.model_copy(
                update={"requested_at": request.contract.created_at - timedelta(seconds=1)}
            )
        )


def test_scope_route_entry_and_lineage_shapes_are_strict(valid_plan: ParsePlan) -> None:
    invalid_scopes: tuple[tuple[dict[str, object], str], ...] = (
        (
            {"kind": ParseScopeKind.ARTIFACT, "start_page": 1},
            "artifact scope cannot claim page bounds",
        ),
        ({"kind": ParseScopeKind.PAGE_RANGE}, "requires both page bounds"),
        (
            {"kind": ParseScopeKind.PAGE_RANGE, "start_page": 2, "end_page": 1},
            "start cannot exceed",
        ),
    )
    for payload, message in invalid_scopes:
        with pytest.raises(ValidationError, match=message):
            ParseScope.model_validate(payload)

    pdf_route = next(route for route in valid_plan.routes if route.fallback_parser_ids)
    simple_route = next(
        route
        for route in valid_plan.routes
        if route.disposition is RouteDisposition.PARSE and not route.fallback_parser_ids
    )
    metadata_route = next(
        route for route in valid_plan.routes if route.disposition is RouteDisposition.METADATA_ONLY
    )
    first_rule = pdf_route.escalation_rules[0]
    route_cases: tuple[tuple[ParserRoute, dict[str, object], str], ...] = (
        (
            pdf_route,
            {"fallback_parser_ids": (pdf_route.fallback_parser_ids[0],) * 2},
            "fallback parser ids must be unique",
        ),
        (
            metadata_route,
            {
                "disposition": RouteDisposition.UNSUPPORTED,
                "blockers": (RouteBlockerCode.UNKNOWN_FORMAT,) * 2,
            },
            "route blockers must be unique",
        ),
        (
            simple_route,
            {"quality_checks": (simple_route.quality_checks[0],) * 2},
            "quality-check ids must be unique",
        ),
        (
            pdf_route,
            {"escalation_rules": (first_rule, first_rule)},
            "escalation-rule ids must be unique",
        ),
        (
            simple_route,
            {"fallback_parser_ids": (simple_route.primary_parser_id,)},
            "primary parser cannot also be a fallback",
        ),
        (simple_route, {"target_module": None}, "parse routes require a target"),
        (
            simple_route,
            {"blockers": (RouteBlockerCode.POLICY_BLOCKED,)},
            "parse routes cannot retain blockers",
        ),
        (
            metadata_route,
            {"primary_parser_id": "generic.document"},
            "non-executable routes cannot claim parser execution details",
        ),
        (
            metadata_route,
            {"disposition": RouteDisposition.NEEDS_REVIEW},
            "blocked routes require a structured blocker",
        ),
        (
            metadata_route,
            {"blockers": (RouteBlockerCode.UNKNOWN_FORMAT,)},
            "metadata-only routes cannot retain blockers",
        ),
        (metadata_route, {"confidence": 0.1}, "cannot claim cost or routing confidence"),
        (
            pdf_route,
            {
                "escalation_rules": (
                    first_rule.model_copy(update={"trigger_check_id": "pqc_ffffffffffffffff"}),
                )
            },
            "reference declared quality checks",
        ),
        (pdf_route, {"escalation_rules": ()}, "fallback parser requires exactly one"),
    )
    for route, changes, message in route_cases:
        payload = route.model_dump(mode="python")
        payload.update(changes)
        with pytest.raises(ValidationError, match=message):
            ParserRoute.model_validate(payload)

    entry = valid_plan.entries[0]
    entry_cases: tuple[tuple[dict[str, object], str], ...] = (
        ({"route_ids": (entry.route_ids[0],) * 2}, "route ids must be unique"),
        ({"route_hashes": (entry.route_hashes[0],) * 2}, "route hashes must be unique"),
        (
            {"route_ids": (entry.route_ids[0], _different(entry.route_ids[0]))},
            "must have equal length",
        ),
    )
    for changes, message in entry_cases:
        payload = entry.model_dump(mode="python")
        payload.update(changes)
        with pytest.raises(ValidationError, match=message):
            ArtifactPlanEntry.model_validate(payload)

    source = valid_plan.source_objects[0]
    source_cases: tuple[tuple[dict[str, object], str], ...] = (
        (
            {"acquisition_ids": (source.acquisition_ids[0],) * 2},
            "acquisition ids must be unique",
        ),
        (
            {"candidate_ids": (source.candidate_ids[0],) * 2},
            "candidate ids must be unique",
        ),
    )
    for changes, message in source_cases:
        payload = source.model_dump(mode="python")
        payload.update(changes)
        with pytest.raises(ValidationError, match=message):
            ParseSourceObjectRef.model_validate(payload)


def _replace_classification(
    plan: ParsePlan,
    original: ArtifactClassification,
    replacement: ArtifactClassification,
) -> ParsePlan:
    return plan.model_copy(
        update={
            "classifications": tuple(
                replacement if item is original else item for item in plan.classifications
            )
        }
    )


def _replace_route(plan: ParsePlan, original: ParserRoute, replacement: ParserRoute) -> ParsePlan:
    return plan.model_copy(
        update={"routes": tuple(replacement if item is original else item for item in plan.routes)}
    )


def _replace_entry(
    plan: ParsePlan,
    original: ArtifactPlanEntry,
    replacement: ArtifactPlanEntry,
) -> ParsePlan:
    return plan.model_copy(
        update={
            "entries": tuple(replacement if item is original else item for item in plan.entries)
        }
    )


def _assert_plan_error(plan: ParsePlan, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _validate_aggregate_plan(plan)


def test_aggregate_plan_rejects_broken_coverage_lineage_and_budget(valid_plan: ParsePlan) -> None:
    first_classification = valid_plan.classifications[0]
    first_entry = valid_plan.entries[0]
    first_route = valid_plan.routes[0]

    changed = first_classification.model_copy(
        update={"created_at": first_classification.created_at + timedelta(seconds=1)}
    )
    _assert_plan_error(
        _replace_classification(valid_plan, first_classification, changed),
        "must share aggregate ParsePlan metadata",
    )

    _assert_plan_error(
        valid_plan.model_copy(
            update={"source_objects": (*valid_plan.source_objects, valid_plan.source_objects[0])}
        ),
        "source object ids must be unique",
    )
    _assert_plan_error(
        valid_plan.model_copy(update={"classifications": valid_plan.classifications[1:]}),
        "requires exactly one classification",
    )

    changed = first_classification.model_copy(update={"byte_sha256": "f" * 64})
    _assert_plan_error(
        _replace_classification(valid_plan, first_classification, changed),
        "exactly reference immutable source objects",
    )

    source = next(
        item
        for item in valid_plan.source_objects
        if item.object_id == first_classification.object_id
    )
    changed = first_classification.model_copy(
        update={
            "features": first_classification.features.model_copy(
                update={"sampled_bytes": source.size_bytes + 1}
            )
        }
    )
    _assert_plan_error(
        _replace_classification(valid_plan, first_classification, changed),
        "samples cannot exceed",
    )

    pdf_classification = next(
        item for item in valid_plan.classifications if item.format_family is FormatFamily.PDF
    )
    changed = pdf_classification.model_copy(
        update={
            "features": pdf_classification.features.model_copy(
                update={"sampled_pages": valid_plan.policy.max_sample_pages_per_artifact + 1}
            )
        }
    )
    _assert_plan_error(
        _replace_classification(valid_plan, pdf_classification, changed),
        "sampled pages cannot exceed policy",
    )

    changed = first_classification.model_copy(update={"confidence": 0.1})
    _assert_plan_error(
        _replace_classification(valid_plan, first_classification, changed),
        "low-confidence classifications require",
    )

    changed = first_classification.model_copy(
        update={"basis": (*first_classification.basis, ClassificationBasis.MODEL_CANDIDATE)}
    )
    _assert_plan_error(
        _replace_classification(valid_plan, first_classification, changed),
        "model classification evidence requires",
    )

    _assert_plan_error(
        valid_plan.model_copy(
            update={
                "runtime": valid_plan.runtime.model_copy(
                    update={"capability_registry_hash": "f" * 64}
                )
            }
        ),
        "runtime must bind its parser registry",
    )
    _assert_plan_error(
        valid_plan.model_copy(
            update={
                "runtime": valid_plan.runtime.model_copy(
                    update={"model_classification_enabled": True}
                )
            }
        ),
        "model classification is blocked",
    )
    _assert_plan_error(
        valid_plan.model_copy(
            update={
                "runtime": valid_plan.runtime.model_copy(update={"external_network_enabled": True})
            }
        ),
        "external classifier network is blocked",
    )

    changed_entry = first_entry.model_copy(update={"classification_hash": "e" * 64})
    _assert_plan_error(
        _replace_entry(valid_plan, first_entry, changed_entry),
        "entries must resolve to their object classification",
    )
    changed_entry = first_entry.model_copy(
        update={
            "route_ids": (_different(first_entry.route_ids[0]),),
            "route_hashes": first_entry.route_hashes,
        }
    )
    _assert_plan_error(
        _replace_entry(valid_plan, first_entry, changed_entry),
        "resolve every declared route",
    )
    changed_entry = first_entry.model_copy(update={"route_hashes": ("d" * 64,)})
    _assert_plan_error(
        _replace_entry(valid_plan, first_entry, changed_entry),
        "route hashes must match",
    )

    changed_route = first_route.model_copy(update={"capability_registry_hash": "c" * 64})
    _assert_plan_error(
        _replace_route(valid_plan, first_route, changed_route),
        "routes must resolve to their entry and registry",
    )

    changed_entry = first_entry.model_copy(update={"status": ParsePlanStatus.NEEDS_REVIEW})
    _assert_plan_error(
        _replace_entry(valid_plan, first_entry, changed_entry),
        "status must be derived from route dispositions",
    )

    orphan_route = first_route.model_copy(
        update={"route_id": _different(first_route.route_id), "route_hash": "b" * 64}
    )
    _assert_plan_error(
        valid_plan.model_copy(update={"routes": (*valid_plan.routes, orphan_route)}),
        "every parser route must belong",
    )

    _assert_plan_error(
        valid_plan.model_copy(update={"status": ParsePlanningStatus.PARTIAL}),
        "aggregate ParsePlan status must be derived",
    )
    _assert_plan_error(
        valid_plan.model_copy(
            update={
                "policy": valid_plan.policy.model_copy(
                    update={"max_total_planned_cost_micro_usd": 1}
                )
            }
        ),
        "cannot exceed policy or runtime budget",
    )


def test_aggregate_plan_rejects_bad_gap_and_page_scope_coverage(valid_plan: ParsePlan) -> None:
    route = valid_plan.routes[0]
    classification = next(
        item
        for item in valid_plan.classifications
        if item.classification_id == route.classification_id
    )
    other_object = next(
        item.object_id for item in valid_plan.source_objects if item.object_id != route.object_id
    )
    unresolved_gap = ParsingGap(
        gap_id="pgp_1111111111111111",
        code=ParsingGapCode.FORMAT_GAP,
        object_id=other_object,
        classification_id=classification.classification_id,
        route_id=route.route_id,
        detail="The gap deliberately disagrees with the referenced route.",
    )
    _assert_plan_error(
        valid_plan.model_copy(update={"gaps": (unresolved_gap,)}),
        "gaps must resolve to a route",
    )

    blocked_route = route.model_copy(
        update={
            "disposition": RouteDisposition.UNSUPPORTED,
            "target_module": None,
            "primary_parser_id": None,
            "fallback_parser_ids": (),
            "resource_tier": None,
            "quality_checks": (),
            "escalation_rules": (),
            "blockers": (RouteBlockerCode.UNKNOWN_FORMAT,),
            "max_cost_micro_usd": 0,
            "confidence": 0.0,
        }
    )
    _assert_plan_error(
        _replace_route(valid_plan, route, blocked_route),
        "every blocked route requires gaps",
    )

    wrong_gap = unresolved_gap.model_copy(
        update={
            "object_id": route.object_id,
            "code": ParsingGapCode.CAPABILITY_GAP,
        }
    )
    blocked_plan = _replace_route(valid_plan, route, blocked_route).model_copy(
        update={"gaps": (wrong_gap,)}
    )
    _assert_plan_error(blocked_plan, "gap codes must exactly explain")

    pdf_classification = next(
        item for item in valid_plan.classifications if item.format_family is FormatFamily.PDF
    )
    pdf_route = next(
        item
        for item in valid_plan.routes
        if item.classification_id == pdf_classification.classification_id
    )
    page_route = pdf_route.model_copy(
        update={"scope": ParseScope(kind=ParseScopeKind.PAGE_RANGE, start_page=1, end_page=1)}
    )
    page_plan = _replace_route(valid_plan, pdf_route, page_route)
    _assert_plan_error(
        page_plan.model_copy(
            update={
                "policy": page_plan.policy.model_copy(update={"allow_page_level_routing": False})
            }
        ),
        "page-level routing is blocked",
    )

    parse_classification_ids = {
        item.classification_id
        for item in valid_plan.routes
        if item.disposition is RouteDisposition.PARSE
    }
    unknown_pages_classification = next(
        item
        for item in valid_plan.classifications
        if item.features.total_pages is None and item.classification_id in parse_classification_ids
    )
    unknown_pages_route = next(
        item
        for item in valid_plan.routes
        if item.classification_id == unknown_pages_classification.classification_id
        and item.disposition is RouteDisposition.PARSE
    )
    changed_route = unknown_pages_route.model_copy(
        update={"scope": ParseScope(kind=ParseScopeKind.PAGE_RANGE, start_page=1, end_page=1)}
    )
    _assert_plan_error(
        _replace_route(valid_plan, unknown_pages_route, changed_route),
        "requires a known total page count",
    )

    two_page_classification = pdf_classification.model_copy(
        update={"features": pdf_classification.features.model_copy(update={"total_pages": 2})}
    )
    incomplete_plan = _replace_classification(
        _replace_route(valid_plan, pdf_route, page_route),
        pdf_classification,
        two_page_classification,
    )
    _assert_plan_error(incomplete_plan, "cover every page exactly once")

    second_page_route = page_route.model_copy(
        update={
            "route_id": _different(page_route.route_id),
            "route_hash": "a" * 64,
            "scope": ParseScope(kind=ParseScopeKind.PAGE_RANGE, start_page=3, end_page=3),
        }
    )
    pdf_entry = next(
        item
        for item in valid_plan.entries
        if item.classification_id == pdf_classification.classification_id
    )
    two_route_entry = pdf_entry.model_copy(
        update={
            "route_ids": (page_route.route_id, second_page_route.route_id),
            "route_hashes": (page_route.route_hash, second_page_route.route_hash),
        }
    )
    noncontiguous = incomplete_plan.model_copy(
        update={
            "routes": (*incomplete_plan.routes, second_page_route),
            "entries": tuple(
                two_route_entry if item is pdf_entry else item for item in incomplete_plan.entries
            ),
        }
    )
    _assert_plan_error(noncontiguous, "ordered, disjoint, and contiguous")


def test_parser_route_capability_validation_covers_policy_and_fallback_edges(
    valid_plan: ParsePlan,
) -> None:
    pdf_route = next(route for route in valid_plan.routes if route.fallback_parser_ids)
    classification = next(
        item
        for item in valid_plan.classifications
        if item.classification_id == pdf_route.classification_id
    )
    parser_by_id = {item.parser_id: item for item in valid_plan.capability_registry.parsers}
    available = set(valid_plan.runtime.available_parser_ids)

    route_cases: tuple[
        tuple[ParserRoute, dict[str, ParserCapability], set[str], ParsePlanningPolicy, str], ...
    ] = (
        (
            pdf_route.model_copy(update={"primary_parser_id": "missing.parser"}),
            parser_by_id,
            available,
            valid_plan.policy,
            "only available registered parsers",
        ),
        (
            pdf_route.model_copy(update={"resource_tier": ResourceTier.MEDIUM}),
            parser_by_id,
            available,
            valid_plan.policy,
            "tier must match its primary parser",
        ),
        (
            pdf_route,
            parser_by_id,
            available,
            valid_plan.policy.model_copy(update={"allowed_resource_tiers": (ResourceTier.HIGH,)}),
            "primary resource tier is blocked",
        ),
        (
            pdf_route.model_copy(
                update={"max_cost_micro_usd": valid_plan.policy.max_route_cost_micro_usd + 1}
            ),
            parser_by_id,
            available,
            valid_plan.policy,
            "cost cap cannot exceed policy",
        ),
        (
            pdf_route.model_copy(
                update={
                    "quality_checks": (
                        pdf_route.quality_checks[0].model_copy(
                            update={"kind": QualityCheckKind.FIGURE_GEOMETRY}
                        ),
                    )
                }
            ),
            parser_by_id,
            available,
            valid_plan.policy,
            "quality checks must be declared",
        ),
        (
            pdf_route.model_copy(update={"target_module": ParserTargetModule.TABLE}),
            parser_by_id,
            available,
            valid_plan.policy,
            "does not support its classified scope",
        ),
        (
            pdf_route,
            parser_by_id,
            available,
            valid_plan.policy.model_copy(update={"allowed_resource_tiers": (ResourceTier.LOW,)}),
            "parser resource tier is blocked",
        ),
        (
            pdf_route.model_copy(update={"max_cost_micro_usd": 50}),
            parser_by_id,
            available,
            valid_plan.policy,
            "cost cap must cover every selected parser",
        ),
        (
            pdf_route.model_copy(update={"resource_tier": ResourceTier.MEDIUM}),
            {
                **parser_by_id,
                cast(str, pdf_route.primary_parser_id): parser_by_id[
                    cast(str, pdf_route.primary_parser_id)
                ].model_copy(update={"resource_tier": ResourceTier.MEDIUM}),
                pdf_route.fallback_parser_ids[0]: parser_by_id[
                    pdf_route.fallback_parser_ids[0]
                ].model_copy(update={"resource_tier": ResourceTier.LOW}),
            },
            available,
            valid_plan.policy,
            "fallback parsers cannot be cheaper",
        ),
        (
            pdf_route.model_copy(
                update={
                    "escalation_rules": (
                        pdf_route.escalation_rules[0].model_copy(
                            update={"resource_tier": ResourceTier.MEDIUM}
                        ),
                    )
                }
            ),
            parser_by_id,
            available,
            valid_plan.policy,
            "escalation tiers must match",
        ),
        (
            pdf_route,
            {
                **parser_by_id,
                pdf_route.fallback_parser_ids[0]: parser_by_id[
                    pdf_route.fallback_parser_ids[0]
                ].model_copy(update={"fallback_trigger_checks": ()}),
            },
            available,
            valid_plan.policy,
            "fallback triggers must be declared",
        ),
    )
    for route, registry, runtime_ids, policy, message in route_cases:
        with pytest.raises(ValueError, match=message):
            _validate_parser_route(classification, route, registry, runtime_ids, policy)


def test_gap_code_and_status_derivation_cover_all_dispositions(valid_plan: ParsePlan) -> None:
    route = valid_plan.routes[0].model_copy(update={"blockers": (RouteBlockerCode.NEEDS_PASSWORD,)})
    gap = ParsingGap(
        gap_id="pgp_2222222222222222",
        code=ParsingGapCode.DAMAGED_INPUT,
        object_id=route.object_id,
        classification_id=route.classification_id,
        route_id=route.route_id,
        detail="The structured gap intentionally does not match the blocker.",
    )
    with pytest.raises(ValueError, match="gap codes must exactly explain"):
        _validate_gap_codes(route, (gap,))

    assert _derive_status(()) is ParsePlanStatus.UNSUPPORTED
    assert _derive_status((RouteDisposition.PARSE,)) is ParsePlanStatus.SUCCEEDED
    assert _derive_status((RouteDisposition.NEEDS_REVIEW,)) is ParsePlanStatus.NEEDS_REVIEW
    assert _derive_status((RouteDisposition.UNSUPPORTED,)) is ParsePlanStatus.UNSUPPORTED
    assert _derive_status((RouteDisposition.FAILED,)) is ParsePlanStatus.FAILED
    assert (
        _derive_status((RouteDisposition.PARSE, RouteDisposition.NEEDS_REVIEW))
        is ParsePlanStatus.PARTIAL
    )


class _InvalidClassifier:
    def classify(
        self,
        obj: BronzeObject,
        content: bytes,
        policy: ParsePlanningPolicy,
    ) -> ClassificationDecision:
        del obj, content, policy
        return cast(ClassificationDecision, object())


class _InvalidRouter:
    def route(
        self,
        classification: ArtifactClassification,
        *,
        size_bytes: int,
        registry: ParserCapabilityRegistry,
        runtime: ParsePlanningRuntimeSnapshot,
        policy: ParsePlanningPolicy,
        remaining_cost_micro_usd: int,
    ) -> tuple[RouteDecision, ...]:
        del classification, size_bytes, registry, runtime, policy, remaining_cost_micro_usd
        raise ValueError("untrusted router adapter returned malformed data")


class _EmptyRouter:
    def route(
        self,
        classification: ArtifactClassification,
        *,
        size_bytes: int,
        registry: ParserCapabilityRegistry,
        runtime: ParsePlanningRuntimeSnapshot,
        policy: ParsePlanningPolicy,
        remaining_cost_micro_usd: int,
    ) -> tuple[RouteDecision, ...]:
        del classification, size_bytes, registry, runtime, policy, remaining_cost_micro_usd
        return ()


class _SecondReadCorruptStore:
    def __init__(self, delegate: MemoryBronzeStore, target_hash: str) -> None:
        self._delegate = delegate
        self._target_hash = target_hash
        self._reads: dict[str, int] = {}

    def put(self, content: bytes) -> BronzeWriteReceipt:
        return self._delegate.put(content)

    def read(self, byte_sha256: str) -> bytes:
        count = self._reads.get(byte_sha256, 0) + 1
        self._reads[byte_sha256] = count
        content = self._delegate.read(byte_sha256)
        if byte_sha256 == self._target_hash and count >= 2:
            return content + b"tampered-after-request-verification"
        return content

    def contains(self, byte_sha256: str) -> bool:
        return self._delegate.contains(byte_sha256)


class _StaticCheckpoint:
    def __init__(self, result: ParsePlanningResult) -> None:
        self._result = result

    def load(self, idempotency_key: str) -> ParsePlanningResult | None:
        del idempotency_key
        return self._result

    def save(self, result: ParsePlanningResult) -> ParsePlanningResult:
        return result


def _assert_service_error(service: ParsePlanningService, request: ParsePlanningRequest) -> AppError:
    with pytest.raises(AppError) as caught:
        asyncio.run(service.execute(request))
    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR
    return caught.value


def test_service_wraps_untrusted_classifier_and_router_failures(
    chain: _IaChain,
) -> None:
    classifier_error = _assert_service_error(
        ParsePlanningService(store=chain.store, classifier=_InvalidClassifier()),
        chain.parse_request,
    )
    assert "classifier returned an invalid" in classifier_error.message

    router_error = _assert_service_error(
        ParsePlanningService(store=chain.store, router=_InvalidRouter()),
        chain.parse_request,
    )
    assert "router returned an invalid" in router_error.message

    omitted_error = _assert_service_error(
        ParsePlanningService(store=chain.store, router=_EmptyRouter()),
        chain.parse_request,
    )
    assert "omitted an artifact disposition" in omitted_error.message


def test_service_detects_post_verification_byte_drift(chain: _IaChain) -> None:
    target_hash = chain.download_result.artifact_set.objects[0].byte_sha256
    store = _SecondReadCorruptStore(chain.store, target_hash)
    error = _assert_service_error(
        ParsePlanningService(store=store),
        chain.parse_request,
    )
    assert "Bronze bytes differ" in error.message


def test_service_rejects_checkpoint_from_another_producer(
    chain: _IaChain,
    integrity_result: ParsePlanningResult,
) -> None:
    foreign = integrity_result.model_copy(update={"producer_version": "9.9.9"})
    service = ParsePlanningService(
        store=chain.store,
        checkpoints=_StaticCheckpoint(foreign),
    )
    error = _assert_service_error(service, chain.parse_request)
    assert "producer does not match" in error.message


def _skip_request_integrity(request: ParsePlanningRequest, store: BronzeByteStore) -> None:
    del request, store


def _assert_result_integrity_error(
    result: ParsePlanningResult,
    chain: _IaChain,
) -> None:
    with pytest.raises(AppError) as caught:
        integrity_module.verify_parse_planning_integrity(result, chain.parse_request, chain.store)
    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_result_integrity_reports_precise_snapshot_and_lineage_failures(
    chain: _IaChain,
    integrity_result: ParsePlanningResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        integrity_module,
        "verify_parse_planning_request_integrity",
        _skip_request_integrity,
    )

    without_completion = chain.download_result.model_copy(
        update={
            "events": tuple(
                item
                for item in chain.download_result.events
                if item.event_type is not EventType.ARTIFACT_DOWNLOAD_COMPLETED
            )
        }
    )
    altered_request = chain.parse_request.model_copy(update={"download_result": without_completion})
    with pytest.raises(AppError, match="exactly one upstream artifact completion"):
        integrity_module.verify_parse_planning_integrity(
            integrity_result,
            altered_request,
            chain.store,
        )

    _assert_result_integrity_error(
        integrity_result.model_copy(update={"input_hash": "f" * 64}),
        chain,
    )

    reversed_sources = tuple(reversed(integrity_result.plan.source_objects))
    _assert_result_integrity_error(
        integrity_result.model_copy(
            update={
                "plan": integrity_result.plan.model_copy(
                    update={"source_objects": reversed_sources}
                )
            }
        ),
        chain,
    )

    source = integrity_result.plan.source_objects[0]
    changed_source = source.model_copy(update={"size_bytes": source.size_bytes + 1})
    changed_sources = (changed_source, *integrity_result.plan.source_objects[1:])
    _assert_result_integrity_error(
        integrity_result.model_copy(
            update={
                "plan": integrity_result.plan.model_copy(update={"source_objects": changed_sources})
            }
        ),
        chain,
    )


def _replace_integrity_route(
    result: ParsePlanningResult,
    original: ParserRoute,
    replacement: ParserRoute,
) -> ParsePlanningResult:
    return result.model_copy(
        update={
            "plan": result.plan.model_copy(
                update={
                    "routes": tuple(
                        replacement if item is original else item for item in result.plan.routes
                    )
                }
            )
        }
    )


def test_result_integrity_rejects_nondeterministic_route_entry_and_gap_data(
    chain: _IaChain,
    integrity_result: ParsePlanningResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        integrity_module,
        "verify_parse_planning_request_integrity",
        _skip_request_integrity,
    )
    route = next(
        item for item in integrity_result.plan.routes if item.disposition is RouteDisposition.PARSE
    )

    check = route.quality_checks[0].model_copy(update={"check_id": "pqc_ffffffffffffffff"})
    _assert_result_integrity_error(
        _replace_integrity_route(
            integrity_result,
            route,
            route.model_copy(update={"quality_checks": (check, *route.quality_checks[1:])}),
        ),
        chain,
    )

    _assert_result_integrity_error(
        _replace_integrity_route(
            integrity_result,
            route,
            route.model_copy(update={"primary_parser_id": None}),
        ),
        chain,
    )

    _assert_result_integrity_error(
        _replace_integrity_route(
            integrity_result,
            route,
            route.model_copy(update={"max_cost_micro_usd": route.max_cost_micro_usd + 1}),
        ),
        chain,
    )

    route_with_fallback = next(
        item for item in integrity_result.plan.routes if item.escalation_rules
    )
    rule = route_with_fallback.escalation_rules[0].model_copy(update={"rule_hash": "e" * 64})
    _assert_result_integrity_error(
        _replace_integrity_route(
            integrity_result,
            route_with_fallback,
            route_with_fallback.model_copy(update={"escalation_rules": (rule,)}),
        ),
        chain,
    )

    entry = integrity_result.plan.entries[0]
    changed_entry = entry.model_copy(update={"entry_hash": "d" * 64})
    _assert_result_integrity_error(
        integrity_result.model_copy(
            update={
                "plan": integrity_result.plan.model_copy(
                    update={"entries": (changed_entry, *integrity_result.plan.entries[1:])}
                )
            }
        ),
        chain,
    )

    classification = next(
        item
        for item in integrity_result.plan.classifications
        if item.classification_id == route.classification_id
    )
    gap = ParsingGap(
        gap_id="pgp_3333333333333333",
        code=ParsingGapCode.INTERNAL_ERROR,
        object_id=route.object_id,
        classification_id=classification.classification_id,
        route_id=route.route_id,
        detail="The supplied id is intentionally not content-derived.",
    )
    _assert_result_integrity_error(
        integrity_result.model_copy(
            update={"plan": integrity_result.plan.model_copy(update={"gaps": (gap,)})}
        ),
        chain,
    )


def test_memory_checkpoint_rejects_invalid_keys_and_conflicts(
    integrity_result: ParsePlanningResult,
) -> None:
    store = MemoryParseCheckpointStore()
    with pytest.raises(AppError) as caught:
        store.load("NOT-A-SHA256")
    assert caught.value.code is ErrorCode.INVALID_REQUEST

    assert store.save(integrity_result) is integrity_result
    conflict = integrity_result.model_copy(update={"output_hash": "f" * 64})
    with pytest.raises(AppError) as caught:
        store.save(conflict)
    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_filesystem_checkpoint_enforces_bounded_metadata(
    integrity_result: ParsePlanningResult,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileSystemParseCheckpointStore(tmp_path / "bounded")
    monkeypatch.setattr(checkpoint_module, "_MAX_CHECKPOINT_BYTES", 1)
    with pytest.raises(AppError) as caught:
        store.save(integrity_result)
    assert caught.value.code is ErrorCode.VALIDATION_FAILED

    target = (
        tmp_path
        / "bounded"
        / integrity_result.idempotency_key[:2]
        / f"{integrity_result.idempotency_key}.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"{}")
    with pytest.raises(AppError) as caught:
        store.load(integrity_result.idempotency_key)
    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_filesystem_checkpoint_maps_initialization_and_persistence_io_errors(
    integrity_result: ParsePlanningResult,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked_root = tmp_path / "blocked"
    original_mkdir = Path.mkdir

    def guarded_mkdir(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if path == blocked_root:
            raise OSError("simulated root initialization failure")
        original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", guarded_mkdir)
    with pytest.raises(AppError) as caught:
        FileSystemParseCheckpointStore(blocked_root)
    assert caught.value.code is ErrorCode.CONFIGURATION_ERROR

    monkeypatch.setattr(Path, "mkdir", original_mkdir)
    store = FileSystemParseCheckpointStore(tmp_path / "persist")

    def fail_link(source: Path | str, destination: Path | str) -> None:
        del source, destination
        raise OSError("simulated atomic publication failure")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(AppError) as caught:
        store.save(integrity_result)
    assert caught.value.code is ErrorCode.INTERNAL_ERROR


def test_filesystem_checkpoint_handles_publish_race_and_verification_loss(
    integrity_result: ParsePlanningResult,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    race_store = FileSystemParseCheckpointStore(tmp_path / "race")

    def race_link(source: Path | str, destination: Path | str) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        destination_path.write_bytes(source_path.read_bytes())
        raise FileExistsError("simulated concurrent publication")

    monkeypatch.setattr(os, "link", race_link)
    assert race_store.save(integrity_result) == integrity_result

    monkeypatch.undo()
    lost_store = FileSystemParseCheckpointStore(tmp_path / "lost")

    def lost_load(idempotency_key: str) -> ParsePlanningResult | None:
        del idempotency_key
        return None

    monkeypatch.setattr(lost_store, "load", lost_load)
    with pytest.raises(AppError, match="could not be verified") as caught:
        lost_store.save(integrity_result)
    assert caught.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR

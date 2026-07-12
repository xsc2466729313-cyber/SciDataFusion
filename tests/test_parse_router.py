from __future__ import annotations

from datetime import UTC, datetime

from scidatafusion.contracts.artifacts import ArtifactKind
from scidatafusion.contracts.parsing import (
    ArtifactClassification,
    ClassificationBasis,
    ClassificationReviewCode,
    FormatFamily,
    PageStructuralFeature,
    ParsePlanningExecutionMode,
    ParsePlanningPolicy,
    ParsePlanningRuntimeSnapshot,
    ParserCapability,
    ParserCapabilityRegistry,
    ParserTargetModule,
    ParseScopeKind,
    QualityCheckKind,
    ResourceTier,
    RouteBlockerCode,
    RouteDisposition,
    StructuralFeatures,
)
from scidatafusion.parsing.registry import load_default_parser_registry
from scidatafusion.parsing.router import RegistryParseRouter, RouteDecision

NOW = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)


def _classification(
    family: FormatFamily,
    media_type: str,
    kind: ArtifactKind,
    *,
    features: StructuralFeatures | None = None,
    review_codes: tuple[ClassificationReviewCode, ...] = (),
) -> ArtifactClassification:
    return ArtifactClassification(
        task_id="tsk_11111111111111111111111111111111",
        run_id="run_11111111111111111111111111111111",
        contract_version="1.0.0",
        created_at=NOW,
        producer_version="1.0.0",
        classification_id="cls_11111111111111111111111111111111",
        object_id="brz_11111111111111111111111111111111",
        byte_sha256="1" * 64,
        object_metadata_hash="2" * 64,
        acquisition_ids=("acq_1111111111111111",),
        artifact_set_hash="3" * 64,
        manifest_hash="4" * 64,
        classified_media_type=media_type,
        artifact_kind=kind,
        format_family=family,
        features=features or StructuralFeatures(sampled_bytes=128),
        basis=(ClassificationBasis.MAGIC_BYTES,),
        confidence=0.0 if family is FormatFamily.UNKNOWN else 1.0,
        source_media_type_mismatch=False,
        requires_review=bool(review_codes),
        review_codes=review_codes,
        classification_hash="5" * 64,
    )


def _runtime(
    registry: ParserCapabilityRegistry,
    *,
    available_parser_ids: tuple[str, ...] | None = None,
    remaining_cost_micro_usd: int = 100_000,
) -> ParsePlanningRuntimeSnapshot:
    return ParsePlanningRuntimeSnapshot(
        execution_mode=ParsePlanningExecutionMode.OFFLINE,
        capability_registry_hash=registry.registry_hash,
        available_parser_ids=(
            available_parser_ids
            if available_parser_ids is not None
            else tuple(item.parser_id for item in registry.parsers)
        ),
        remaining_cost_micro_usd=remaining_cost_micro_usd,
        checked_at=NOW,
        runtime_hash="6" * 64,
    )


def _route(
    classification: ArtifactClassification,
    *,
    registry: ParserCapabilityRegistry | None = None,
    runtime: ParsePlanningRuntimeSnapshot | None = None,
    policy: ParsePlanningPolicy | None = None,
    remaining_cost_micro_usd: int = 100_000,
) -> tuple[RouteDecision, ...]:
    selected_registry = registry or load_default_parser_registry()
    return RegistryParseRouter().route(
        classification,
        size_bytes=1_000,
        registry=selected_registry,
        runtime=runtime or _runtime(selected_registry),
        policy=policy or ParsePlanningPolicy(),
        remaining_cost_micro_usd=remaining_cost_micro_usd,
    )


def test_pdf_uses_low_cost_primary_and_quality_gated_local_ocr_fallback() -> None:
    decisions = _route(
        _classification(
            FormatFamily.PDF,
            "application/pdf",
            ArtifactKind.DOCUMENT,
        )
    )

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.disposition is RouteDisposition.PARSE
    assert decision.target_module is ParserTargetModule.DOCUMENT
    assert decision.primary is not None
    assert decision.primary.parser_id == "m09.pdf_text"
    assert decision.primary.resource_tier is ResourceTier.LOW
    assert [item.capability.parser_id for item in decision.fallbacks] == ["m09.pdf_ocr"]
    assert [item.trigger for item in decision.fallbacks] == [QualityCheckKind.TEXT_COVERAGE]
    assert decision.max_cost_micro_usd == 5_000


def test_archive_container_is_metadata_only_without_parser_or_cost() -> None:
    decision = _route(
        _classification(
            FormatFamily.ARCHIVE,
            "application/zip",
            ArtifactKind.ARCHIVE,
        )
    )[0]

    assert decision.disposition is RouteDisposition.METADATA_ONLY
    assert decision.primary is None
    assert decision.fallbacks == ()
    assert decision.target_module is None
    assert decision.max_cost_micro_usd == 0
    assert decision.blockers == ()


def test_unknown_format_and_missing_capability_are_explicitly_blocked() -> None:
    unknown = _route(
        _classification(
            FormatFamily.UNKNOWN,
            "application/octet-stream",
            ArtifactKind.UNKNOWN,
            review_codes=(ClassificationReviewCode.UNKNOWN_FORMAT,),
        )
    )[0]
    assert unknown.disposition is RouteDisposition.UNSUPPORTED
    assert unknown.blockers == (RouteBlockerCode.UNKNOWN_FORMAT,)

    default = load_default_parser_registry()
    csv_only = next(item for item in default.parsers if item.parser_id == "m10.csv")
    registry = ParserCapabilityRegistry(
        registry_version="1.0.0",
        parsers=(csv_only,),
        registry_hash="7" * 64,
    )
    missing = _route(
        _classification(
            FormatFamily.PDF,
            "application/pdf",
            ArtifactKind.DOCUMENT,
        ),
        registry=registry,
    )[0]
    assert missing.disposition is RouteDisposition.NEEDS_REVIEW
    assert missing.blockers == (RouteBlockerCode.CAPABILITY_MISSING,)


def test_unavailable_parser_and_resource_policy_fail_closed() -> None:
    registry = load_default_parser_registry()
    csv_parser = next(item.parser_id for item in registry.parsers if item.parser_id == "m10.csv")
    classification = _classification(
        FormatFamily.PDF,
        "application/pdf",
        ArtifactKind.DOCUMENT,
    )

    unavailable = _route(
        classification,
        registry=registry,
        runtime=_runtime(registry, available_parser_ids=(csv_parser,)),
    )[0]
    assert unavailable.disposition is RouteDisposition.NEEDS_REVIEW
    assert unavailable.blockers == (RouteBlockerCode.PARSER_UNAVAILABLE,)

    policy_blocked = _route(
        classification,
        registry=registry,
        policy=ParsePlanningPolicy(allowed_resource_tiers=(ResourceTier.MEDIUM,)),
    )[0]
    assert policy_blocked.disposition is RouteDisposition.NEEDS_REVIEW
    assert policy_blocked.blockers == (RouteBlockerCode.POLICY_BLOCKED,)

    all_unavailable = _route(
        classification,
        registry=registry,
        runtime=_runtime(registry, available_parser_ids=()),
    )[0]
    assert all_unavailable.disposition is RouteDisposition.NEEDS_REVIEW
    assert all_unavailable.blockers == (RouteBlockerCode.PARSER_UNAVAILABLE,)


def test_primary_route_over_budget_is_not_planned() -> None:
    default = load_default_parser_registry()
    text_parser = next(item for item in default.parsers if item.parser_id == "m09.text")
    priced = text_parser.model_copy(update={"estimated_cost_micro_usd": 10})
    registry = ParserCapabilityRegistry(
        registry_version="1.0.0",
        parsers=(priced,),
        registry_hash="8" * 64,
    )

    decision = _route(
        _classification(
            FormatFamily.PLAIN_TEXT,
            "text/plain",
            ArtifactKind.DOCUMENT,
        ),
        registry=registry,
        policy=ParsePlanningPolicy(
            max_route_cost_micro_usd=100,
            max_total_planned_cost_micro_usd=100,
        ),
        remaining_cost_micro_usd=9,
    )[0]

    assert decision.disposition is RouteDisposition.NEEDS_REVIEW
    assert decision.blockers == (RouteBlockerCode.BUDGET_EXHAUSTED,)
    assert decision.max_cost_micro_usd == 0


def test_primary_selection_prefers_cost_before_resource_tier_and_budget() -> None:
    default = load_default_parser_registry()
    base = next(item for item in default.parsers if item.parser_id == "m09.text")
    expensive_low = ParserCapability.model_validate(
        base.model_dump(mode="python")
        | {
            "parser_id": "test.expensive_low",
            "resource_tier": ResourceTier.LOW,
            "estimated_cost_micro_usd": 10,
            "capability_hash": "7" * 64,
        }
    )
    affordable_medium = ParserCapability.model_validate(
        base.model_dump(mode="python")
        | {
            "parser_id": "test.affordable_medium",
            "resource_tier": ResourceTier.MEDIUM,
            "estimated_cost_micro_usd": 5,
            "capability_hash": "8" * 64,
        }
    )
    registry = ParserCapabilityRegistry(
        registry_version="1.0.0",
        parsers=(expensive_low, affordable_medium),
        registry_hash="9" * 64,
    )
    runtime = _runtime(registry, remaining_cost_micro_usd=5)

    decision = _route(
        _classification(
            FormatFamily.PLAIN_TEXT,
            "text/plain",
            ArtifactKind.DOCUMENT,
        ),
        registry=registry,
        runtime=runtime,
        policy=ParsePlanningPolicy(
            max_route_cost_micro_usd=100,
            max_total_planned_cost_micro_usd=100,
        ),
    )[0]

    assert decision.disposition is RouteDisposition.PARSE
    assert decision.primary == affordable_medium
    assert decision.max_cost_micro_usd == 5


def test_primary_selection_uses_resource_tier_then_id_for_equal_costs() -> None:
    default = load_default_parser_registry()
    base = next(item for item in default.parsers if item.parser_id == "m09.text")
    capabilities = tuple(
        ParserCapability.model_validate(
            base.model_dump(mode="python")
            | {
                "parser_id": parser_id,
                "resource_tier": tier,
                "estimated_cost_micro_usd": 5,
                "capability_hash": hash_character * 64,
            }
        )
        for parser_id, tier, hash_character in (
            ("test.medium", ResourceTier.MEDIUM, "7"),
            ("test.low_z", ResourceTier.LOW, "8"),
            ("test.low_a", ResourceTier.LOW, "9"),
        )
    )
    registry = ParserCapabilityRegistry(
        registry_version="1.0.0",
        parsers=capabilities,
        registry_hash="a" * 64,
    )

    decision = _route(
        _classification(
            FormatFamily.PLAIN_TEXT,
            "text/plain",
            ArtifactKind.DOCUMENT,
        ),
        registry=registry,
    )[0]

    assert decision.primary is not None
    assert decision.primary.parser_id == "test.low_a"


def test_fallback_must_support_the_primary_selected_target_module() -> None:
    default = load_default_parser_registry()
    primary_base = next(item for item in default.parsers if item.parser_id == "m09.pdf_text")
    fallback_base = next(item for item in default.parsers if item.parser_id == "m09.pdf_ocr")
    primary = ParserCapability.model_validate(
        primary_base.model_dump(mode="python")
        | {
            "parser_id": "test.multitarget_primary",
            "target_modules": (
                ParserTargetModule.DOCUMENT,
                ParserTargetModule.TABLE,
            ),
            "capability_hash": "7" * 64,
        }
    )
    wrong_target_fallback = ParserCapability.model_validate(
        fallback_base.model_dump(mode="python")
        | {
            "parser_id": "test.table_only_fallback",
            "target_modules": (ParserTargetModule.TABLE,),
            "estimated_cost_micro_usd": 1,
            "capability_hash": "8" * 64,
        }
    )
    compatible_fallback = ParserCapability.model_validate(
        fallback_base.model_dump(mode="python")
        | {
            "parser_id": "test.document_fallback",
            "target_modules": (ParserTargetModule.DOCUMENT,),
            "estimated_cost_micro_usd": 2,
            "capability_hash": "9" * 64,
        }
    )
    registry = ParserCapabilityRegistry(
        registry_version="1.0.0",
        parsers=(primary, wrong_target_fallback, compatible_fallback),
        registry_hash="a" * 64,
    )

    decision = _route(
        _classification(
            FormatFamily.PDF,
            "application/pdf",
            ArtifactKind.DOCUMENT,
        ),
        registry=registry,
    )[0]

    assert decision.target_module is ParserTargetModule.DOCUMENT
    assert [item.capability.parser_id for item in decision.fallbacks] == ["test.document_fallback"]
    assert all(
        decision.target_module in item.capability.target_modules for item in decision.fallbacks
    )


def test_mixed_pdf_pages_form_ordered_contiguous_routes() -> None:
    pages = (
        PageStructuralFeature(
            page_number=1,
            text_layer_density=0.90,
            scanned_probability=0.10,
            table_probability=0.0,
            figure_probability=0.0,
        ),
        PageStructuralFeature(
            page_number=2,
            text_layer_density=0.85,
            scanned_probability=0.20,
            table_probability=0.0,
            figure_probability=0.0,
        ),
        PageStructuralFeature(
            page_number=3,
            text_layer_density=0.10,
            scanned_probability=0.90,
            table_probability=0.0,
            figure_probability=0.0,
        ),
        PageStructuralFeature(
            page_number=4,
            text_layer_density=0.05,
            scanned_probability=0.95,
            table_probability=0.0,
            figure_probability=0.0,
        ),
    )
    features = StructuralFeatures(
        sampled_bytes=1_000,
        total_pages=4,
        sampled_pages=4,
        pages=pages,
        text_layer_density=0.475,
        scanned_page_ratio=0.5375,
        table_page_ratio=0.0,
        figure_page_ratio=0.0,
    )

    decisions = _route(
        _classification(
            FormatFamily.PDF,
            "application/pdf",
            ArtifactKind.DOCUMENT,
            features=features,
        ),
        remaining_cost_micro_usd=10_000,
    )

    assert len(decisions) == 2
    assert all(item.scope.kind is ParseScopeKind.PAGE_RANGE for item in decisions)
    assert [(item.scope.start_page, item.scope.end_page) for item in decisions] == [
        (1, 2),
        (3, 4),
    ]
    assert decisions[0].fallbacks == ()
    assert [item.capability.parser_id for item in decisions[1].fallbacks] == ["m09.pdf_ocr"]
    assert sum(item.max_cost_micro_usd for item in decisions) <= 10_000


def test_each_page_group_fails_closed_when_its_remaining_budget_is_exhausted() -> None:
    default = load_default_parser_registry()
    base = next(item for item in default.parsers if item.parser_id == "m09.pdf_text")
    priced = ParserCapability.model_validate(
        base.model_dump(mode="python")
        | {
            "parser_id": "test.priced_pdf",
            "estimated_cost_micro_usd": 6,
            "capability_hash": "7" * 64,
        }
    )
    registry = ParserCapabilityRegistry(
        registry_version="1.0.0",
        parsers=(priced,),
        registry_hash="8" * 64,
    )
    pages = (
        PageStructuralFeature(
            page_number=1,
            text_layer_density=0.9,
            scanned_probability=0.1,
            table_probability=0.0,
            figure_probability=0.0,
        ),
        PageStructuralFeature(
            page_number=2,
            text_layer_density=0.1,
            scanned_probability=0.9,
            table_probability=0.0,
            figure_probability=0.0,
        ),
    )
    features = StructuralFeatures(
        sampled_bytes=1_000,
        total_pages=2,
        sampled_pages=2,
        pages=pages,
        text_layer_density=0.5,
        scanned_page_ratio=0.5,
        table_page_ratio=0.0,
        figure_page_ratio=0.0,
    )

    decisions = _route(
        _classification(
            FormatFamily.PDF,
            "application/pdf",
            ArtifactKind.DOCUMENT,
            features=features,
        ),
        registry=registry,
        remaining_cost_micro_usd=10,
    )

    assert [item.disposition for item in decisions] == [
        RouteDisposition.PARSE,
        RouteDisposition.NEEDS_REVIEW,
    ]
    assert decisions[0].max_cost_micro_usd == 6
    assert decisions[1].blockers == (RouteBlockerCode.BUDGET_EXHAUSTED,)
    assert decisions[1].primary is None
    assert decisions[1].max_cost_micro_usd == 0
    assert sum(item.max_cost_micro_usd for item in decisions) <= 10

"""Registry-driven, low-cost-first parser routing for M08."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from scidatafusion.contracts.parsing import (
    ArtifactClassification,
    ClassificationReviewCode,
    FormatFamily,
    ParsePlanningPolicy,
    ParsePlanningRuntimeSnapshot,
    ParserCapability,
    ParserCapabilityRegistry,
    ParserTargetModule,
    ParseScope,
    ParseScopeKind,
    QualityCheckKind,
    ResourceTier,
    RouteBlockerCode,
    RouteDisposition,
)

_RESOURCE_RANK = {
    ResourceTier.LOW: 0,
    ResourceTier.MEDIUM: 1,
    ResourceTier.HIGH: 2,
}


@dataclass(frozen=True, slots=True)
class FallbackDecision:
    """One registry-backed fallback and the primary quality gate that enables it."""

    capability: ParserCapability
    trigger: QualityCheckKind


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Pure route values used to construct a strict, hash-linked ParserRoute."""

    scope: ParseScope
    disposition: RouteDisposition
    target_module: ParserTargetModule | None
    primary: ParserCapability | None
    fallbacks: tuple[FallbackDecision, ...]
    quality_checks: tuple[QualityCheckKind, ...]
    blockers: tuple[RouteBlockerCode, ...]
    max_cost_micro_usd: int
    confidence: float
    rationale: str


class ParseRouter(Protocol):
    """Plan registered parsers without invoking any downstream parser."""

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
        """Return complete artifact or page-scope decisions for one object."""


class RegistryParseRouter:
    """Select the cheapest compatible primary and conditional registered fallbacks."""

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
        """Build stable routes from classification, registry, runtime, and budget."""

        scope = ParseScope(kind=ParseScopeKind.ARTIFACT)
        remaining_cost_micro_usd = min(
            remaining_cost_micro_usd,
            runtime.remaining_cost_micro_usd,
            policy.max_total_planned_cost_micro_usd,
        )
        blocked = _classification_blocker(classification)
        if blocked is not None:
            disposition = (
                RouteDisposition.UNSUPPORTED
                if blocked is RouteBlockerCode.UNKNOWN_FORMAT
                else RouteDisposition.NEEDS_REVIEW
            )
            return (
                _blocked_decision(
                    scope,
                    disposition=disposition,
                    blockers=(blocked,),
                    rationale="classification_requires_explicit_disposition",
                ),
            )
        if classification.format_family is FormatFamily.ARCHIVE:
            return (
                RouteDecision(
                    scope=scope,
                    disposition=RouteDisposition.METADATA_ONLY,
                    target_module=None,
                    primary=None,
                    fallbacks=(),
                    quality_checks=(),
                    blockers=(),
                    max_cost_micro_usd=0,
                    confidence=0.0,
                    rationale="archive_container_already_expanded_by_m07",
                ),
            )

        matching = tuple(
            item
            for item in registry.parsers
            if classification.artifact_kind in item.artifact_kinds
            and classification.format_family in item.format_families
            and classification.classified_media_type in item.media_types
            and size_bytes <= item.max_input_bytes
        )
        available_ids = set(runtime.available_parser_ids)
        usable = tuple(
            item
            for item in matching
            if item.parser_id in available_ids
            and item.resource_tier in policy.allowed_resource_tiers
            and (not item.requires_network or runtime.external_network_enabled)
        )
        primaries = sorted(
            (item for item in usable if item.primary_eligible),
            key=_capability_sort_key,
        )
        if not primaries:
            if not matching:
                blocker = RouteBlockerCode.CAPABILITY_MISSING
                rationale = "no_registered_parser_supports_classification"
            elif any(item.parser_id not in available_ids for item in matching):
                blocker = RouteBlockerCode.PARSER_UNAVAILABLE
                rationale = "registered_parser_is_unavailable"
            else:
                blocker = RouteBlockerCode.POLICY_BLOCKED
                rationale = "registered_parser_is_blocked_by_policy"
            return (
                _blocked_decision(
                    scope,
                    disposition=RouteDisposition.NEEDS_REVIEW,
                    blockers=(blocker,),
                    rationale=rationale,
                ),
            )

        primary = primaries[0]
        if primary.estimated_cost_micro_usd > min(
            policy.max_route_cost_micro_usd,
            remaining_cost_micro_usd,
        ):
            return (
                _blocked_decision(
                    scope,
                    disposition=RouteDisposition.NEEDS_REVIEW,
                    blockers=(RouteBlockerCode.BUDGET_EXHAUSTED,),
                    rationale="primary_parser_exceeds_remaining_plan_budget",
                ),
            )

        if _needs_page_routes(classification, policy, primary):
            return self._page_routes(
                classification,
                primary=primary,
                usable=usable,
                policy=policy,
                remaining_cost_micro_usd=remaining_cost_micro_usd,
            )
        return (
            _parse_decision(
                scope,
                classification=classification,
                primary=primary,
                usable=usable,
                policy=policy,
                remaining_cost_micro_usd=remaining_cost_micro_usd,
                include_text_fallback=True,
                rationale="registered_low_cost_primary",
            ),
        )

    @staticmethod
    def _page_routes(
        classification: ArtifactClassification,
        *,
        primary: ParserCapability,
        usable: tuple[ParserCapability, ...],
        policy: ParsePlanningPolicy,
        remaining_cost_micro_usd: int,
    ) -> tuple[RouteDecision, ...]:
        pages = classification.features.pages
        groups: list[tuple[int, int, bool]] = []
        start = pages[0].page_number
        end = start
        scanned = pages[0].scanned_probability >= 0.8
        for page in pages[1:]:
            page_scanned = page.scanned_probability >= 0.8
            if page.page_number == end + 1 and page_scanned == scanned:
                end = page.page_number
                continue
            groups.append((start, end, scanned))
            start = page.page_number
            end = page.page_number
            scanned = page_scanned
        groups.append((start, end, scanned))

        decisions: list[RouteDecision] = []
        remaining = remaining_cost_micro_usd
        for start, end, scanned in groups:
            scope = ParseScope(
                kind=ParseScopeKind.PAGE_RANGE,
                start_page=start,
                end_page=end,
            )
            if primary.estimated_cost_micro_usd > min(
                policy.max_route_cost_micro_usd,
                remaining,
            ):
                decisions.append(
                    _blocked_decision(
                        scope,
                        disposition=RouteDisposition.NEEDS_REVIEW,
                        blockers=(RouteBlockerCode.BUDGET_EXHAUSTED,),
                        rationale="page_scope_primary_exceeds_remaining_plan_budget",
                    )
                )
                continue
            decision = _parse_decision(
                scope,
                classification=classification,
                primary=primary,
                usable=usable,
                policy=policy,
                remaining_cost_micro_usd=remaining,
                include_text_fallback=scanned,
                rationale=(
                    "page_scope_requires_text_quality_fallback"
                    if scanned
                    else "page_scope_has_text_layer"
                ),
            )
            decisions.append(decision)
            remaining = max(0, remaining - decision.max_cost_micro_usd)
        return tuple(decisions)


def _classification_blocker(
    classification: ArtifactClassification,
) -> RouteBlockerCode | None:
    codes = set(classification.review_codes)
    if ClassificationReviewCode.UNKNOWN_FORMAT in codes:
        return RouteBlockerCode.UNKNOWN_FORMAT
    if ClassificationReviewCode.NEEDS_PASSWORD in codes:
        return RouteBlockerCode.NEEDS_PASSWORD
    if ClassificationReviewCode.DAMAGED_FILE in codes:
        return RouteBlockerCode.DAMAGED_FILE
    if classification.requires_review:
        return RouteBlockerCode.CLASSIFICATION_REVIEW_REQUIRED
    return None


def _needs_page_routes(
    classification: ArtifactClassification,
    policy: ParsePlanningPolicy,
    primary: ParserCapability,
) -> bool:
    features = classification.features
    if not (
        policy.allow_page_level_routing
        and primary.supports_page_scope
        and features.total_pages is not None
        and features.sampled_pages == features.total_pages
        and features.total_pages > 1
    ):
        return False
    states = {page.scanned_probability >= 0.8 for page in features.pages}
    return len(states) > 1


def _parse_decision(
    scope: ParseScope,
    *,
    classification: ArtifactClassification,
    primary: ParserCapability,
    usable: tuple[ParserCapability, ...],
    policy: ParsePlanningPolicy,
    remaining_cost_micro_usd: int,
    include_text_fallback: bool,
    rationale: str,
) -> RouteDecision:
    target_module = primary.target_modules[0]
    fallback_candidates: list[FallbackDecision] = []
    if include_text_fallback:
        primary_checks = set(primary.quality_checks)
        for capability in sorted(usable, key=_capability_sort_key):
            if capability.parser_id == primary.parser_id or capability.primary_eligible:
                continue
            if _RESOURCE_RANK[capability.resource_tier] < _RESOURCE_RANK[primary.resource_tier]:
                continue
            if target_module not in capability.target_modules:
                continue
            triggers = tuple(
                item for item in capability.fallback_trigger_checks if item in primary_checks
            )
            if triggers:
                fallback_candidates.append(
                    FallbackDecision(capability=capability, trigger=triggers[0])
                )

    route_limit = min(policy.max_route_cost_micro_usd, remaining_cost_micro_usd)
    selected: list[FallbackDecision] = []
    cost = primary.estimated_cost_micro_usd
    for fallback in fallback_candidates:
        next_cost = cost + fallback.capability.estimated_cost_micro_usd
        if next_cost <= route_limit:
            selected.append(fallback)
            cost = next_cost
    return RouteDecision(
        scope=scope,
        disposition=RouteDisposition.PARSE,
        target_module=target_module,
        primary=primary,
        fallbacks=tuple(selected),
        quality_checks=primary.quality_checks,
        blockers=(),
        max_cost_micro_usd=cost,
        confidence=classification.confidence,
        rationale=rationale,
    )


def _blocked_decision(
    scope: ParseScope,
    *,
    disposition: RouteDisposition,
    blockers: tuple[RouteBlockerCode, ...],
    rationale: str,
) -> RouteDecision:
    return RouteDecision(
        scope=scope,
        disposition=disposition,
        target_module=None,
        primary=None,
        fallbacks=(),
        quality_checks=(),
        blockers=blockers,
        max_cost_micro_usd=0,
        confidence=0.0,
        rationale=rationale,
    )


def _capability_sort_key(item: ParserCapability) -> tuple[int, int, str]:
    return (
        item.estimated_cost_micro_usd,
        _RESOURCE_RANK[item.resource_tier],
        item.parser_id,
    )

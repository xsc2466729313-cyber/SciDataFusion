"""Pure M06 projections from M04 coverage cells and M05 candidate metadata."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from scidatafusion.contracts.connectors import (
    AccessStatus,
    CandidateIdentifier,
    CoverageAssessment,
    IdentifierKind,
    SourceCandidate,
)
from scidatafusion.contracts.search import QueryFamilyState, SearchPlan, SourceCategory
from scidatafusion.contracts.selection import (
    CandidateCoverageState,
    LicenseDecision,
    SelectedCoverageClaim,
    SourceSelectionPolicy,
)


def candidate_claims(
    candidate: SourceCandidate,
    plan: SearchPlan,
    policy: SourceSelectionPolicy,
) -> tuple[SelectedCoverageClaim, ...]:
    """Project evidence-backed candidate claims through exact M04 coverage cells."""

    projected: list[SelectedCoverageClaim] = []
    for claim in candidate.coverage_claims:
        state = claim_coverage_state(
            assessment=claim.assessment,
            confidence=claim.confidence,
            policy=policy,
        )
        if state is None:
            continue
        cells = tuple(
            cell
            for cell in plan.coverage_matrix.cells
            if cell.field_name == claim.field_name
            and set(cell.source_ids).intersection(candidate.source_ids)
        )
        contract_source_types = _unique(cell.contract_source_type for cell in cells)
        source_ids = _unique(
            source_id
            for source_id in candidate.source_ids
            if any(source_id in cell.source_ids for cell in cells)
        )
        if not contract_source_types or not source_ids:
            continue
        projected.append(
            SelectedCoverageClaim(
                field_name=claim.field_name,
                state=state,
                assessment=claim.assessment,
                confidence=claim.confidence,
                basis=claim.basis,
                evidence_ids=claim.evidence_ids,
                contract_source_types=contract_source_types,
                source_ids=source_ids,
            )
        )
    return tuple(projected)


def claim_coverage_state(
    *,
    assessment: CoverageAssessment,
    confidence: float,
    policy: SourceSelectionPolicy,
) -> CandidateCoverageState | None:
    """Classify a discovery claim without upgrading unknown evidence."""

    if assessment is CoverageAssessment.UNKNOWN:
        return None
    if confidence >= policy.minimum_claim_confidence:
        return CandidateCoverageState.CANDIDATE_COVERED
    if confidence >= policy.uncertain_claim_confidence:
        return CandidateCoverageState.UNCERTAIN
    return None


def candidate_download_locators(candidate: SourceCandidate) -> tuple[CandidateIdentifier, ...]:
    """Retain only normalized candidate identifiers and validated HTTPS landing URLs."""

    locators = [*candidate.identifiers]
    locators.extend(
        CandidateIdentifier(kind=IdentifierKind.URL, value=value)
        for value in candidate.landing_urls
    )
    seen: set[tuple[IdentifierKind, str]] = set()
    result: list[CandidateIdentifier] = []
    for locator in locators:
        key = (locator.kind, locator.value)
        if key not in seen:
            seen.add(key)
            result.append(locator)
    return tuple(result)


def applicable_categories(plan: SearchPlan) -> tuple[SourceCategory, ...]:
    """Return distinct categories that have an available or budget-deferred query family."""

    return _unique(
        family.category
        for family in plan.query_family_set.families
        if family.state is not QueryFamilyState.CAPABILITY_UNAVAILABLE
    )


def assess_license(candidate: SourceCandidate) -> tuple[LicenseDecision, str]:
    """Apply a conservative redistribution decision to normalized discovery metadata."""

    components = {item.name: item for item in candidate.assessment.components}
    license_score = components.get("license_clarity")
    if (
        candidate.access_statuses == (AccessStatus.OPEN,)
        and candidate.license_labels
        and license_score is not None
        and license_score.value == 1.0
    ):
        return (
            LicenseDecision.ALLOWED,
            "All observed access metadata is open and every license label is recognized as open.",
        )
    if (
        AccessStatus.RESTRICTED in candidate.access_statuses
        and AccessStatus.OPEN not in candidate.access_statuses
    ):
        return (
            LicenseDecision.RESTRICTED,
            "Observed access metadata is restricted and no open replica is known.",
        )
    return (
        LicenseDecision.NEEDS_REVIEW,
        "Redistribution permission is not established by the available discovery metadata.",
    )


_T = TypeVar("_T")


def _unique(values: Iterable[_T]) -> tuple[_T, ...]:
    return tuple(dict.fromkeys(values))

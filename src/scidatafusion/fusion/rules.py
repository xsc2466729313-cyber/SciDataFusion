"""Pure first-slice M17 reconciliation rules."""

from __future__ import annotations

from scidatafusion.contracts.fusion import FusionCandidate, FusionDecision


def candidate_comparison_hash(candidate: FusionCandidate) -> str:
    """Return the normalized hash when available, otherwise the immutable raw hash."""

    return candidate.normalized_value_sha256 or candidate.raw_value_sha256


def decide_candidates(
    candidates: tuple[FusionCandidate, ...],
) -> tuple[FusionDecision, FusionCandidate | None, bool]:
    """Select only a single eligible value or exact eligible consensus."""

    if not candidates:
        raise ValueError("M17 field decisions require at least one candidate")
    hashes = {candidate_comparison_hash(item) for item in candidates}
    if all(item.eligible_for_gold for item in candidates):
        if len(candidates) == 1:
            return FusionDecision.SINGLE_ELIGIBLE, candidates[0], False
        if len(hashes) == 1:
            return FusionDecision.EXACT_CONSENSUS, candidates[0], False
    if len(candidates) > 1 and len(hashes) > 1:
        return FusionDecision.UNRESOLVED_CONFLICT, None, True
    return FusionDecision.WITHHELD_REVIEW, None, False

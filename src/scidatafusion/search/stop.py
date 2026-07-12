"""Pure, unit-testable search stopping policy for M04 and M06."""

from __future__ import annotations

from scidatafusion.contracts.search import (
    SearchProgressSnapshot,
    SearchStopDecision,
    SearchStopOutcome,
    SearchStopPolicySpec,
    SearchStopReason,
)


class SearchStopPolicy:
    """Evaluate hard resource limits before evidence-saturation conditions."""

    @staticmethod
    def evaluate(
        spec: SearchStopPolicySpec,
        progress: SearchProgressSnapshot,
    ) -> SearchStopDecision:
        """Return one deterministic stop/continue decision for a progress snapshot."""

        hard_reason: tuple[SearchStopReason, str] | None = None
        if progress.cancelled:
            hard_reason = (SearchStopReason.CANCELLED, "The task was cancelled.")
        elif progress.consumed_cost_micro_usd >= spec.max_cost_micro_usd:
            hard_reason = (SearchStopReason.COST_LIMIT, "The search cost ceiling was reached.")
        elif progress.elapsed_seconds >= spec.max_duration_seconds:
            hard_reason = (
                SearchStopReason.DURATION_LIMIT,
                "The search duration ceiling was reached.",
            )
        elif progress.downloaded_bytes >= spec.max_download_bytes:
            hard_reason = (
                SearchStopReason.DOWNLOAD_LIMIT,
                "The download byte ceiling was reached.",
            )
        elif progress.model_tokens >= spec.max_model_tokens:
            hard_reason = (
                SearchStopReason.MODEL_USAGE_LIMIT,
                "The model token ceiling was reached.",
            )
        elif progress.completed_rounds >= spec.max_search_rounds:
            hard_reason = (
                SearchStopReason.SEARCH_ROUND_LIMIT,
                "The maximum number of search rounds was reached.",
            )
        if hard_reason is not None:
            complete = SearchStopPolicy._coverage_ready(spec, progress)
            return SearchStopDecision(
                should_stop=True,
                reason=hard_reason[0],
                outcome=(SearchStopOutcome.SUCCEEDED if complete else SearchStopOutcome.PARTIAL),
                detail=hard_reason[1],
            )

        window = spec.stagnation_rounds
        stagnant = (
            len(progress.recent_marginal_gains) >= window
            and len(progress.recent_new_source_counts) >= window
            and all(
                gain < spec.marginal_gain_threshold
                for gain in progress.recent_marginal_gains[-window:]
            )
            and all(
                count <= spec.max_new_sources_when_stagnant
                for count in progress.recent_new_source_counts[-window:]
            )
        )
        if SearchStopPolicy._coverage_ready(spec, progress) and stagnant:
            return SearchStopDecision(
                should_stop=True,
                reason=SearchStopReason.COVERAGE_SATURATED,
                outcome=SearchStopOutcome.SUCCEEDED,
                detail="Coverage targets are met and two recent rounds are below marginal gain.",
            )
        return SearchStopDecision(
            should_stop=False,
            reason=SearchStopReason.CONTINUE_SEARCH,
            outcome=SearchStopOutcome.CONTINUE,
            detail="Hard limits and evidence-saturation conditions have not been reached.",
        )

    @staticmethod
    def _coverage_ready(
        spec: SearchStopPolicySpec,
        progress: SearchProgressSnapshot,
    ) -> bool:
        return (
            progress.critical_gap_count == 0
            and progress.required_field_coverage >= spec.required_coverage_threshold
            and progress.source_category_coverage >= spec.source_category_coverage_threshold
            and (progress.has_primary_source or not spec.require_primary_source)
        )

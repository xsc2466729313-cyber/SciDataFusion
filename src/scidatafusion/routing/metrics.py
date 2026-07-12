"""Automatically calculated benchmark metrics for M02 routing."""

from __future__ import annotations

from collections.abc import Sequence

from scidatafusion.contracts.routing import RoutingDecision, RoutingMetrics, RoutingStatus


def calculate_routing_metrics(
    decisions: Sequence[RoutingDecision],
    expected_domains: Sequence[str],
    expected_archetypes: Sequence[set[str]],
    expected_unsupported: Sequence[bool],
) -> RoutingMetrics:
    """Calculate domain accuracy, multi-label macro-F1, and unsupported recall."""

    size = len(decisions)
    if not (
        len(expected_domains) == size
        and len(expected_archetypes) == size
        and len(expected_unsupported) == size
    ):
        msg = "decisions and all expected-label sequences must have equal lengths"
        raise ValueError(msg)
    if size == 0:
        return RoutingMetrics(
            sample_count=0,
            domain_accuracy=0.0,
            archetype_macro_f1=0.0,
            unsupported_recall=0.0,
        )

    correct_domains = sum(
        decision.domain_profile.primary_domain == expected
        for decision, expected in zip(decisions, expected_domains, strict=True)
    )
    labels = sorted(
        set().union(
            *(set(decision.task_archetypes.archetypes) for decision in decisions),
            *expected_archetypes,
        )
    )
    f1_values: list[float] = []
    for label in labels:
        true_positive = false_positive = false_negative = 0
        for decision, expected in zip(decisions, expected_archetypes, strict=True):
            predicted = label in decision.task_archetypes.archetypes
            actual = label in expected
            true_positive += int(predicted and actual)
            false_positive += int(predicted and not actual)
            false_negative += int(not predicted and actual)
        denominator = 2 * true_positive + false_positive + false_negative
        f1_values.append(0.0 if denominator == 0 else 2 * true_positive / denominator)

    unsupported_positives = sum(expected_unsupported)
    unsupported_true_positives = sum(
        expected and decision.status == RoutingStatus.UNSUPPORTED
        for decision, expected in zip(decisions, expected_unsupported, strict=True)
    )
    return RoutingMetrics(
        sample_count=size,
        domain_accuracy=correct_domains / size,
        archetype_macro_f1=sum(f1_values) / len(f1_values) if f1_values else 0.0,
        unsupported_recall=(
            unsupported_true_positives / unsupported_positives if unsupported_positives else 0.0
        ),
    )

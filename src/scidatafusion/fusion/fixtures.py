"""Deterministic offline M17 policy and runtime snapshot."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.fusion import (
    FusionExecutionMode,
    FusionPolicy,
    FusionRuleDescriptor,
    FusionRuntimeSnapshot,
)
from scidatafusion.fusion.integrity import calculate_fusion_rule_hash, calculate_fusion_runtime_hash


@dataclass(frozen=True, slots=True)
class OfflineFusionBundle:
    policy: FusionPolicy
    runtime: FusionRuntimeSnapshot


def build_offline_fusion_bundle(
    *, not_before: datetime, clock: Callable[[], datetime] = utc_now
) -> OfflineFusionBundle:
    checked_at = clock()
    if checked_at < not_before:
        raise ValueError("M17 runtime cannot predate its upstream entity-resolution result")
    rule_draft = FusionRuleDescriptor(
        rule_id="m17.exact_consensus_preserve_conflicts",
        rule_version="1.0.0",
        rule_hash="0" * 64,
    )
    rule = rule_draft.model_copy(update={"rule_hash": calculate_fusion_rule_hash(rule_draft)})
    runtime_draft = FusionRuntimeSnapshot(
        execution_mode=FusionExecutionMode.OFFLINE,
        rule=rule,
        checked_at=checked_at,
        runtime_hash="0" * 64,
    )
    runtime = runtime_draft.model_copy(
        update={"runtime_hash": calculate_fusion_runtime_hash(runtime_draft)}
    )
    return OfflineFusionBundle(policy=FusionPolicy(), runtime=runtime)

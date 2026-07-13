"""Deterministic offline M16 policy and runtime snapshot."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.entity_resolution import (
    EntityExecutionMode,
    EntityResolutionPolicy,
    EntityRuleDescriptor,
    EntityRuntimeSnapshot,
)
from scidatafusion.entity_resolution.integrity import (
    calculate_entity_rule_hash,
    calculate_entity_runtime_hash,
)


@dataclass(frozen=True, slots=True)
class OfflineEntityResolutionBundle:
    policy: EntityResolutionPolicy
    runtime: EntityRuntimeSnapshot


def build_offline_entity_resolution_bundle(
    *, not_before: datetime, clock: Callable[[], datetime] = utc_now
) -> OfflineEntityResolutionBundle:
    checked_at = clock()
    if checked_at < not_before:
        raise ValueError("M16 runtime cannot predate its upstream normalization result")
    rule_draft = EntityRuleDescriptor(
        rule_id="m16.exact_stable_identifier",
        rule_version="1.0.0",
        rule_hash="0" * 64,
    )
    rule = rule_draft.model_copy(update={"rule_hash": calculate_entity_rule_hash(rule_draft)})
    runtime_draft = EntityRuntimeSnapshot(
        execution_mode=EntityExecutionMode.OFFLINE,
        rule=rule,
        checked_at=checked_at,
        runtime_hash="0" * 64,
    )
    runtime = runtime_draft.model_copy(
        update={"runtime_hash": calculate_entity_runtime_hash(runtime_draft)}
    )
    return OfflineEntityResolutionBundle(policy=EntityResolutionPolicy(), runtime=runtime)

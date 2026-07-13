"""Deterministic offline M14 policy and rule snapshot."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.mapping import (
    MappingExecutionMode,
    MappingPolicy,
    MappingRuleDescriptor,
    MappingRuntimeSnapshot,
)
from scidatafusion.mapping.integrity import (
    calculate_mapping_rule_hash,
    calculate_mapping_runtime_hash,
)


@dataclass(frozen=True, slots=True)
class OfflineMappingBundle:
    policy: MappingPolicy
    runtime: MappingRuntimeSnapshot


def build_offline_mapping_bundle(
    *,
    not_before: datetime,
    clock: Callable[[], datetime] = utc_now,
) -> OfflineMappingBundle:
    """Build the versioned no-model exact-contract mapping runtime."""

    checked_at = clock()
    if checked_at < not_before:
        raise ValueError("M14 runtime cannot predate its upstream extraction result")
    rule_draft = MappingRuleDescriptor(
        rule_id="m14.exact_contract_field",
        rule_version="1.0.0",
        rule_hash="0" * 64,
    )
    rule = rule_draft.model_copy(update={"rule_hash": calculate_mapping_rule_hash(rule_draft)})
    runtime_draft = MappingRuntimeSnapshot(
        execution_mode=MappingExecutionMode.OFFLINE,
        rule=rule,
        checked_at=checked_at,
        runtime_hash="0" * 64,
    )
    runtime = runtime_draft.model_copy(
        update={"runtime_hash": calculate_mapping_runtime_hash(runtime_draft)}
    )
    return OfflineMappingBundle(policy=MappingPolicy(), runtime=runtime)

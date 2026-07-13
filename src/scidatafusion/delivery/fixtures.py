"""Deterministic M20 policy and runtime fixture."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.delivery import (
    DeliveryExecutionMode,
    DeliveryPolicy,
    DeliveryRuleDescriptor,
    DeliveryRuntimeSnapshot,
)
from scidatafusion.delivery.integrity import (
    calculate_delivery_rule_hash,
    calculate_delivery_runtime_hash,
)


@dataclass(frozen=True, slots=True)
class OfflineDeliveryBundle:
    policy: DeliveryPolicy
    runtime: DeliveryRuntimeSnapshot


def build_offline_delivery_bundle(
    *,
    not_before: datetime,
    code_revision: str = "working-tree",
    clock: Callable[[], datetime] = utc_now,
) -> OfflineDeliveryBundle:
    checked_at = clock()
    if checked_at < not_before:
        raise ValueError("M20 runtime cannot predate M19")
    rule_draft = DeliveryRuleDescriptor(
        rule_id="m20.quality_gated_reproduction",
        rule_version="1.0.0",
        rule_hash="0" * 64,
    )
    rule = rule_draft.model_copy(update={"rule_hash": calculate_delivery_rule_hash(rule_draft)})
    runtime_draft = DeliveryRuntimeSnapshot(
        execution_mode=DeliveryExecutionMode.OFFLINE,
        rule=rule,
        code_revision=code_revision,
        parser_version="1.0.0",
        checked_at=checked_at,
        runtime_hash="0" * 64,
    )
    runtime = runtime_draft.model_copy(
        update={"runtime_hash": calculate_delivery_runtime_hash(runtime_draft)}
    )
    return OfflineDeliveryBundle(policy=DeliveryPolicy(), runtime=runtime)

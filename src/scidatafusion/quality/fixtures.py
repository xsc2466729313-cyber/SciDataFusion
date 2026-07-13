"""Deterministic offline M18 policy and runtime snapshot."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.quality import (
    QualityAuditPolicy,
    QualityExecutionMode,
    QualityRuleDescriptor,
    QualityRuntimeSnapshot,
)
from scidatafusion.quality.integrity import (
    calculate_quality_rule_hash,
    calculate_quality_runtime_hash,
)


@dataclass(frozen=True, slots=True)
class OfflineQualityBundle:
    policy: QualityAuditPolicy
    runtime: QualityRuntimeSnapshot


def build_offline_quality_bundle(
    *, not_before: datetime, clock: Callable[[], datetime] = utc_now
) -> OfflineQualityBundle:
    checked_at = clock()
    if checked_at < not_before:
        raise ValueError("M18 runtime cannot predate its upstream fusion result")
    rule_draft = QualityRuleDescriptor(
        rule_id="m18.contract_quality_gates",
        rule_version="1.0.0",
        rule_hash="0" * 64,
    )
    rule = rule_draft.model_copy(update={"rule_hash": calculate_quality_rule_hash(rule_draft)})
    runtime_draft = QualityRuntimeSnapshot(
        execution_mode=QualityExecutionMode.OFFLINE,
        rule=rule,
        checked_at=checked_at,
        runtime_hash="0" * 64,
    )
    runtime = runtime_draft.model_copy(
        update={"runtime_hash": calculate_quality_runtime_hash(runtime_draft)}
    )
    return OfflineQualityBundle(policy=QualityAuditPolicy(), runtime=runtime)

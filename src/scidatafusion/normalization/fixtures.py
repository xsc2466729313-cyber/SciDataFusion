"""Deterministic offline M15 policy and runtime snapshot."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.normalization import (
    NormalizationExecutionMode,
    NormalizationPolicy,
    NormalizationRuleDescriptor,
    NormalizationRuntimeSnapshot,
)
from scidatafusion.normalization.integrity import (
    calculate_normalization_rule_hash,
    calculate_normalization_runtime_hash,
)


@dataclass(frozen=True, slots=True)
class OfflineNormalizationBundle:
    policy: NormalizationPolicy
    runtime: NormalizationRuntimeSnapshot


def build_offline_normalization_bundle(
    *, not_before: datetime, clock: Callable[[], datetime] = utc_now
) -> OfflineNormalizationBundle:
    checked_at = clock()
    if checked_at < not_before:
        raise ValueError("M15 runtime cannot predate its upstream mapping result")
    rule_draft = NormalizationRuleDescriptor(
        rule_id="m15.exact_decimal_no_guess", rule_version="1.0.0", rule_hash="0" * 64
    )
    rule = rule_draft.model_copy(
        update={"rule_hash": calculate_normalization_rule_hash(rule_draft)}
    )
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    runtime_draft = NormalizationRuntimeSnapshot(
        execution_mode=NormalizationExecutionMode.OFFLINE,
        rule=rule,
        decimal_library_version=version,
        checked_at=checked_at,
        runtime_hash="0" * 64,
    )
    runtime = runtime_draft.model_copy(
        update={"runtime_hash": calculate_normalization_runtime_hash(runtime_draft)}
    )
    return OfflineNormalizationBundle(policy=NormalizationPolicy(), runtime=runtime)

"""Deterministic offline M13 policy and rule snapshot."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.extraction import (
    ExtractionExecutionMode,
    ExtractionPolicy,
    ExtractionRuleDescriptor,
    ExtractionRuntimeSnapshot,
)
from scidatafusion.extraction.integrity import (
    calculate_extraction_rule_hash,
    calculate_extraction_runtime_hash,
)


@dataclass(frozen=True, slots=True)
class OfflineExtractionBundle:
    policy: ExtractionPolicy
    runtime: ExtractionRuntimeSnapshot


def build_offline_extraction_bundle(
    *,
    not_before: datetime,
    clock: Callable[[], datetime] = utc_now,
) -> OfflineExtractionBundle:
    """Build the versioned no-model exact-header table extraction runtime."""

    checked_at = clock()
    if checked_at < not_before:
        raise ValueError("M13 runtime cannot predate its upstream table result")
    rule_draft = ExtractionRuleDescriptor(
        rule_id="m13.table_exact_header",
        rule_version="1.0.0",
        rule_hash="0" * 64,
    )
    rule = rule_draft.model_copy(update={"rule_hash": calculate_extraction_rule_hash(rule_draft)})
    runtime_draft = ExtractionRuntimeSnapshot(
        execution_mode=ExtractionExecutionMode.OFFLINE,
        rule=rule,
        checked_at=checked_at,
        runtime_hash="0" * 64,
    )
    runtime = runtime_draft.model_copy(
        update={"runtime_hash": calculate_extraction_runtime_hash(runtime_draft)}
    )
    return OfflineExtractionBundle(policy=ExtractionPolicy(), runtime=runtime)

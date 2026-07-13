"""Deterministic offline M19 policy and runtime snapshot."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.knowledge import (
    KnowledgeExecutionMode,
    KnowledgePolicy,
    KnowledgeRuleDescriptor,
    KnowledgeRuntimeSnapshot,
)
from scidatafusion.knowledge.integrity import (
    calculate_knowledge_rule_hash,
    calculate_knowledge_runtime_hash,
)


@dataclass(frozen=True, slots=True)
class OfflineKnowledgeBundle:
    policy: KnowledgePolicy
    runtime: KnowledgeRuntimeSnapshot


def build_offline_knowledge_bundle(
    *, not_before: datetime, clock: Callable[[], datetime] = utc_now
) -> OfflineKnowledgeBundle:
    checked_at = clock()
    if checked_at < not_before:
        raise ValueError("M19 runtime cannot predate its upstream quality result")
    rule_draft = KnowledgeRuleDescriptor(
        rule_id="m19.sparse_graph_quarantine",
        rule_version="1.0.0",
        rule_hash="0" * 64,
    )
    rule = rule_draft.model_copy(update={"rule_hash": calculate_knowledge_rule_hash(rule_draft)})
    runtime_draft = KnowledgeRuntimeSnapshot(
        execution_mode=KnowledgeExecutionMode.OFFLINE,
        rule=rule,
        checked_at=checked_at,
        runtime_hash="0" * 64,
    )
    runtime = runtime_draft.model_copy(
        update={"runtime_hash": calculate_knowledge_runtime_hash(runtime_draft)}
    )
    return OfflineKnowledgeBundle(policy=KnowledgePolicy(), runtime=runtime)

"""Offline M08 capability snapshot for the Ia vertical slice."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.parsing import (
    ParsePlanningExecutionMode,
    ParsePlanningPolicy,
    ParsePlanningRuntimeSnapshot,
    ParserCapabilityRegistry,
)
from scidatafusion.parsing.integrity import calculate_parse_runtime_hash
from scidatafusion.parsing.registry import load_default_parser_registry


@dataclass(frozen=True, slots=True)
class OfflineParsePlanningBundle:
    """Pinned registry, policy, and no-network runtime used by offline acceptance."""

    registry: ParserCapabilityRegistry
    policy: ParsePlanningPolicy
    runtime: ParsePlanningRuntimeSnapshot


def build_offline_parse_planning_bundle(
    *,
    clock: Callable[[], datetime] = utc_now,
) -> OfflineParsePlanningBundle:
    """Build a deterministic local snapshot without model or network classification."""

    registry = load_default_parser_registry()
    policy = ParsePlanningPolicy()
    runtime_draft = ParsePlanningRuntimeSnapshot(
        execution_mode=ParsePlanningExecutionMode.OFFLINE,
        capability_registry_hash=registry.registry_hash,
        available_parser_ids=tuple(
            item.parser_id for item in registry.parsers if not item.requires_network
        ),
        model_classification_enabled=False,
        external_network_enabled=False,
        remaining_cost_micro_usd=policy.max_total_planned_cost_micro_usd,
        checked_at=clock(),
        runtime_hash="0" * 64,
    )
    runtime = runtime_draft.model_copy(
        update={"runtime_hash": calculate_parse_runtime_hash(runtime_draft)}
    )
    return OfflineParsePlanningBundle(
        registry=registry,
        policy=policy,
        runtime=runtime,
    )

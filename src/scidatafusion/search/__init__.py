"""Deterministic, capability-backed scientific search planning."""

from scidatafusion.contracts.search import (
    SearchPlanningRequest,
    SearchPlanningResult,
    SourceCapabilityRegistry,
)
from scidatafusion.search.planner import (
    SearchPlanner,
    calculate_search_plan_hash,
    verify_search_plan_integrity,
)
from scidatafusion.search.query_expansion import deduplicate_queries, normalize_query
from scidatafusion.search.registry import (
    SourceCapabilityRegistryLoader,
    load_default_source_capability_registry,
    load_source_capability_registry,
    source_ids,
)
from scidatafusion.search.stop import SearchStopPolicy

__all__ = [
    "SearchPlanner",
    "SearchPlanningRequest",
    "SearchPlanningResult",
    "SearchStopPolicy",
    "SourceCapabilityRegistry",
    "SourceCapabilityRegistryLoader",
    "calculate_search_plan_hash",
    "deduplicate_queries",
    "load_default_source_capability_registry",
    "load_source_capability_registry",
    "normalize_query",
    "source_ids",
    "verify_search_plan_integrity",
]

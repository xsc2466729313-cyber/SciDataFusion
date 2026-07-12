"""Deterministic no-network M10 runtime for the native CSV vertical slice."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.parsing import (
    ParsePlanningExecutionMode,
    ParsePlanningRuntimeSnapshot,
    ParserCapabilityRegistry,
    ParserTargetModule,
)
from scidatafusion.contracts.tables import (
    TableExecutionMode,
    TableParserRuntimeDescriptor,
    TableParsingPolicy,
    TableParsingRuntimeSnapshot,
)
from scidatafusion.parsing.integrity import calculate_parse_runtime_hash
from scidatafusion.parsing.registry import (
    calculate_parser_capability_hash,
    calculate_parser_registry_hash,
)
from scidatafusion.tables.csv import CsvTableAdapter
from scidatafusion.tables.integrity import (
    calculate_table_descriptor_hash,
    calculate_table_runtime_hash,
)


@dataclass(frozen=True, slots=True)
class OfflineTableParsingBundle:
    policy: TableParsingPolicy
    runtime: TableParsingRuntimeSnapshot


def build_offline_table_parsing_bundle(
    registry: ParserCapabilityRegistry,
    m08_runtime: ParsePlanningRuntimeSnapshot,
    *,
    clock: Callable[[], datetime] = utc_now,
    engine_version: str | None = None,
) -> OfflineTableParsingBundle:
    """Expose only the deterministic local CSV adapter present in the exact M08 snapshot."""

    if not hmac.compare_digest(registry.registry_hash, calculate_parser_registry_hash(registry)):
        raise ValueError("M10 offline runtime requires an integrity-valid parser registry")
    if any(
        item.capability_hash != calculate_parser_capability_hash(item) for item in registry.parsers
    ):
        raise ValueError("M10 offline runtime rejects a tampered parser capability")
    if not hmac.compare_digest(m08_runtime.runtime_hash, calculate_parse_runtime_hash(m08_runtime)):
        raise ValueError("M10 offline runtime requires an integrity-valid M08 runtime")
    if m08_runtime.capability_registry_hash != registry.registry_hash:
        raise ValueError("M10 offline runtime requires the exact M08 registry")
    if m08_runtime.execution_mode is not ParsePlanningExecutionMode.OFFLINE or (
        m08_runtime.model_classification_enabled or m08_runtime.external_network_enabled
    ):
        raise ValueError("M10 offline runtime rejects M08 model or network execution")
    checked_at = clock()
    if checked_at < m08_runtime.checked_at:
        raise ValueError("M10 runtime cannot predate M08")
    adapter = CsvTableAdapter(engine_version=engine_version)
    capability = next(
        (item for item in registry.parsers if item.parser_id == adapter.parser_id), None
    )
    if capability is None or not (
        ParserTargetModule.TABLE in capability.target_modules
        and capability.deterministic
        and not capability.requires_model
        and not capability.requires_network
        and capability.parser_version == adapter.parser_version
    ):
        raise ValueError("M10 CSV adapter does not match its registered capability")
    descriptors: tuple[TableParserRuntimeDescriptor, ...] = ()
    if adapter.parser_id in m08_runtime.available_parser_ids:
        draft = TableParserRuntimeDescriptor(
            parser_id=adapter.parser_id,
            parser_version=adapter.parser_version,
            capability_hash=capability.capability_hash,
            engine_name=adapter.engine_name,
            engine_version=adapter.engine_version,
            descriptor_hash="0" * 64,
        )
        descriptors = (
            draft.model_copy(update={"descriptor_hash": calculate_table_descriptor_hash(draft)}),
        )
    policy = TableParsingPolicy()
    runtime_draft = TableParsingRuntimeSnapshot(
        execution_mode=TableExecutionMode.OFFLINE,
        available_parser_ids=tuple(item.parser_id for item in descriptors),
        parser_descriptors=descriptors,
        model_execution_enabled=False,
        external_network_enabled=False,
        checked_at=checked_at,
        runtime_hash="0" * 64,
    )
    runtime = runtime_draft.model_copy(
        update={"runtime_hash": calculate_table_runtime_hash(runtime_draft)}
    )
    return OfflineTableParsingBundle(policy=policy, runtime=runtime)

"""Deterministic no-network M09 runtime assembly for the offline vertical slice."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.documents import (
    DocumentExecutionMode,
    DocumentParserRuntimeDescriptor,
    DocumentParsingPolicy,
    DocumentParsingRuntimeSnapshot,
)
from scidatafusion.contracts.parsing import (
    ParsePlanningExecutionMode,
    ParsePlanningRuntimeSnapshot,
    ParserCapabilityRegistry,
    ParserId,
    ParserTargetModule,
)
from scidatafusion.documents.html import HtmlDocumentAdapter
from scidatafusion.documents.integrity import (
    calculate_document_parser_descriptor_hash,
    calculate_document_runtime_hash,
)
from scidatafusion.documents.pdf import PypdfDocumentAdapter
from scidatafusion.documents.plain import PlainTextDocumentAdapter
from scidatafusion.parsing.integrity import calculate_parse_runtime_hash
from scidatafusion.parsing.registry import (
    calculate_parser_capability_hash,
    calculate_parser_registry_hash,
)

_IMPLEMENTED_PARSER_IDS: tuple[ParserId, ...] = (
    "m09.pdf_text",
    "m09.html",
    "m09.text",
)


@dataclass(frozen=True, slots=True)
class OfflineDocumentParsingBundle:
    """Bounded M09 policy and immutable runtime for local deterministic adapters."""

    policy: DocumentParsingPolicy
    runtime: DocumentParsingRuntimeSnapshot


def build_offline_document_parsing_bundle(
    registry: ParserCapabilityRegistry,
    m08_runtime: ParsePlanningRuntimeSnapshot,
    *,
    clock: Callable[[], datetime] = utc_now,
    engine_version_resolver: Callable[[str], str] | None = None,
) -> OfflineDocumentParsingBundle:
    """Expose only implemented local M09 adapters still available in the exact M08 snapshot."""

    if not hmac.compare_digest(
        m08_runtime.capability_registry_hash,
        registry.registry_hash,
    ):
        raise ValueError("M09 offline runtime requires the exact M08 capability registry")
    if not hmac.compare_digest(
        registry.registry_hash,
        calculate_parser_registry_hash(registry),
    ) or any(
        not hmac.compare_digest(
            capability.capability_hash,
            calculate_parser_capability_hash(capability),
        )
        for capability in registry.parsers
    ):
        raise ValueError("M09 offline runtime requires an integrity-valid parser registry")
    if not hmac.compare_digest(
        m08_runtime.runtime_hash,
        calculate_parse_runtime_hash(m08_runtime),
    ):
        raise ValueError("M09 offline runtime requires an integrity-valid M08 runtime")
    if m08_runtime.execution_mode is not ParsePlanningExecutionMode.OFFLINE:
        raise ValueError("M09 offline fixtures require an offline M08 runtime")
    if m08_runtime.model_classification_enabled or m08_runtime.external_network_enabled:
        raise ValueError("M09 offline fixtures reject M08 model or network permissions")

    checked_at = clock()
    if checked_at < m08_runtime.checked_at:
        raise ValueError("M09 offline runtime cannot predate its M08 availability snapshot")

    adapters = _implemented_adapters(engine_version_resolver)
    adapter_by_id = {adapter.parser_id: adapter for adapter in adapters}
    if tuple(adapter_by_id) != _IMPLEMENTED_PARSER_IDS:
        raise RuntimeError("M09 offline adapter registry does not match the implemented allowlist")
    capability_by_id = {item.parser_id: item for item in registry.parsers}
    available_in_m08 = set(m08_runtime.available_parser_ids)
    descriptors: list[DocumentParserRuntimeDescriptor] = []
    for parser_id in _IMPLEMENTED_PARSER_IDS:
        adapter = adapter_by_id[parser_id]
        capability = capability_by_id.get(parser_id)
        if capability is None:
            raise ValueError(f"M09 implemented parser is missing from M08 registry: {parser_id}")
        if (
            ParserTargetModule.DOCUMENT not in capability.target_modules
            or capability.requires_model
            or capability.requires_network
            or not capability.deterministic
            or capability.parser_version != adapter.parser_version
        ):
            raise ValueError(
                f"M09 offline adapter does not match its deterministic capability: {parser_id}"
            )
        if parser_id not in available_in_m08:
            continue
        draft = DocumentParserRuntimeDescriptor(
            parser_id=parser_id,
            parser_version=adapter.parser_version,
            capability_hash=capability.capability_hash,
            engine_name=adapter.engine_name,
            engine_version=adapter.engine_version,
            descriptor_hash="0" * 64,
        )
        descriptors.append(
            draft.model_copy(
                update={"descriptor_hash": calculate_document_parser_descriptor_hash(draft)}
            )
        )

    policy = DocumentParsingPolicy()
    runtime_draft = DocumentParsingRuntimeSnapshot(
        execution_mode=DocumentExecutionMode.OFFLINE,
        available_parser_ids=tuple(item.parser_id for item in descriptors),
        parser_descriptors=tuple(descriptors),
        model_execution_enabled=False,
        external_network_enabled=False,
        remaining_cost_micro_usd=min(
            policy.max_total_cost_micro_usd,
            m08_runtime.remaining_cost_micro_usd,
        ),
        checked_at=checked_at,
        runtime_hash="0" * 64,
    )
    runtime = runtime_draft.model_copy(
        update={"runtime_hash": calculate_document_runtime_hash(runtime_draft)}
    )
    return OfflineDocumentParsingBundle(policy=policy, runtime=runtime)


def _implemented_adapters(
    engine_version_resolver: Callable[[str], str] | None,
) -> tuple[PypdfDocumentAdapter, HtmlDocumentAdapter, PlainTextDocumentAdapter]:
    if engine_version_resolver is None:
        return (
            PypdfDocumentAdapter(),
            HtmlDocumentAdapter(),
            PlainTextDocumentAdapter(),
        )
    return (
        PypdfDocumentAdapter(engine_version=engine_version_resolver("pypdf")),
        HtmlDocumentAdapter(engine_version=engine_version_resolver("python.html-parser")),
        PlainTextDocumentAdapter(engine_version=engine_version_resolver("python.text")),
    )

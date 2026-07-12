from __future__ import annotations

import platform
from datetime import UTC, datetime, timedelta

import pypdf
import pytest

from scidatafusion.contracts.documents import DocumentExecutionMode
from scidatafusion.documents.fixtures import build_offline_document_parsing_bundle
from scidatafusion.documents.integrity import (
    calculate_document_parser_descriptor_hash,
    calculate_document_runtime_hash,
)
from scidatafusion.parsing.fixtures import (
    OfflineParsePlanningBundle,
    build_offline_parse_planning_bundle,
)
from scidatafusion.parsing.integrity import calculate_parse_runtime_hash

M08_TIME = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
M09_TIME = M08_TIME + timedelta(seconds=1)
CONTROLLED_ENGINE_VERSIONS = {
    "pypdf": "6.14.0",
    "python.html-parser": "3.11.0",
    "python.text": "3.11.0",
}


def _m08_bundle() -> OfflineParsePlanningBundle:
    return build_offline_parse_planning_bundle(clock=lambda: M08_TIME)


def _controlled_version(engine_name: str) -> str:
    return CONTROLLED_ENGINE_VERSIONS[engine_name]


def test_offline_bundle_is_deterministic_and_content_addressed() -> None:
    m08 = _m08_bundle()
    first = build_offline_document_parsing_bundle(
        m08.registry,
        m08.runtime,
        clock=lambda: M09_TIME,
        engine_version_resolver=_controlled_version,
    )
    second = build_offline_document_parsing_bundle(
        m08.registry,
        m08.runtime,
        clock=lambda: M09_TIME,
        engine_version_resolver=_controlled_version,
    )

    assert first == second
    assert first.runtime.runtime_hash == calculate_document_runtime_hash(first.runtime)
    assert all(
        descriptor.descriptor_hash == calculate_document_parser_descriptor_hash(descriptor)
        for descriptor in first.runtime.parser_descriptors
    )


def test_offline_bundle_exposes_only_implemented_document_adapters() -> None:
    m08 = _m08_bundle()
    bundle = build_offline_document_parsing_bundle(
        m08.registry,
        m08.runtime,
        clock=lambda: M09_TIME,
        engine_version_resolver=_controlled_version,
    )
    capabilities = {item.parser_id: item for item in m08.registry.parsers}

    assert bundle.runtime.available_parser_ids == (
        "m09.pdf_text",
        "m09.html",
        "m09.text",
    )
    assert tuple(item.engine_name for item in bundle.runtime.parser_descriptors) == (
        "pypdf",
        "python.html-parser",
        "python.text",
    )
    for descriptor in bundle.runtime.parser_descriptors:
        capability = capabilities[descriptor.parser_id]
        assert descriptor.parser_version == capability.parser_version
        assert descriptor.capability_hash == capability.capability_hash
        assert descriptor.engine_version == CONTROLLED_ENGINE_VERSIONS[descriptor.engine_name]

    unavailable = {
        "m09.pdf_ocr",
        "m09.pdf_vlm",
        "m09.docx",
        "m09.pptx",
        "m09.xml",
    }
    assert unavailable.isdisjoint(bundle.runtime.available_parser_ids)
    assert all(
        not parser_id.startswith("m10.") for parser_id in bundle.runtime.available_parser_ids
    )


def test_offline_bundle_can_only_shrink_m08_parser_availability() -> None:
    m08 = _m08_bundle()
    available = tuple(
        parser_id for parser_id in m08.runtime.available_parser_ids if parser_id != "m09.html"
    )
    draft = m08.runtime.model_copy(
        update={"available_parser_ids": available, "runtime_hash": "0" * 64}
    )
    shrunk_m08_runtime = draft.model_copy(
        update={"runtime_hash": calculate_parse_runtime_hash(draft)}
    )
    bundle = build_offline_document_parsing_bundle(
        m08.registry,
        shrunk_m08_runtime,
        clock=lambda: M09_TIME,
        engine_version_resolver=_controlled_version,
    )

    assert bundle.runtime.available_parser_ids == ("m09.pdf_text", "m09.text")
    assert set(bundle.runtime.available_parser_ids).issubset(
        shrunk_m08_runtime.available_parser_ids
    )


def test_offline_bundle_disables_models_network_ocr_and_vlm() -> None:
    m08 = _m08_bundle()
    bundle = build_offline_document_parsing_bundle(
        m08.registry,
        m08.runtime,
        clock=lambda: M09_TIME,
        engine_version_resolver=_controlled_version,
    )

    assert bundle.runtime.execution_mode is DocumentExecutionMode.OFFLINE
    assert bundle.runtime.model_execution_enabled is False
    assert bundle.runtime.external_network_enabled is False
    assert bundle.policy.allow_model_execution is False
    assert bundle.policy.allow_external_network is False
    assert bundle.policy.allow_ocr is False
    assert bundle.policy.allow_vlm is False
    assert bundle.runtime.remaining_cost_micro_usd == min(
        bundle.policy.max_total_cost_micro_usd,
        m08.runtime.remaining_cost_micro_usd,
    )


def test_default_engine_versions_are_read_from_the_local_runtime() -> None:
    m08 = _m08_bundle()
    bundle = build_offline_document_parsing_bundle(
        m08.registry,
        m08.runtime,
        clock=lambda: M09_TIME,
    )
    versions = {item.engine_name: item.engine_version for item in bundle.runtime.parser_descriptors}

    assert versions == {
        "pypdf": pypdf.__version__,
        "python.html-parser": platform.python_version(),
        "python.text": platform.python_version(),
    }


def test_offline_bundle_rejects_tampered_or_incompatible_m08_snapshots() -> None:
    m08 = _m08_bundle()
    tampered = m08.runtime.model_copy(update={"runtime_hash": "f" * 64})
    with pytest.raises(ValueError, match="integrity-valid M08 runtime"):
        build_offline_document_parsing_bundle(
            m08.registry,
            tampered,
            clock=lambda: M09_TIME,
            engine_version_resolver=_controlled_version,
        )

    with pytest.raises(ValueError, match="cannot predate"):
        build_offline_document_parsing_bundle(
            m08.registry,
            m08.runtime,
            clock=lambda: M08_TIME - timedelta(seconds=1),
            engine_version_resolver=_controlled_version,
        )

    wrong_registry = m08.registry.model_copy(update={"registry_hash": "f" * 64})
    with pytest.raises(ValueError, match="exact M08 capability registry"):
        build_offline_document_parsing_bundle(
            wrong_registry,
            m08.runtime,
            clock=lambda: M09_TIME,
            engine_version_resolver=_controlled_version,
        )

    first_capability = m08.registry.parsers[0].model_copy(update={"parser_version": "9.9.9"})
    tampered_registry = m08.registry.model_copy(
        update={"parsers": (first_capability, *m08.registry.parsers[1:])}
    )
    with pytest.raises(ValueError, match="integrity-valid parser registry"):
        build_offline_document_parsing_bundle(
            tampered_registry,
            m08.runtime,
            clock=lambda: M09_TIME,
            engine_version_resolver=_controlled_version,
        )

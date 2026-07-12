from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from scidatafusion.connectors import (
    ConnectorRegistryLoader,
    calculate_connector_descriptor_hash,
    find_connector_by_id,
    find_connector_by_source,
    load_connector_registry,
    load_default_connector_registry,
    require_connector_by_id,
    require_connector_by_source,
)
from scidatafusion.contracts.connectors import AuthKind, ConnectorDescriptor
from scidatafusion.domain.registry import RegistryErrorCode, RegistryLoadError, canonical_hash
from scidatafusion.search.registry import SourceCapabilityRegistryLoader

_REGISTRY_PATH = Path("src/scidatafusion/connector_registry/registry.json")


def test_default_connector_registry_matches_m04_capabilities() -> None:
    registry = load_default_connector_registry()
    capabilities = SourceCapabilityRegistryLoader.load_default()

    assert registry.registry_version == "1.0.0"
    assert len(registry.connectors) == len(capabilities.capabilities) == 4
    for capability in capabilities.capabilities:
        descriptor = require_connector_by_source(registry, capability.source_id)
        assert descriptor.connector_id == capability.connector_id
        assert descriptor.category is capability.category
        assert descriptor.protocol is capability.protocol
        assert descriptor.supported_operation_ids == tuple(
            item.operation_id for item in capability.operations
        )
        assert descriptor.supported_dialects == tuple(
            item.dialect for item in capability.operations
        )


def test_default_registry_pins_endpoints_and_credential_references() -> None:
    registry = ConnectorRegistryLoader.load_default()
    expected_endpoints = {
        "vizier_tap": "https://tapvizier.cds.unistra.fr/TAPVizieR/tap/sync",
        "openalex_literature": "https://api.openalex.org/works",
        "zenodo_repository": "https://zenodo.org/api/records",
        "supplement_web": "https://api.crossref.org/works",
    }
    assert {item.source_id: item.endpoint for item in registry.connectors} == expected_endpoints
    assert all(item.endpoint.startswith("https://") for item in registry.connectors)

    openalex = require_connector_by_source(registry, "openalex_literature")
    assert openalex.auth_kind is AuthKind.QUERY_API_KEY
    assert openalex.credential_environment == "OPENALEX_API_KEY"
    assert openalex.api_key_parameter == "api_key"

    zenodo = require_connector_by_source(registry, "zenodo_repository")
    assert zenodo.auth_kind is AuthKind.BEARER
    assert zenodo.credential_environment == "ZENODO_ACCESS_TOKEN"
    assert zenodo.api_key_parameter is None

    for source_id in ("vizier_tap", "supplement_web"):
        descriptor = require_connector_by_source(registry, source_id)
        assert descriptor.auth_kind is AuthKind.NONE
        assert descriptor.credential_environment is None
        assert descriptor.api_key_parameter is None


def test_connector_lookups_are_exact_and_fail_closed() -> None:
    registry = load_connector_registry(_REGISTRY_PATH)
    descriptor = registry.connectors[0]

    assert find_connector_by_id(registry, descriptor.connector_id) is descriptor
    assert find_connector_by_source(registry, descriptor.source_id) is descriptor
    assert require_connector_by_id(registry, descriptor.connector_id) is descriptor
    assert require_connector_by_source(registry, descriptor.source_id) is descriptor
    assert find_connector_by_id(registry, "unknown_connector") is None
    assert find_connector_by_source(registry, "unknown_source") is None
    with pytest.raises(KeyError, match="connector is not registered"):
        require_connector_by_id(registry, "unknown_connector")
    with pytest.raises(KeyError, match="connector source is not registered"):
        require_connector_by_source(registry, "unknown_source")


def test_descriptor_hash_is_canonical_and_sensitive_to_metadata() -> None:
    descriptor = ConnectorRegistryLoader.load_default().connectors[0]
    expected = canonical_hash(descriptor.model_dump(mode="json"))
    assert calculate_connector_descriptor_hash(descriptor) == expected
    assert calculate_connector_descriptor_hash(descriptor) == expected

    changed = ConnectorDescriptor.model_validate(
        {**descriptor.model_dump(), "requests_per_minute": descriptor.requests_per_minute + 1}
    )
    assert calculate_connector_descriptor_hash(changed) != expected


def test_connector_registry_rejects_hash_tampering(tmp_path: Path) -> None:
    raw = cast(dict[str, Any], json.loads(_REGISTRY_PATH.read_text(encoding="utf-8")))
    connectors = cast(list[dict[str, Any]], raw["connectors"])
    connectors[0]["requests_per_minute"] += 1
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(RegistryLoadError) as error:
        ConnectorRegistryLoader.from_file(tampered)
    assert error.value.code is RegistryErrorCode.HASH_MISMATCH


def test_connector_registry_rejects_unknown_fields_even_with_valid_hash(
    tmp_path: Path,
) -> None:
    raw = cast(dict[str, Any], json.loads(_REGISTRY_PATH.read_text(encoding="utf-8")))
    connectors = cast(list[dict[str, Any]], raw["connectors"])
    connectors[0]["credential_value"] = "must-not-be-accepted"
    raw["content_hash"] = canonical_hash(
        {"connectors": raw["connectors"], "registry_version": raw["registry_version"]}
    )
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(RegistryLoadError) as error:
        ConnectorRegistryLoader.from_file(invalid)
    assert error.value.code is RegistryErrorCode.INVALID_SCHEMA


@pytest.mark.parametrize(
    ("contents", "expected_code"),
    (
        ("not-json", RegistryErrorCode.INVALID_JSON),
        ('{"registry_version":"1.0.0"}', RegistryErrorCode.INVALID_SCHEMA),
    ),
)
def test_connector_registry_reports_structured_parse_failures(
    tmp_path: Path,
    contents: str,
    expected_code: RegistryErrorCode,
) -> None:
    path = tmp_path / "registry.json"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(RegistryLoadError) as error:
        ConnectorRegistryLoader.from_file(path)
    assert error.value.code is expected_code


def test_connector_registry_reports_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(RegistryLoadError) as error:
        ConnectorRegistryLoader.from_file(missing)
    assert error.value.code is RegistryErrorCode.NOT_FOUND

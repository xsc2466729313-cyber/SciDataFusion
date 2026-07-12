from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from scidatafusion.domain.registry import RegistryErrorCode, RegistryLoadError, canonical_hash
from scidatafusion.parsing.registry import (
    ParserCapabilityRegistryLoader,
    calculate_parser_capability_hash,
    calculate_parser_registry_hash,
    find_parser,
    load_default_parser_registry,
)

_REGISTRY_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "scidatafusion"
    / "registries"
    / "parser_capabilities.json"
)
_PINNED_REGISTRY_HASH = "c730fdad1494054042602cd3c09b702744ae71c82aed37e00af91618edfd1202"


def _raw_registry() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(_REGISTRY_PATH.read_text(encoding="utf-8")))


def _write_registry(path: Path, raw: dict[str, Any]) -> None:
    path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")


def _recalculate_registry_hash(raw: dict[str, Any]) -> None:
    raw["registry_hash"] = canonical_hash(
        {
            "parsers": raw["parsers"],
            "registry_version": raw["registry_version"],
        }
    )


def test_default_parser_registry_hashes_are_stable_and_verified() -> None:
    first = load_default_parser_registry()
    second = ParserCapabilityRegistryLoader.load_default()

    assert first == second
    assert first.registry_version == "1.0.0"
    assert len(first.parsers) == 16
    assert first.registry_hash == _PINNED_REGISTRY_HASH
    assert calculate_parser_registry_hash(first) == _PINNED_REGISTRY_HASH
    assert tuple(calculate_parser_capability_hash(item) for item in first.parsers) == tuple(
        item.capability_hash for item in first.parsers
    )
    assert len({item.capability_hash for item in first.parsers}) == len(first.parsers)


def test_parser_registry_lookup_is_exact_and_non_executable() -> None:
    registry = load_default_parser_registry()
    parser = registry.parsers[0]

    assert find_parser(registry, parser.parser_id) is parser
    assert find_parser(registry, "m09.pdf-text") is None
    assert find_parser(registry, "unknown.parser") is None


def test_parser_registry_reports_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"

    with pytest.raises(RegistryLoadError) as error:
        ParserCapabilityRegistryLoader.from_file(missing)

    assert error.value.code is RegistryErrorCode.NOT_FOUND
    assert error.value.path == missing.resolve()


@pytest.mark.parametrize(
    ("mutation_target", "field", "value"),
    (
        ("registry", "unexpected", True),
        ("parser", "unexpected", True),
        ("parser", "entrypoint", "os.system"),
        ("parser", "module", "subprocess"),
        ("parser", "command", ["powershell", "-Command", "Invoke-WebRequest"]),
    ),
)
def test_parser_registry_rejects_extra_and_executable_metadata_even_with_valid_outer_hash(
    tmp_path: Path,
    mutation_target: str,
    field: str,
    value: object,
) -> None:
    raw = _raw_registry()
    if mutation_target == "registry":
        raw[field] = value
    else:
        parsers = cast(list[dict[str, Any]], raw["parsers"])
        parsers[0][field] = value
    _recalculate_registry_hash(raw)
    path = tmp_path / f"extra-{field}.json"
    _write_registry(path, raw)

    with pytest.raises(RegistryLoadError) as error:
        ParserCapabilityRegistryLoader.from_file(path)

    assert error.value.code is RegistryErrorCode.INVALID_SCHEMA


@pytest.mark.parametrize(
    "mutation",
    (
        {"registry_version": "not-semver"},
        {"parsers": []},
        {"registry_hash": "not-a-sha256"},
    ),
)
def test_parser_registry_rejects_invalid_schema(
    tmp_path: Path,
    mutation: dict[str, object],
) -> None:
    raw = _raw_registry()
    raw.update(mutation)
    path = tmp_path / "invalid-schema.json"
    _write_registry(path, raw)

    with pytest.raises(RegistryLoadError) as error:
        ParserCapabilityRegistryLoader.from_file(path)

    assert error.value.code is RegistryErrorCode.INVALID_SCHEMA


def test_parser_registry_rejects_capability_hash_tampering(tmp_path: Path) -> None:
    raw = _raw_registry()
    parsers = cast(list[dict[str, Any]], raw["parsers"])
    parsers[0]["max_input_bytes"] = cast(int, parsers[0]["max_input_bytes"]) - 1
    _recalculate_registry_hash(raw)
    path = tmp_path / "tampered-capability.json"
    _write_registry(path, raw)

    with pytest.raises(RegistryLoadError) as error:
        ParserCapabilityRegistryLoader.from_file(path)

    assert error.value.code is RegistryErrorCode.HASH_MISMATCH
    assert "parser capability hash mismatch" in error.value.detail


def test_parser_registry_rejects_outer_hash_tampering(tmp_path: Path) -> None:
    raw = _raw_registry()
    parsers = cast(list[dict[str, Any]], raw["parsers"])
    parsers.reverse()
    path = tmp_path / "tampered-registry.json"
    _write_registry(path, raw)

    with pytest.raises(RegistryLoadError) as error:
        ParserCapabilityRegistryLoader.from_file(path)

    assert error.value.code is RegistryErrorCode.HASH_MISMATCH
    assert error.value.detail == "parser registry content hash mismatch"

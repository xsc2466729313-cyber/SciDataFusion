"""Strict content-addressed parser capability registry for M08."""

from __future__ import annotations

import hmac
import json
from pathlib import Path

from pydantic import ValidationError

from scidatafusion.contracts.parsing import (
    ParserCapability,
    ParserCapabilityRegistry,
    ParserId,
)
from scidatafusion.domain.registry import RegistryErrorCode, RegistryLoadError, canonical_hash

_MAX_REGISTRY_BYTES = 2 * 1024 * 1024
_DEFAULT_REGISTRY = Path(__file__).resolve().parents[1] / "registries" / "parser_capabilities.json"


def calculate_parser_capability_hash(capability: ParserCapability) -> str:
    """Recalculate one parser descriptor hash without its self-reference."""

    return canonical_hash(capability.model_dump(mode="json", exclude={"capability_hash"}))


def calculate_parser_registry_hash(registry: ParserCapabilityRegistry) -> str:
    """Recalculate the registry hash from its ordered capability snapshot."""

    return canonical_hash(registry.model_dump(mode="json", exclude={"registry_hash"}))


class ParserCapabilityRegistryLoader:
    """Load a bounded, strict, content-addressed parser registry from JSON."""

    @classmethod
    def from_file(cls, path: Path | str) -> ParserCapabilityRegistry:
        """Read and verify one untrusted registry without dynamic imports."""

        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise RegistryLoadError(RegistryErrorCode.NOT_FOUND, resolved, "file does not exist")
        try:
            size = resolved.stat().st_size
        except OSError as exc:
            raise RegistryLoadError(RegistryErrorCode.NOT_FOUND, resolved, str(exc)) from exc
        if size > _MAX_REGISTRY_BYTES:
            raise RegistryLoadError(
                RegistryErrorCode.TOO_LARGE,
                resolved,
                f"registry is {size} bytes; maximum is {_MAX_REGISTRY_BYTES}",
            )
        try:
            encoded = resolved.read_bytes()
        except OSError as exc:
            raise RegistryLoadError(RegistryErrorCode.NOT_FOUND, resolved, str(exc)) from exc
        try:
            json.loads(encoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RegistryLoadError(RegistryErrorCode.INVALID_JSON, resolved, str(exc)) from exc
        try:
            registry = ParserCapabilityRegistry.model_validate_json(encoded)
        except ValidationError as exc:
            raise RegistryLoadError(RegistryErrorCode.INVALID_SCHEMA, resolved, str(exc)) from exc

        for capability in registry.parsers:
            expected = calculate_parser_capability_hash(capability)
            if not hmac.compare_digest(capability.capability_hash, expected):
                raise RegistryLoadError(
                    RegistryErrorCode.HASH_MISMATCH,
                    resolved,
                    f"parser capability hash mismatch: {capability.parser_id}",
                )
        expected_registry = calculate_parser_registry_hash(registry)
        if not hmac.compare_digest(registry.registry_hash, expected_registry):
            raise RegistryLoadError(
                RegistryErrorCode.HASH_MISMATCH,
                resolved,
                "parser registry content hash mismatch",
            )
        return registry

    @classmethod
    def load_default(cls) -> ParserCapabilityRegistry:
        """Load the package's pinned parser capability snapshot."""

        return cls.from_file(_DEFAULT_REGISTRY)


def load_default_parser_registry() -> ParserCapabilityRegistry:
    """Return the verified default parser capability registry."""

    return ParserCapabilityRegistryLoader.load_default()


def find_parser(
    registry: ParserCapabilityRegistry,
    parser_id: ParserId,
) -> ParserCapability | None:
    """Find an exact parser descriptor without executing registry content."""

    return next((item for item in registry.parsers if item.parser_id == parser_id), None)

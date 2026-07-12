"""Strict loader for the versioned M04 source-capability registry."""

from __future__ import annotations

import hmac
import json
from pathlib import Path

from pydantic import ValidationError

from scidatafusion.contracts.search import SourceCapabilityRegistry, SourceId
from scidatafusion.domain.registry import RegistryErrorCode, RegistryLoadError, canonical_hash

_MAX_REGISTRY_BYTES = 2 * 1024 * 1024
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class SourceCapabilityRegistryLoader:
    """Load an immutable capability snapshot from untrusted JSON bytes."""

    @classmethod
    def from_file(cls, path: Path | str) -> SourceCapabilityRegistry:
        """Read, strictly validate, and verify one content-addressed registry."""

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
            raw = json.loads(encoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RegistryLoadError(RegistryErrorCode.INVALID_JSON, resolved, str(exc)) from exc
        try:
            registry = SourceCapabilityRegistry.model_validate_json(encoded)
        except ValidationError as exc:
            raise RegistryLoadError(RegistryErrorCode.INVALID_SCHEMA, resolved, str(exc)) from exc

        actual_hash = canonical_hash(
            {
                "capabilities": raw["capabilities"],
                "registry_version": raw["registry_version"],
                "term_expansions": raw["term_expansions"],
            }
        )
        if not hmac.compare_digest(registry.content_hash, actual_hash):
            raise RegistryLoadError(
                RegistryErrorCode.HASH_MISMATCH,
                resolved,
                f"declared {registry.content_hash}, calculated {actual_hash}",
            )
        return registry

    @classmethod
    def load_default(cls) -> SourceCapabilityRegistry:
        """Load the repository's pinned source-capability registry."""

        return cls.from_file(_PROJECT_ROOT / "search_capabilities" / "registry.json")


def load_source_capability_registry(path: Path | str) -> SourceCapabilityRegistry:
    """Functional entry point for callers that do not need a loader class."""

    return SourceCapabilityRegistryLoader.from_file(path)


def load_default_source_capability_registry() -> SourceCapabilityRegistry:
    """Return the repository's verified default capability snapshot."""

    return SourceCapabilityRegistryLoader.load_default()


def source_ids(registry: SourceCapabilityRegistry) -> tuple[SourceId, ...]:
    """Return source identifiers in their versioned registry priority order."""

    return tuple(capability.source_id for capability in registry.capabilities)

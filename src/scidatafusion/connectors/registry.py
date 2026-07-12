"""Strict loader and lookups for the content-addressed M05 Connector registry."""

from __future__ import annotations

import hmac
import json
from pathlib import Path

from pydantic import ValidationError

from scidatafusion.contracts.connectors import ConnectorDescriptor, ConnectorId, ConnectorRegistry
from scidatafusion.contracts.search import SourceId
from scidatafusion.domain.registry import RegistryErrorCode, RegistryLoadError, canonical_hash

_MAX_REGISTRY_BYTES = 2 * 1024 * 1024
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def calculate_connector_descriptor_hash(descriptor: ConnectorDescriptor) -> str:
    """Return the stable hash bound into one Connector runtime entry."""

    return canonical_hash(descriptor.model_dump(mode="json"))


class ConnectorRegistryLoader:
    """Load an immutable Connector registry from untrusted JSON bytes."""

    @classmethod
    def from_file(cls, path: Path | str) -> ConnectorRegistry:
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
            registry = ConnectorRegistry.model_validate_json(encoded)
        except ValidationError as exc:
            raise RegistryLoadError(RegistryErrorCode.INVALID_SCHEMA, resolved, str(exc)) from exc

        actual_hash = canonical_hash(
            {
                "connectors": raw["connectors"],
                "registry_version": raw["registry_version"],
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
    def load_default(cls) -> ConnectorRegistry:
        """Load the package's pinned Connector registry."""

        return cls.from_file(_PACKAGE_ROOT / "connector_registry" / "registry.json")


def load_connector_registry(path: Path | str) -> ConnectorRegistry:
    """Functional entry point for loading an explicit Connector registry."""

    return ConnectorRegistryLoader.from_file(path)


def load_default_connector_registry() -> ConnectorRegistry:
    """Return the verified default Connector registry snapshot."""

    return ConnectorRegistryLoader.load_default()


def find_connector_by_id(
    registry: ConnectorRegistry, connector_id: ConnectorId
) -> ConnectorDescriptor | None:
    """Find a Connector by exact stable identifier without fallback matching."""

    return next((item for item in registry.connectors if item.connector_id == connector_id), None)


def require_connector_by_id(
    registry: ConnectorRegistry, connector_id: ConnectorId
) -> ConnectorDescriptor:
    """Return an exact Connector match or fail closed."""

    descriptor = find_connector_by_id(registry, connector_id)
    if descriptor is None:
        raise KeyError(f"connector is not registered: {connector_id}")
    return descriptor


def find_connector_by_source(
    registry: ConnectorRegistry, source_id: SourceId
) -> ConnectorDescriptor | None:
    """Find the sole Connector registered for an exact source identifier."""

    return next((item for item in registry.connectors if item.source_id == source_id), None)


def require_connector_by_source(
    registry: ConnectorRegistry, source_id: SourceId
) -> ConnectorDescriptor:
    """Return a source's exact Connector match or fail closed."""

    descriptor = find_connector_by_source(registry, source_id)
    if descriptor is None:
        raise KeyError(f"connector source is not registered: {source_id}")
    return descriptor

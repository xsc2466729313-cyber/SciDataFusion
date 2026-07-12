"""Source-agnostic Connector protocols and in-memory boundary implementations."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Protocol, runtime_checkable

from pydantic import SecretStr, ValidationError

from scidatafusion.contracts.base import ArtifactReference
from scidatafusion.contracts.connectors import (
    ConnectorAttempt,
    ConnectorDescriptor,
    ConnectorErrorCode,
    ConnectorExecutionPolicy,
    ConnectorPage,
    ConnectorRuntimeEntry,
)
from scidatafusion.contracts.search import ExecutableQuery


@dataclass(frozen=True, slots=True)
class ConnectorExecutionOutcome:
    """One query's immutable pages, attempts, and optional terminal error."""

    pages: tuple[ConnectorPage, ...]
    attempts: tuple[ConnectorAttempt, ...]
    error_code: ConnectorErrorCode | None = None


@runtime_checkable
class Connector(Protocol):
    """Source-neutral interface consumed by the M05 batch executor."""

    @property
    def descriptor(self) -> ConnectorDescriptor:
        """Return the immutable registry descriptor bound to this Connector."""

    @property
    def parser_version(self) -> str:
        """Return the exact parser implementation version used for evidence."""

    async def execute(
        self,
        query: ExecutableQuery,
        runtime_entry: ConnectorRuntimeEntry,
        policy: ConnectorExecutionPolicy,
    ) -> ConnectorExecutionOutcome:
        """Execute one planned query without mutating scientific values."""


class CredentialProvider(Protocol):
    """Resolve a named credential while keeping its value wrapped as a secret."""

    def get(self, environment_name: str) -> SecretStr | None:
        """Return a secret wrapper or ``None``; never return a plain-text value."""


class EnvironmentCredentialProvider:
    """Read only the exact environment variable named by a Connector descriptor."""

    __slots__ = ("_environment",)

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = os.environ if environment is None else environment

    def get(self, environment_name: str) -> SecretStr | None:
        value = self._environment.get(environment_name)
        if value is None or not value.strip():
            return None
        return SecretStr(value)


class MappingCredentialProvider:
    """Test-friendly credential provider whose representation cannot expose values."""

    __slots__ = ("_credentials",)

    def __init__(self, credentials: Mapping[str, SecretStr | str]) -> None:
        wrapped = {
            name: value if isinstance(value, SecretStr) else SecretStr(value)
            for name, value in credentials.items()
        }
        self._credentials = {
            name: value for name, value in wrapped.items() if value.get_secret_value().strip()
        }

    def get(self, environment_name: str) -> SecretStr | None:
        return self._credentials.get(environment_name)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(credential_names={tuple(sorted(self._credentials))!r})"


class ArtifactStore(Protocol):
    """Persist immutable raw bytes and return a content-addressed reference."""

    def put(self, content: bytes, *, media_type: str, created_at: datetime) -> ArtifactReference:
        """Store bytes once by SHA-256 and return their stable reference."""

    def contains(self, reference: ArtifactReference) -> bool:
        """Return whether the exact referenced bytes remain available."""


class MemoryArtifactStore:
    """Process-local immutable artifact store for mock, fixture, and unit-test runs."""

    def __init__(self) -> None:
        self._content: dict[str, bytes] = {}
        self._references: dict[tuple[str, str], ArtifactReference] = {}
        self._lock = RLock()

    def put(self, content: bytes, *, media_type: str, created_at: datetime) -> ArtifactReference:
        digest = hashlib.sha256(content).hexdigest()
        key = (digest, media_type)
        with self._lock:
            previous = self._content.get(digest)
            if previous is not None and previous != content:
                raise RuntimeError("content-addressed artifact collision")
            self._content[digest] = content
            reference = self._references.get(key)
            if reference is None:
                artifact_digest = hashlib.sha256(f"{digest}\x00{media_type}".encode()).hexdigest()
                reference = ArtifactReference(
                    artifact_id=f"art_{artifact_digest[:32]}",
                    uri=f"memory://connector-artifacts/sha256/{digest}",
                    sha256=digest,
                    media_type=media_type,
                    size_bytes=len(content),
                    created_at=created_at,
                )
                self._references[key] = reference
            return reference

    def read(self, digest: str) -> bytes | None:
        """Return immutable bytes for verification without exposing a mutable buffer."""

        with self._lock:
            content = self._content.get(digest)
            return bytes(content) if content is not None else None

    def contains(self, reference: ArtifactReference) -> bool:
        """Verify that a reference resolves to bytes with the declared content metadata."""

        with self._lock:
            content = self._content.get(reference.sha256)
            stored_reference = self._references.get((reference.sha256, reference.media_type))
        return (
            content is not None
            and hashlib.sha256(content).hexdigest() == reference.sha256
            and stored_reference == reference
        )


class ConnectorPageCache(Protocol):
    """Cache validated Connector pages by a credential-free request hash."""

    def get(self, key: str) -> ConnectorPage | None:
        """Return a freshly validated page or ``None`` on miss/corruption."""

    def put(self, key: str, page: ConnectorPage) -> None:
        """Store an immutable serialized page after contract validation."""


class MemoryConnectorPageCache:
    """Validated process-local cache that never returns an unchecked object reference."""

    def __init__(self) -> None:
        self._payloads: dict[str, bytes] = {}
        self._lock = RLock()

    def get(self, key: str) -> ConnectorPage | None:
        with self._lock:
            payload = self._payloads.get(key)
        if payload is None:
            return None
        try:
            return ConnectorPage.model_validate_json(payload)
        except ValidationError:
            with self._lock:
                self._payloads.pop(key, None)
            return None

    def put(self, key: str, page: ConnectorPage) -> None:
        validated = ConnectorPage.model_validate(page.model_dump(mode="python"))
        with self._lock:
            self._payloads[key] = validated.model_dump_json().encode("utf-8")


class ResponseParseError(ValueError):
    """Structured parser failure for an untrusted external response."""

    def __init__(
        self,
        message: str,
        *,
        code: ConnectorErrorCode = ConnectorErrorCode.INVALID_RESPONSE,
    ) -> None:
        super().__init__(message)
        self.code = code

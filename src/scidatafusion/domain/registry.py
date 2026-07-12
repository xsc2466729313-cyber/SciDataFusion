"""Strict, content-addressed JSON registries for routing packs."""

from __future__ import annotations

import hashlib
import hmac
import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, ValidationError, model_validator

from scidatafusion.contracts.base import ContentHash, NonEmptyStr, SemanticVersion, StrictContract
from scidatafusion.contracts.routing import CapabilityName, PackName, PackReference

_MAX_REGISTRY_BYTES = 2 * 1024 * 1024
_PACKAGE_REGISTRY_ROOT = Path(__file__).resolve().parents[1] / "registries"


def canonical_hash(value: object) -> str:
    """Return a stable SHA-256 for JSON-compatible structured data."""

    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RegistryErrorCode(StrEnum):
    """Machine-readable registry loading failures."""

    NOT_FOUND = "routing_registry_not_found"
    TOO_LARGE = "routing_registry_too_large"
    INVALID_JSON = "routing_registry_invalid_json"
    INVALID_SCHEMA = "routing_registry_invalid_schema"
    HASH_MISMATCH = "routing_registry_hash_mismatch"


class RegistryLoadError(ValueError):
    """Structured error raised when an untrusted registry cannot be accepted."""

    def __init__(self, code: RegistryErrorCode, path: Path, detail: str) -> None:
        self.code = code
        self.path = path
        self.detail = detail
        super().__init__(f"{code.value}: {path}: {detail}")


class KeywordRule(StrictContract):
    """One deterministic phrase vote loaded from a pack manifest."""

    term: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=2, max_length=128),
    ]
    weight: float = Field(gt=0.0, le=10.0, allow_inf_nan=False)


class DomainPackManifest(StrictContract):
    """Validated metadata used to identify and safely enable a domain pack."""

    name: PackName
    version: SemanticVersion
    description: NonEmptyStr
    domains: tuple[PackName, ...]
    subdomains: tuple[PackName, ...] = ()
    keyword_rules: tuple[KeywordRule, ...]
    supported_archetypes: tuple[PackName, ...]
    required_capabilities: tuple[CapabilityName, ...]
    optional_capabilities: tuple[CapabilityName, ...] = ()

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        """Reject ambiguous or duplicate manifest entries."""

        if not self.domains or self.name not in self.domains:
            msg = "domain pack name must be listed in domains"
            raise ValueError(msg)
        for values, label in (
            (self.domains, "domains"),
            (self.subdomains, "subdomains"),
            (self.supported_archetypes, "supported_archetypes"),
            (self.required_capabilities, "required_capabilities"),
            (self.optional_capabilities, "optional_capabilities"),
        ):
            if len(values) != len(set(values)):
                msg = f"{label} must not contain duplicates"
                raise ValueError(msg)
        terms = tuple(rule.term.casefold() for rule in self.keyword_rules)
        if len(terms) != len(set(terms)):
            msg = "keyword terms must be unique within a pack"
            raise ValueError(msg)
        return self


class TaskPackManifest(StrictContract):
    """Validated metadata for a reusable task-archetype implementation."""

    name: PackName
    version: SemanticVersion
    description: NonEmptyStr
    archetypes: tuple[PackName, ...]
    keyword_rules: tuple[KeywordRule, ...]
    activate_with_any: tuple[PackName, ...] = ()
    required_capabilities: tuple[CapabilityName, ...]
    optional_capabilities: tuple[CapabilityName, ...] = ()

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        """Reject duplicate labels and relationship rules."""

        if not self.archetypes or self.name not in self.archetypes:
            msg = "task pack name must be listed in archetypes"
            raise ValueError(msg)
        for values, label in (
            (self.archetypes, "archetypes"),
            (self.activate_with_any, "activate_with_any"),
            (self.required_capabilities, "required_capabilities"),
            (self.optional_capabilities, "optional_capabilities"),
        ):
            if len(values) != len(set(values)):
                msg = f"{label} must not contain duplicates"
                raise ValueError(msg)
        terms = tuple(rule.term.casefold() for rule in self.keyword_rules)
        if len(terms) != len(set(terms)):
            msg = "keyword terms must be unique within a pack"
            raise ValueError(msg)
        return self


class _DomainRegistryDocument(StrictContract):
    registry_type: Literal["domain"]
    registry_version: SemanticVersion
    content_hash: ContentHash
    packs: tuple[DomainPackManifest, ...]

    @model_validator(mode="after")
    def unique_packs(self) -> Self:
        names = tuple(pack.name for pack in self.packs)
        if len(names) != len(set(names)):
            msg = "domain pack names must be unique"
            raise ValueError(msg)
        return self


class _TaskRegistryDocument(StrictContract):
    registry_type: Literal["task"]
    registry_version: SemanticVersion
    content_hash: ContentHash
    packs: tuple[TaskPackManifest, ...]

    @model_validator(mode="after")
    def unique_packs(self) -> Self:
        names = tuple(pack.name for pack in self.packs)
        if len(names) != len(set(names)):
            msg = "task pack names must be unique"
            raise ValueError(msg)
        return self


def _read_registry(path: Path) -> bytes:
    if not path.is_file():
        raise RegistryLoadError(RegistryErrorCode.NOT_FOUND, path, "file does not exist")
    size = path.stat().st_size
    if size > _MAX_REGISTRY_BYTES:
        raise RegistryLoadError(
            RegistryErrorCode.TOO_LARGE,
            path,
            f"registry is {size} bytes; maximum is {_MAX_REGISTRY_BYTES}",
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RegistryLoadError(RegistryErrorCode.NOT_FOUND, path, str(exc)) from exc


def _parse_document(
    path: Path,
    model: type[_DomainRegistryDocument] | type[_TaskRegistryDocument],
) -> _DomainRegistryDocument | _TaskRegistryDocument:
    raw = _read_registry(path)
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryLoadError(RegistryErrorCode.INVALID_JSON, path, str(exc)) from exc
    try:
        document = model.model_validate_json(raw)
    except ValidationError as exc:
        raise RegistryLoadError(RegistryErrorCode.INVALID_SCHEMA, path, str(exc)) from exc

    hash_payload = {
        "packs": parsed["packs"],
        "registry_type": parsed["registry_type"],
        "registry_version": parsed["registry_version"],
    }
    actual_hash = canonical_hash(hash_payload)
    if not hmac.compare_digest(document.content_hash, actual_hash):
        raise RegistryLoadError(
            RegistryErrorCode.HASH_MISMATCH,
            path,
            f"declared {document.content_hash}, calculated {actual_hash}",
        )
    return document


class DomainPackRegistry(StrictContract):
    """Immutable registry snapshot used by every domain-routing decision."""

    registry_version: SemanticVersion
    content_hash: ContentHash
    packs: tuple[DomainPackManifest, ...]

    @model_validator(mode="after")
    def unique_packs(self) -> Self:
        """Preserve file-loader uniqueness guarantees for direct construction too."""

        names = tuple(pack.name for pack in self.packs)
        if len(names) != len(set(names)):
            msg = "domain pack names must be unique"
            raise ValueError(msg)
        return self

    @classmethod
    def from_file(cls, path: Path | str) -> DomainPackRegistry:
        """Load and verify one domain registry JSON file."""

        resolved = Path(path).resolve()
        document = _parse_document(resolved, _DomainRegistryDocument)
        if not isinstance(document, _DomainRegistryDocument):  # pragma: no cover - type narrowing
            msg = "expected a domain registry"
            raise RegistryLoadError(RegistryErrorCode.INVALID_SCHEMA, resolved, msg)
        return cls(
            registry_version=document.registry_version,
            content_hash=document.content_hash,
            packs=document.packs,
        )

    @classmethod
    def load_default(cls) -> DomainPackRegistry:
        """Load the installed package's version-pinned domain registry."""

        return cls.from_file(_PACKAGE_REGISTRY_ROOT / "domain_packs.json")

    def get(self, name: str) -> DomainPackManifest | None:
        """Return a pack by exact stable name."""

        return next((pack for pack in self.packs if pack.name == name), None)

    def require(self, name: str) -> DomainPackManifest:
        """Return a pack or raise a structured compatibility error."""

        pack = self.get(name)
        if pack is None:
            raise KeyError(f"domain pack is not registered: {name}")
        return pack

    def reference(self, pack: DomainPackManifest) -> PackReference:
        """Build a content-addressed immutable reference for a manifest."""

        return PackReference(
            name=pack.name,
            pack_type="domain",
            version=pack.version,
            content_hash=canonical_hash(pack.model_dump(mode="json")),
        )

    @property
    def capabilities(self) -> frozenset[str]:
        """Capabilities needed to exercise every registered domain pack."""

        return frozenset(
            capability
            for pack in self.packs
            for capability in (*pack.required_capabilities, *pack.optional_capabilities)
        )


class TaskPackRegistry(StrictContract):
    """Immutable registry snapshot used by task-archetype routing."""

    registry_version: SemanticVersion
    content_hash: ContentHash
    packs: tuple[TaskPackManifest, ...]

    @model_validator(mode="after")
    def unique_packs(self) -> Self:
        """Preserve file-loader uniqueness guarantees for direct construction too."""

        names = tuple(pack.name for pack in self.packs)
        if len(names) != len(set(names)):
            msg = "task pack names must be unique"
            raise ValueError(msg)
        return self

    @classmethod
    def from_file(cls, path: Path | str) -> TaskPackRegistry:
        """Load and verify one task registry JSON file."""

        resolved = Path(path).resolve()
        document = _parse_document(resolved, _TaskRegistryDocument)
        if not isinstance(document, _TaskRegistryDocument):  # pragma: no cover - type narrowing
            msg = "expected a task registry"
            raise RegistryLoadError(RegistryErrorCode.INVALID_SCHEMA, resolved, msg)
        return cls(
            registry_version=document.registry_version,
            content_hash=document.content_hash,
            packs=document.packs,
        )

    @classmethod
    def load_default(cls) -> TaskPackRegistry:
        """Load the installed package's version-pinned task registry."""

        return cls.from_file(_PACKAGE_REGISTRY_ROOT / "task_packs.json")

    def get(self, name: str) -> TaskPackManifest | None:
        """Return a task pack by exact stable name."""

        return next((pack for pack in self.packs if pack.name == name), None)

    def require(self, name: str) -> TaskPackManifest:
        """Return a task pack or fail explicitly."""

        pack = self.get(name)
        if pack is None:
            raise KeyError(f"task pack is not registered: {name}")
        return pack

    def reference(self, pack: TaskPackManifest) -> PackReference:
        """Build a content-addressed immutable reference for a task manifest."""

        return PackReference(
            name=pack.name,
            pack_type="task",
            version=pack.version,
            content_hash=canonical_hash(pack.model_dump(mode="json")),
        )

    @property
    def capabilities(self) -> frozenset[str]:
        """Capabilities needed to exercise every registered task pack."""

        return frozenset(
            capability
            for pack in self.packs
            for capability in (*pack.required_capabilities, *pack.optional_capabilities)
        )


def combined_registry_hash(
    domain_registry: DomainPackRegistry,
    task_registry: TaskPackRegistry,
) -> str:
    """Bind decisions to both exact registry snapshots."""

    return canonical_hash(
        {
            "domain": {
                "content_hash": domain_registry.content_hash,
                "version": domain_registry.registry_version,
            },
            "task": {
                "content_hash": task_registry.content_hash,
                "version": task_registry.registry_version,
            },
        }
    )

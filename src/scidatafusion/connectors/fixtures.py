"""Packaged, no-network Ia replay transport for the M05 demonstration."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from scidatafusion.connectors.base import MemoryArtifactStore
from scidatafusion.connectors.http import ControlledHttpConnector
from scidatafusion.connectors.registry import (
    ConnectorRegistryLoader,
    calculate_connector_descriptor_hash,
)
from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.connectors import (
    ConnectorHealth,
    ConnectorRegistry,
    ConnectorRuntimeEntry,
    ConnectorRuntimeSnapshot,
    ExecutionMode,
)

_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "connector_fixtures" / "ia" / "responses.json"
)
_EXPECTED_KEYS = frozenset(
    {
        "crossref_empty",
        "crossref_en",
        "openalex_empty",
        "openalex_en_1",
        "openalex_en_2",
        "vizier_empty",
        "vizier_en",
        "zenodo_empty",
        "zenodo_en",
    }
)


@dataclass(frozen=True, slots=True)
class OfflineConnectorBundle:
    """Owned fixture Connectors plus the exact runtime snapshot they implement."""

    connectors: dict[str, ControlledHttpConnector]
    artifacts: MemoryArtifactStore
    runtime_snapshot: ConnectorRuntimeSnapshot

    async def aclose(self) -> None:
        """Close every owned mock HTTP client."""

        await asyncio.gather(*(connector.aclose() for connector in self.connectors.values()))


def build_offline_ia_connector_bundle(
    registry: ConnectorRegistry | None = None,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> OfflineConnectorBundle:
    """Build four real parsers backed only by packaged deterministic response bytes."""

    connector_registry = registry or ConnectorRegistryLoader.load_default()
    fixtures = _load_fixtures()
    checked_at = clock()
    connectors: dict[str, ControlledHttpConnector] = {}
    artifacts = MemoryArtifactStore()
    entries: list[ConnectorRuntimeEntry] = []
    for descriptor in connector_registry.connectors:
        connectors[descriptor.source_id] = ControlledHttpConnector(
            descriptor,
            transport=_transport_for_source(descriptor.source_id, fixtures),
            artifacts=artifacts,
            clock=clock,
        )
        entries.append(
            ConnectorRuntimeEntry(
                connector_id=descriptor.connector_id,
                source_id=descriptor.source_id,
                descriptor_hash=calculate_connector_descriptor_hash(descriptor),
                health=ConnectorHealth.HEALTHY,
                execution_mode=ExecutionMode.OFFLINE_FIXTURE,
                credential_available=False,
                auth_scope_id="offline.ia.fixture",
                checked_at=checked_at,
            )
        )
    return OfflineConnectorBundle(
        connectors=connectors,
        artifacts=artifacts,
        runtime_snapshot=ConnectorRuntimeSnapshot(
            connector_registry_hash=connector_registry.content_hash,
            entries=tuple(entries),
        ),
    )


def _load_fixtures() -> dict[str, object]:
    if not _FIXTURE_PATH.is_file() or _FIXTURE_PATH.stat().st_size > 2 * 1024 * 1024:
        raise RuntimeError("the packaged M05 fixture is missing or exceeds its size limit")
    try:
        parsed: object = json.loads(_FIXTURE_PATH.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("the packaged M05 fixture is not valid JSON") from exc
    if (
        not isinstance(parsed, dict)
        or any(not isinstance(key, str) for key in parsed)
        or set(parsed) != _EXPECTED_KEYS
    ):
        raise RuntimeError("the packaged M05 fixture has an unexpected schema")
    return {str(key): value for key, value in parsed.items()}


def _transport_for_source(source_id: str, fixtures: dict[str, object]) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        query_text = _request_query_text(source_id, request)
        if _contains_non_ascii(query_text):
            key = {
                "openalex_literature": "openalex_empty",
                "zenodo_repository": "zenodo_empty",
                "vizier_tap": "vizier_empty",
                "supplement_web": "crossref_empty",
            }[source_id]
        elif source_id == "openalex_literature":
            key = (
                "openalex_en_2"
                if request.url.params.get("cursor") == "fixture-page-2"
                else "openalex_en_1"
            )
        else:
            key = {
                "zenodo_repository": "zenodo_en",
                "vizier_tap": "vizier_en",
                "supplement_web": "crossref_en",
            }[source_id]
        content = json.dumps(
            fixtures[key],
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return httpx.Response(
            200,
            content=content,
            headers={"Content-Type": "application/json"},
        )

    return httpx.MockTransport(handler)


def _request_query_text(source_id: str, request: httpx.Request) -> str:
    if source_id == "openalex_literature":
        return _query_parameter(request, "search")
    if source_id == "zenodo_repository":
        return _query_parameter(request, "q")
    if source_id == "supplement_web":
        return _query_parameter(request, "query.bibliographic")
    form = parse_qs(request.content.decode("utf-8"))
    return form.get("QUERY", [""])[0]


def _query_parameter(request: httpx.Request, name: str) -> str:
    value: object = request.url.params.get(name, "")
    return value if isinstance(value, str) else ""


def _contains_non_ascii(value: str) -> bool:
    return any(ord(character) > 127 for character in value)

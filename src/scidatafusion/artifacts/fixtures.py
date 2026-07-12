"""Packaged no-network M07 acquisition fixture for the Ia vertical slice."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from scidatafusion.artifacts.integrity import (
    calculate_candidate_locator_hash,
    calculate_download_runtime_hash,
    calculate_url_locator_hash,
)
from scidatafusion.contracts.artifacts import (
    DownloadApprovalKind,
    DownloadExecutionMode,
    DownloadPolicy,
    DownloadRuntimeSnapshot,
    SourceDownloadApproval,
)
from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.selection import LicenseDecision, SelectedSourceSet
from scidatafusion.domain.registry import canonical_hash

_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "artifact_fixtures" / "ia" / "downloads.json"
)


@dataclass(frozen=True, slots=True)
class OfflineArtifactBundle:
    """Mock transport plus the exact offline authorization snapshot it implements."""

    transport: httpx.MockTransport
    runtime: DownloadRuntimeSnapshot
    policy: DownloadPolicy
    approvals: tuple[SourceDownloadApproval, ...]


@dataclass(frozen=True, slots=True)
class _FixtureResponse:
    status_code: int
    content: bytes
    headers: dict[str, str]


def build_offline_ia_artifact_bundle(
    selected: SelectedSourceSet,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> OfflineArtifactBundle:
    """Build a deterministic fixture runtime without external network authorization."""

    fixture = _load_fixture()
    checked_at = clock()
    fixture_id = _required_text(fixture, "fixture_id")
    allowed_hosts = _string_tuple(fixture.get("allowed_hosts"), label="allowed_hosts")
    runtime_draft = DownloadRuntimeSnapshot(
        execution_mode=DownloadExecutionMode.OFFLINE_FIXTURE,
        network_enabled=False,
        allowed_hosts=allowed_hosts,
        fixture_id=fixture_id,
        checked_at=checked_at,
        runtime_hash="0" * 64,
    )
    runtime = runtime_draft.model_copy(
        update={"runtime_hash": calculate_download_runtime_hash(runtime_draft)}
    )
    derived = _derived_urls(fixture.get("approved_derived_urls"))
    approval_principal = canonical_hash(
        {"fixture_id": fixture_id, "purpose": "offline-license-simulation"}
    )
    approvals: list[SourceDownloadApproval] = []
    for source in selected.sources:
        if source.license_decision is LicenseDecision.ALLOWED:
            continue
        hashes = [calculate_candidate_locator_hash(locator) for locator in source.download_locators]
        for locator in source.download_locators:
            for url in derived.get(locator.value, ()):
                hashes.append(calculate_url_locator_hash(url))
        approvals.append(
            SourceDownloadApproval(
                candidate_id=source.candidate_id,
                kind=DownloadApprovalKind.OFFLINE_FIXTURE,
                approval_ref=f"offline-fixture:{fixture_id}:{source.candidate_id}",
                approved_by_hash=approval_principal,
                locator_hashes=tuple(dict.fromkeys(hashes)),
                approved_at=checked_at,
            )
        )
    max_total = max(1, selected.reserved_download_bytes)
    policy = DownloadPolicy(
        max_total_bytes=max_total,
        max_file_bytes=min(1_000_000, max_total),
        max_archive_uncompressed_bytes=1_000_000,
        max_archive_member_bytes=500_000,
        max_archive_entries=100,
        max_archive_depth=1,
        max_root_locators_per_source=2,
        max_attachments_per_landing=5,
    )
    responses = _responses(fixture.get("responses"))

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            return httpx.Response(405, content=b"")
        response = responses.get(str(request.url))
        if response is None:
            return httpx.Response(404, content=b"fixture URL not found")
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers=response.headers,
        )

    return OfflineArtifactBundle(
        transport=httpx.MockTransport(handler),
        runtime=runtime,
        policy=policy,
        approvals=tuple(approvals),
    )


def _load_fixture() -> dict[str, object]:
    if not _FIXTURE_PATH.is_file() or _FIXTURE_PATH.stat().st_size > 2 * 1024 * 1024:
        raise RuntimeError("the packaged M07 fixture is missing or exceeds its size limit")
    try:
        parsed: object = json.loads(_FIXTURE_PATH.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("the packaged M07 fixture is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("the packaged M07 fixture must be a JSON object")
    expected = {"allowed_hosts", "approved_derived_urls", "fixture_id", "responses"}
    if set(parsed) != expected:
        raise RuntimeError("the packaged M07 fixture has an unexpected schema")
    return {str(key): value for key, value in parsed.items()}


def _responses(value: object) -> dict[str, _FixtureResponse]:
    if not isinstance(value, list) or len(value) > 100:
        raise RuntimeError("M07 fixture responses must be a bounded list")
    result: dict[str, _FixtureResponse] = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "content_base64",
            "headers",
            "status_code",
            "url",
        }:
            raise RuntimeError("M07 fixture response has an unexpected schema")
        url = _required_text(item, "url")
        status = item.get("status_code")
        headers_value = item.get("headers")
        encoded = item.get("content_base64")
        if (
            url in result
            or not isinstance(status, int)
            or not 100 <= status <= 599
            or not isinstance(headers_value, dict)
            or any(
                not isinstance(key, str) or not isinstance(header, str)
                for key, header in headers_value.items()
            )
            or not isinstance(encoded, str)
        ):
            raise RuntimeError("M07 fixture response values are invalid")
        try:
            content = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise RuntimeError("M07 fixture response base64 is invalid") from exc
        result[url] = _FixtureResponse(
            content=content,
            headers={str(key): str(header) for key, header in headers_value.items()},
            status_code=status,
        )
    return result


def _derived_urls(value: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise RuntimeError("M07 fixture derived URL map is invalid")
    result: dict[str, tuple[str, ...]] = {}
    for key, urls in value.items():
        if not isinstance(key, str):
            raise RuntimeError("M07 fixture derived URL key is invalid")
        result[key] = _string_tuple(urls, label="approved_derived_urls")
    return result


def _string_tuple(value: object, *, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise RuntimeError(f"M07 fixture {label} must be a non-empty string list")
    result = tuple(str(item) for item in value)
    if len(result) != len(set(result)):
        raise RuntimeError(f"M07 fixture {label} values must be unique")
    return result


def _required_text(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise RuntimeError(f"M07 fixture {key} must be non-empty text")
    return item

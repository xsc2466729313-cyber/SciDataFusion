from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from scidatafusion.artifacts.fixtures import (
    OfflineArtifactBundle,
    build_offline_ia_artifact_bundle,
)
from scidatafusion.artifacts.integrity import (
    calculate_url_locator_hash,
    verify_artifact_download_integrity,
    verify_artifact_download_request_integrity,
)
from scidatafusion.artifacts.service import ArtifactDownloadService
from scidatafusion.artifacts.storage import BronzeWriteReceipt, MemoryBronzeStore
from scidatafusion.cli import _build_search_planning, _execute_offline_connectors
from scidatafusion.contracts.artifacts import (
    ArtifactDownloadRequest,
    ArtifactDownloadStatus,
    ArtifactKind,
    ArtifactRelationship,
    DownloadAttemptStatus,
    DownloadErrorCode,
)
from scidatafusion.contracts.selection import SelectedSourceSet, SourceSelectionRequest
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.selection import SourceSelectionService

NOW = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
GOAL = "Study Type Ia supernova light curves using multi-source integration into CSV."


class _CountingTransport(httpx.AsyncBaseTransport):
    def __init__(self, delegate: httpx.AsyncBaseTransport) -> None:
        self.delegate = delegate
        self.request_count = 0
        self.requests: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request_count += 1
        self.requests.append(str(request.url))
        await asyncio.sleep(0)
        return await self.delegate.handle_async_request(request)

    async def aclose(self) -> None:
        await self.delegate.aclose()


class _BlockingTransport(httpx.AsyncBaseTransport):
    def __init__(self, delegate: httpx.AsyncBaseTransport) -> None:
        self.delegate = delegate
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.request_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request_count += 1
        if self.request_count == 1:
            self.entered.set()
            await self.release.wait()
        return await self.delegate.handle_async_request(request)

    async def aclose(self) -> None:
        await self.delegate.aclose()


class _SelfLinkTransport(httpx.AsyncBaseTransport):
    def __init__(self, delegate: httpx.AsyncBaseTransport) -> None:
        self.delegate = delegate
        self.request_count = 0
        self.requests: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request_count += 1
        self.requests.append(str(request.url))
        if str(request.url) == "https://papers.example/snia-a":
            return httpx.Response(
                200,
                content=(
                    b"<!doctype html><html><body>"
                    b'<a download href="https://papers.example/snia-a">self</a>'
                    b'<a download href="/files/snia-paper.pdf">pdf</a>'
                    b"</body></html>"
                ),
                headers={"Content-Type": "text/html; charset=utf-8"},
            )
        return await self.delegate.handle_async_request(request)

    async def aclose(self) -> None:
        await self.delegate.aclose()


class _FailingStore:
    def __init__(self, *, fail_on_put: int) -> None:
        self.delegate = MemoryBronzeStore()
        self.fail_on_put = fail_on_put
        self.put_count = 0
        self.persisted_hashes: set[str] = set()

    def put(self, content: bytes) -> BronzeWriteReceipt:
        self.put_count += 1
        if self.put_count == self.fail_on_put:
            raise AppError(ErrorCode.INTERNAL_ERROR, "injected Bronze failure")
        receipt = self.delegate.put(content)
        self.persisted_hashes.add(receipt.byte_sha256)
        return receipt

    def read(self, byte_sha256: str) -> bytes:
        return self.delegate.read(byte_sha256)

    def contains(self, byte_sha256: str) -> bool:
        return self.delegate.contains(byte_sha256)


@pytest.fixture(scope="module")
def selected_source_set() -> SelectedSourceSet:
    phase1, planning = _build_search_planning(GOAL, "authenticated-m07-reviewer")
    assert planning is not None
    assert phase1.confirmation is not None
    connector_result = asyncio.run(_execute_offline_connectors(planning))
    selection = SourceSelectionService(clock=lambda: NOW).select(
        SourceSelectionRequest(
            contract=phase1.confirmation.contract,
            search_plan=planning.plan,
            connector_result=connector_result,
        )
    )
    return selection.selected_source_set


def _request(
    selected: SelectedSourceSet, *, approvals: bool = True
) -> tuple[OfflineArtifactBundle, ArtifactDownloadRequest]:
    bundle = build_offline_ia_artifact_bundle(selected, clock=lambda: NOW)
    request = ArtifactDownloadRequest(
        selected_source_set=selected,
        policy=bundle.policy,
        runtime=bundle.runtime,
        approvals=bundle.approvals if approvals else (),
        requested_at=NOW,
    )
    return bundle, request


def test_offline_m07_builds_replayable_deduplicated_bronze_manifest(
    selected_source_set: SelectedSourceSet,
) -> None:
    bundle, request = _request(selected_source_set)
    store = MemoryBronzeStore()
    service = ArtifactDownloadService(
        store=store,
        transport=bundle.transport,
        clock=lambda: NOW,
    )

    result = asyncio.run(service.execute(request))
    replay = asyncio.run(service.execute(request))

    assert replay is result
    assert result.status is ArtifactDownloadStatus.PARTIAL
    assert result.metrics.selected_source_count == 3
    assert result.metrics.attempted_download_count == 5
    assert result.metrics.stored_download_count == 3
    assert result.metrics.deduplicated_download_count == 1
    assert result.metrics.skipped_download_count == 1
    assert result.metrics.failed_download_count == 0
    assert result.metrics.quarantined_download_count == 0
    assert result.metrics.acquisition_count == 6
    assert result.metrics.archive_member_count == 2
    assert result.metrics.bronze_object_count == 5
    assert len(result.events) == 5
    assert all(event.event_type.value == "artifact.stored" for event in result.events)
    assert all(store.read(item.byte_sha256) for item in result.artifact_set.objects)

    relationships = [item.relationship for item in result.manifest.acquisitions]
    assert relationships.count(ArtifactRelationship.ROOT_DOWNLOAD) == 3
    assert relationships.count(ArtifactRelationship.LANDING_ATTACHMENT) == 1
    assert relationships.count(ArtifactRelationship.ARCHIVE_MEMBER) == 2
    assert {item.media.artifact_kind for item in result.artifact_set.objects} == {
        ArtifactKind.ARCHIVE,
        ArtifactKind.DOCUMENT,
        ArtifactKind.LANDING_PAGE,
        ArtifactKind.TABLE,
    }
    csv_objects = [
        item for item in result.artifact_set.objects if item.media.detected_media_type == "text/csv"
    ]
    assert len(csv_objects) == 1
    csv_acquisitions = [
        item for item in result.manifest.acquisitions if item.object_id == csv_objects[0].object_id
    ]
    assert len(csv_acquisitions) == 2
    assert {item.status.value for item in csv_acquisitions} == {
        "stored",
        "deduplicated",
    }
    assert any(
        item.error_code is DownloadErrorCode.LOCATOR_UNSUPPORTED for item in result.run_log.attempts
    )
    serialized = result.model_dump_json()
    assert "evil.example" not in serialized
    assert "malware.exe" not in serialized
    verify_artifact_download_integrity(result, request, store)


def test_missing_license_approval_persists_no_bytes(
    selected_source_set: SelectedSourceSet,
) -> None:
    bundle, request = _request(selected_source_set, approvals=False)
    store = MemoryBronzeStore()
    result = asyncio.run(
        ArtifactDownloadService(
            store=store,
            transport=bundle.transport,
            clock=lambda: NOW,
        ).execute(request)
    )

    assert result.status is ArtifactDownloadStatus.NEEDS_REVIEW
    assert result.artifact_set.objects == ()
    assert result.manifest.acquisitions == ()
    assert result.events == ()
    assert result.metrics.received_bytes == 0
    assert result.metrics.skipped_download_count == 4
    assert {item.error_code for item in result.run_log.attempts} == {
        DownloadErrorCode.LICENSE_APPROVAL_REQUIRED,
        DownloadErrorCode.LOCATOR_UNSUPPORTED,
    }
    assert all(item.status is DownloadAttemptStatus.SKIPPED for item in result.run_log.attempts)


def test_concurrent_identical_requests_share_one_download_execution(
    selected_source_set: SelectedSourceSet,
) -> None:
    bundle, request = _request(selected_source_set)
    transport = _CountingTransport(bundle.transport)
    service = ArtifactDownloadService(
        store=MemoryBronzeStore(),
        transport=transport,
        clock=lambda: NOW,
    )

    async def execute_twice() -> tuple[object, object]:
        first, second = await asyncio.gather(
            service.execute(request),
            service.execute(request),
        )
        return first, second

    first, second = asyncio.run(execute_twice())

    assert first is second
    assert transport.request_count == 5, transport.requests


def test_cancelled_follower_does_not_cancel_shared_download(
    selected_source_set: SelectedSourceSet,
) -> None:
    bundle, request = _request(selected_source_set)

    async def run_scenario() -> tuple[object, object, int, bool]:
        transport = _BlockingTransport(bundle.transport)
        service = ArtifactDownloadService(
            store=MemoryBronzeStore(),
            transport=transport,
            clock=lambda: NOW,
        )
        owner = asyncio.create_task(service.execute(request))
        await transport.entered.wait()
        follower = asyncio.create_task(service.execute(request))
        await asyncio.sleep(0)
        follower.cancel()
        with pytest.raises(asyncio.CancelledError):
            await follower
        transport.release.set()
        result = await owner
        replay = await service.execute(request)
        return result, replay, transport.request_count, not service._inflight

    result, replay, request_count, no_inflight = asyncio.run(run_scenario())

    assert result is replay
    assert request_count == 5
    assert no_inflight


def test_duplicate_self_attachment_is_ignored_before_attempt_creation(
    selected_source_set: SelectedSourceSet,
) -> None:
    bundle, request = _request(selected_source_set)
    transport = _SelfLinkTransport(bundle.transport)
    store = MemoryBronzeStore()
    result = asyncio.run(
        ArtifactDownloadService(
            store=store,
            transport=transport,
            clock=lambda: NOW,
        ).execute(request)
    )

    assert transport.request_count == 5, transport.requests
    assert result.metrics.attempted_download_count == 5
    verify_artifact_download_integrity(result, request, store)


def test_redirect_target_requires_its_own_license_approval(
    selected_source_set: SelectedSourceSet,
) -> None:
    bundle, request = _request(selected_source_set)
    final_hash = calculate_url_locator_hash("https://zenodo.org/api/files/lightcurve.csv")
    approvals = tuple(
        approval.model_copy(
            update={
                "locator_hashes": tuple(
                    item for item in approval.locator_hashes if item != final_hash
                )
            }
        )
        for approval in request.approvals
    )
    restricted_request = request.model_copy(update={"approvals": approvals})
    store = MemoryBronzeStore()
    result = asyncio.run(
        ArtifactDownloadService(
            store=store,
            transport=bundle.transport,
            clock=lambda: NOW,
        ).execute(restricted_request)
    )

    assert any(
        item.error_code is DownloadErrorCode.LICENSE_APPROVAL_REQUIRED
        and item.status is DownloadAttemptStatus.FAILED
        for item in result.run_log.attempts
    )
    assert all(
        item.response is None or item.response.final_locator_hash != final_hash
        for item in result.manifest.acquisitions
    )
    verify_artifact_download_integrity(result, restricted_request, store)


def test_storage_failure_is_audited_without_dangling_manifest_references(
    selected_source_set: SelectedSourceSet,
) -> None:
    bundle, request = _request(selected_source_set)
    store = _FailingStore(fail_on_put=2)
    result = asyncio.run(
        ArtifactDownloadService(
            store=store,
            transport=bundle.transport,
            clock=lambda: NOW,
        ).execute(request)
    )

    storage_failures = [
        item
        for item in result.run_log.attempts
        if item.error_code is DownloadErrorCode.STORAGE_ERROR
    ]
    assert len(storage_failures) == 1
    assert storage_failures[0].status is DownloadAttemptStatus.FAILED
    object_ids = {item.object_id for item in result.artifact_set.objects}
    assert all(item.object_id in object_ids for item in result.manifest.acquisitions)
    assert store.persisted_hashes - {item.byte_sha256 for item in result.artifact_set.objects}
    verify_artifact_download_integrity(result, request, store)


def test_m07_integrity_rejects_runtime_and_bronze_metadata_tampering(
    selected_source_set: SelectedSourceSet,
) -> None:
    bundle, request = _request(selected_source_set)
    store = MemoryBronzeStore()
    result = asyncio.run(
        ArtifactDownloadService(
            store=store,
            transport=bundle.transport,
            clock=lambda: NOW,
        ).execute(request)
    )

    tampered_runtime = request.runtime.model_copy(update={"runtime_hash": "f" * 64})
    with pytest.raises(AppError) as runtime_error:
        verify_artifact_download_request_integrity(
            request.model_copy(update={"runtime": tampered_runtime})
        )
    assert runtime_error.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR

    obj = result.artifact_set.objects[0]
    tampered_obj = obj.model_copy(update={"recorded_at": NOW.replace(hour=9)})
    tampered_set = result.artifact_set.model_copy(
        update={"objects": (tampered_obj, *result.artifact_set.objects[1:])}
    )
    with pytest.raises(AppError, match="metadata hash"):
        verify_artifact_download_integrity(
            result.model_copy(update={"artifact_set": tampered_set}),
            request,
            store,
        )

    event = result.events[0]
    tampered_event = event.model_copy(
        update={"producer": event.producer.model_copy(update={"component": "forged_service"})}
    )
    with pytest.raises(AppError, match="immutable hashes"):
        verify_artifact_download_integrity(
            result.model_copy(update={"events": (tampered_event, *result.events[1:])}),
            request,
            store,
        )

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

import scidatafusion.artifacts.checkpoints as checkpoint_module
from scidatafusion.artifacts.checkpoints import (
    FileSystemArtifactCheckpointStore,
    MemoryArtifactCheckpointStore,
)
from scidatafusion.artifacts.fixtures import (
    OfflineArtifactBundle,
    build_offline_ia_artifact_bundle,
)
from scidatafusion.artifacts.integrity import (
    calculate_download_runtime_hash,
    calculate_url_locator_hash,
    verify_artifact_download_integrity,
    verify_artifact_download_request_integrity,
)
from scidatafusion.artifacts.service import ArtifactDownloadService, _retry_delay
from scidatafusion.artifacts.storage import (
    BronzeWriteReceipt,
    FileSystemBronzeStore,
    MemoryBronzeStore,
)
from scidatafusion.cli import _build_search_planning, _execute_offline_connectors
from scidatafusion.contracts.artifacts import (
    ArtifactDownloadRequest,
    ArtifactDownloadResult,
    ArtifactDownloadStatus,
    ArtifactKind,
    ArtifactRelationship,
    DownloadAttemptStatus,
    DownloadErrorCode,
    DownloadExecutionMode,
    DownloadRuntimeSnapshot,
)
from scidatafusion.contracts.selection import SelectedSourceSet, SourceSelectionRequest
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.selection import (
    SourceSelectionService,
    calculate_selected_source_set_hash,
)

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


class _RetryOnceTransport(httpx.AsyncBaseTransport):
    def __init__(self, delegate: httpx.AsyncBaseTransport) -> None:
        self.delegate = delegate
        self.failed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://data.example/SNIa" and not self.failed:
            self.failed = True
            return httpx.Response(429, headers={"Retry-After": "Sun, 12 Jul 2026 08:00:03 GMT"})
        return await self.delegate.handle_async_request(request)

    async def aclose(self) -> None:
        await self.delegate.aclose()


class _IncompleteOnceTransport(httpx.AsyncBaseTransport):
    def __init__(self, delegate: httpx.AsyncBaseTransport) -> None:
        self.delegate = delegate
        self.failed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://data.example/SNIa" and not self.failed:
            self.failed = True
            return httpx.Response(200, content=b"short", headers={"Content-Length": "10"})
        return await self.delegate.handle_async_request(request)

    async def aclose(self) -> None:
        await self.delegate.aclose()


class _CloseSensitiveTransport(httpx.AsyncBaseTransport):
    def __init__(self, delegate: httpx.AsyncBaseTransport) -> None:
        self.delegate = delegate
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.closed:
            raise httpx.ConnectError("transport was closed early", request=request)
        return await self.delegate.handle_async_request(request)

    async def aclose(self) -> None:
        self.closed = True
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


def _single_source_set(selected: SelectedSourceSet) -> SelectedSourceSet:
    source = selected.sources[0]
    draft = SelectedSourceSet.model_validate(
        selected.model_copy(
            update={
                "selection_id": f"sel_{'0' * 32}",
                "reserved_download_bytes": source.budget_reservation_bytes,
                "sources": (source,),
                "selected_source_set_hash": "0" * 64,
            }
        ).model_dump(mode="python")
    )
    selected_hash = calculate_selected_source_set_hash(draft)
    return SelectedSourceSet.model_validate(
        draft.model_copy(
            update={
                "selection_id": f"sel_{selected_hash[:32]}",
                "selected_source_set_hash": selected_hash,
            }
        ).model_dump(mode="python")
    )


def _two_sources_sharing_one_url(selected: SelectedSourceSet) -> SelectedSourceSet:
    first, second = selected.sources[:2]
    second = second.model_copy(update={"download_locators": (first.download_locators[0],)})
    sources = (first, second)
    draft = SelectedSourceSet.model_validate(
        selected.model_copy(
            update={
                "selection_id": f"sel_{'0' * 32}",
                "reserved_download_bytes": sum(item.budget_reservation_bytes for item in sources),
                "sources": sources,
                "selected_source_set_hash": "0" * 64,
            }
        ).model_dump(mode="python")
    )
    selected_hash = calculate_selected_source_set_hash(draft)
    return SelectedSourceSet.model_validate(
        draft.model_copy(
            update={
                "selection_id": f"sel_{selected_hash[:32]}",
                "selected_source_set_hash": selected_hash,
            }
        ).model_dump(mode="python")
    )


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
    assert len(result.events) == 6
    assert sum(event.event_type.value == "artifact.stored" for event in result.events) == 5
    assert result.events[-1].event_type.value == "artifact.download.completed"
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


def test_durable_checkpoint_replays_across_services_without_network(
    selected_source_set: SelectedSourceSet,
    tmp_path: Path,
) -> None:
    bundle, request = _request(selected_source_set)
    bronze_store = FileSystemBronzeStore(tmp_path / "bronze")
    checkpoints = FileSystemArtifactCheckpointStore(tmp_path / "checkpoints")
    first_transport = _CountingTransport(bundle.transport)
    first = asyncio.run(
        ArtifactDownloadService(
            store=bronze_store,
            checkpoints=checkpoints,
            transport=first_transport,
            clock=lambda: NOW,
        ).execute(request)
    )
    assert first_transport.request_count == 5

    replay_calls = 0

    async def reject_network(_: httpx.Request) -> httpx.Response:
        nonlocal replay_calls
        replay_calls += 1
        raise AssertionError("durable M07 replay must not call the transport")

    replay = asyncio.run(
        ArtifactDownloadService(
            store=bronze_store,
            checkpoints=checkpoints,
            transport=httpx.MockTransport(reject_network),
            clock=lambda: NOW + timedelta(days=1),
        ).execute(request)
    )

    assert replay_calls == 0
    assert replay == first
    assert replay.model_dump_json() == first.model_dump_json()

    checkpoint_file = next((tmp_path / "checkpoints").rglob("*.json"))
    checkpoint_file.write_text("{}", encoding="utf-8")
    with pytest.raises(AppError) as tampered:
        asyncio.run(
            ArtifactDownloadService(
                store=bronze_store,
                checkpoints=checkpoints,
                transport=httpx.MockTransport(reject_network),
            ).execute(request)
        )
    assert tampered.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_checkpoint_stores_reject_conflicts_invalid_keys_and_unsafe_files(
    selected_source_set: SelectedSourceSet,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, request = _request(selected_source_set)
    result = asyncio.run(
        ArtifactDownloadService(
            store=MemoryBronzeStore(),
            transport=bundle.transport,
            clock=lambda: NOW,
        ).execute(request)
    )

    memory = MemoryArtifactCheckpointStore()
    assert memory.load(result.idempotency_key) is None
    assert memory.save(result) is result
    assert memory.save(result) is result
    conflicting = result.model_copy(update={"producer_version": "9.9.9"})
    with pytest.raises(AppError, match="different checkpoint"):
        memory.save(conflicting)
    with pytest.raises(AppError) as invalid_key:
        memory.load("not-a-sha256")
    assert invalid_key.value.code is ErrorCode.INVALID_REQUEST

    checkpoint_root = tmp_path / "strict-checkpoints"
    filesystem = FileSystemArtifactCheckpointStore(checkpoint_root)
    assert filesystem.save(result) == result
    assert filesystem.save(result) == result
    original_file = next(checkpoint_root.rglob("*.json"))
    with pytest.raises(AppError, match="different checkpoint"):
        filesystem.save(conflicting)

    original_read_bytes = Path.read_bytes

    def fail_checkpoint_read(path: Path) -> bytes:
        if path == original_file:
            raise OSError("injected checkpoint read failure")
        return original_read_bytes(path)

    with monkeypatch.context() as unreadable:
        unreadable.setattr(Path, "read_bytes", fail_checkpoint_read)
        with pytest.raises(AppError) as read_failure:
            filesystem.load(result.idempotency_key)
        assert read_failure.value.code is ErrorCode.INTERNAL_ERROR

    wrong_key = "f" * 64 if result.idempotency_key != "f" * 64 else "e" * 64
    wrong_target = checkpoint_root / wrong_key[:2] / f"{wrong_key}.json"
    wrong_target.parent.mkdir(parents=True)
    wrong_target.write_bytes(original_file.read_bytes())
    with pytest.raises(AppError, match="content-addressed key"):
        filesystem.load(wrong_key)

    directory_key = "d" * 64
    directory_target = checkpoint_root / directory_key[:2] / f"{directory_key}.json"
    directory_target.mkdir(parents=True)
    with pytest.raises(AppError, match="regular immutable file"):
        filesystem.load(directory_key)

    with monkeypatch.context() as bounded:
        bounded.setattr(checkpoint_module, "_MAX_CHECKPOINT_BYTES", 1)
        with pytest.raises(AppError, match="metadata size limit"):
            filesystem.load(result.idempotency_key)
        empty_store = FileSystemArtifactCheckpointStore(tmp_path / "bounded-checkpoints")
        with pytest.raises(AppError) as oversized:
            empty_store.save(result)
        assert oversized.value.code is ErrorCode.VALIDATION_FAILED

    invalid_root = tmp_path / "checkpoint-root-file"
    invalid_root.write_text("not a directory", encoding="ascii")
    with pytest.raises(AppError) as invalid_store:
        FileSystemArtifactCheckpointStore(invalid_root)
    assert invalid_store.value.code is ErrorCode.CONFIGURATION_ERROR

    original_link = os.link
    racing_store = FileSystemArtifactCheckpointStore(tmp_path / "racing-checkpoints")

    def publish_then_report_race(source: Path, target: Path) -> None:
        original_link(source, target)
        raise FileExistsError("injected concurrent publication")

    with monkeypatch.context() as racing:
        racing.setattr(os, "link", publish_then_report_race)
        assert racing_store.save(result) == result

    broken_store = FileSystemArtifactCheckpointStore(tmp_path / "broken-checkpoints")

    def fail_publication(_: Path, __: Path) -> None:
        raise OSError("injected publication failure")

    with monkeypatch.context() as broken:
        broken.setattr(os, "link", fail_publication)
        with pytest.raises(AppError) as publication_failure:
            broken_store.save(result)
        assert publication_failure.value.code is ErrorCode.INTERNAL_ERROR

    blocked_root = tmp_path / "blocked-shard-checkpoints"
    blocked_store = FileSystemArtifactCheckpointStore(blocked_root)
    (blocked_root / result.idempotency_key[:2]).write_text("not a directory", encoding="ascii")
    with pytest.raises(AppError) as blocked_shard:
        blocked_store.save(result)
    assert blocked_shard.value.code is ErrorCode.INTERNAL_ERROR


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
    assert len(result.events) == 1
    assert result.events[0].event_type.value == "artifact.download.completed"
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
    transport = _CountingTransport(bundle.transport)
    result = asyncio.run(
        ArtifactDownloadService(
            store=store,
            transport=transport,
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
    assert "https://zenodo.org/api/files/lightcurve.csv" not in transport.requests
    verify_artifact_download_integrity(result, restricted_request, store)


def test_retry_after_controls_bounded_retry_without_losing_attempt_audit(
    selected_source_set: SelectedSourceSet,
) -> None:
    bundle, request = _request(selected_source_set)
    delays: list[float] = []

    async def no_sleep(delay: float) -> None:
        delays.append(delay)

    result = asyncio.run(
        ArtifactDownloadService(
            store=MemoryBronzeStore(),
            transport=_RetryOnceTransport(bundle.transport),
            clock=lambda: NOW,
            sleep=no_sleep,
        ).execute(request)
    )

    retries = [
        item
        for item in result.run_log.attempts
        if item.locator.safe_url == "https://data.example/SNIa"
    ]
    assert [item.http_status for item in retries] == [429, 200]
    assert retries[0].retryable
    assert delays == [3.0]

    conservative_policy = request.policy.model_copy(
        update={
            "base_backoff_seconds": 4.0,
            "max_backoff_seconds": 8.0,
            "max_retry_after_seconds": 1.0,
        }
    )
    assert (
        _retry_delay(
            conservative_policy,
            attempt_number=1,
            retry_after_seconds=0.0,
        )
        == 4.0
    )


def test_recovered_retry_can_finish_successfully(
    selected_source_set: SelectedSourceSet,
) -> None:
    selected = _single_source_set(selected_source_set)
    bundle, request = _request(selected)

    async def no_sleep(_: float) -> None:
        return None

    result = asyncio.run(
        ArtifactDownloadService(
            store=MemoryBronzeStore(),
            transport=_RetryOnceTransport(bundle.transport),
            clock=lambda: NOW,
            sleep=no_sleep,
        ).execute(request)
    )

    assert result.status is ArtifactDownloadStatus.SUCCEEDED
    assert result.metrics.failed_download_count == 1
    assert result.run_log.attempts[0].retryable
    assert result.run_log.attempts[-1].status is DownloadAttemptStatus.STORED


def test_incomplete_stream_is_accounted_and_retried(
    selected_source_set: SelectedSourceSet,
) -> None:
    bundle, request = _request(selected_source_set)
    delays: list[float] = []

    async def no_sleep(delay: float) -> None:
        delays.append(delay)

    result = asyncio.run(
        ArtifactDownloadService(
            store=MemoryBronzeStore(),
            transport=_IncompleteOnceTransport(bundle.transport),
            clock=lambda: NOW,
            sleep=no_sleep,
        ).execute(request)
    )

    attempts = [
        item
        for item in result.run_log.attempts
        if item.locator.safe_url == "https://data.example/SNIa"
    ]
    assert [item.error_code for item in attempts] == [DownloadErrorCode.INCOMPLETE_RESPONSE, None]
    assert attempts[0].bytes_received == 5
    assert attempts[0].retryable
    assert delays == [0.25]


def test_all_transport_failures_are_failed_not_unsupported(
    selected_source_set: SelectedSourceSet,
) -> None:
    bundle, request = _request(selected_source_set)

    async def unavailable(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async def no_sleep(_: float) -> None:
        return None

    result = asyncio.run(
        ArtifactDownloadService(
            store=MemoryBronzeStore(),
            transport=httpx.MockTransport(unavailable),
            clock=lambda: NOW,
            sleep=no_sleep,
        ).execute(request)
    )

    assert bundle.policy.max_attempts == 2
    assert result.status is ArtifactDownloadStatus.FAILED
    assert result.metrics.failed_download_count > 0
    assert not result.artifact_set.objects
    terminal_attempts = {
        (item.candidate_id, item.locator.locator_hash): item for item in result.run_log.attempts
    }
    assert all(not item.retryable for item in terminal_attempts.values())


def test_shared_transport_is_closed_only_by_service_owner(
    selected_source_set: SelectedSourceSet,
) -> None:
    bundle, request = _request(selected_source_set)
    transport = _CloseSensitiveTransport(bundle.transport)
    service = ArtifactDownloadService(
        store=MemoryBronzeStore(),
        transport=transport,
        clock=lambda: NOW,
    )
    later_request = request.model_copy(update={"requested_at": NOW + timedelta(seconds=1)})

    async def execute_both() -> tuple[
        ArtifactDownloadResult,
        ArtifactDownloadResult,
        bool,
    ]:
        first = await service.execute(request)
        second = await service.execute(later_request)
        open_before_close = not transport.closed
        await service.aclose()
        return first, second, open_before_close

    first, second, open_before_close = asyncio.run(execute_both())

    assert open_before_close
    assert transport.closed
    assert first.status is ArtifactDownloadStatus.PARTIAL
    assert second.status is ArtifactDownloadStatus.PARTIAL
    assert not any(
        item.error_code is DownloadErrorCode.TIMEOUT
        for result in (first, second)
        for item in result.run_log.attempts
    )


def test_live_service_fails_closed_without_trusted_request_authorizer(
    selected_source_set: SelectedSourceSet,
) -> None:
    bundle, _ = _request(selected_source_set)
    runtime_draft = DownloadRuntimeSnapshot(
        execution_mode=DownloadExecutionMode.LIVE_NETWORK,
        network_enabled=True,
        allowed_hosts=("example.org",),
        checked_at=NOW,
        runtime_hash="0" * 64,
    )
    runtime = runtime_draft.model_copy(
        update={"runtime_hash": calculate_download_runtime_hash(runtime_draft)}
    )
    live_request = ArtifactDownloadRequest(
        selected_source_set=selected_source_set,
        policy=bundle.policy,
        runtime=runtime,
        requested_at=NOW,
    )
    transport_calls = 0

    async def reject_transport(_: httpx.Request) -> httpx.Response:
        nonlocal transport_calls
        transport_calls += 1
        raise AssertionError("unauthorized live request must not reach transport")

    with pytest.raises(AppError) as blocked:
        asyncio.run(
            ArtifactDownloadService(
                store=MemoryBronzeStore(),
                transport=httpx.MockTransport(reject_transport),
            ).execute(live_request)
        )
    assert blocked.value.code is ErrorCode.SECURITY_POLICY_VIOLATION
    assert transport_calls == 0


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


def test_cache_hit_storage_failure_consumes_no_additional_network_budget(
    selected_source_set: SelectedSourceSet,
) -> None:
    selected = _two_sources_sharing_one_url(selected_source_set)
    bundle, request = _request(selected)
    store = _FailingStore(fail_on_put=4)
    result = asyncio.run(
        ArtifactDownloadService(
            store=store,
            transport=bundle.transport,
            clock=lambda: NOW,
        ).execute(request)
    )

    storage_failure = next(
        item
        for item in result.run_log.attempts
        if item.error_code is DownloadErrorCode.STORAGE_ERROR
    )
    successful_download = next(
        item for item in result.run_log.attempts if item.status is DownloadAttemptStatus.STORED
    )
    assert storage_failure.bytes_received == 0
    assert result.metrics.received_bytes == successful_download.bytes_received


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

    tampered_attempts = tuple(
        item.model_copy(update={"execution_mode": DownloadExecutionMode.MOCK_TRANSPORT})
        for item in result.run_log.attempts
    )
    tampered_run_log = result.run_log.model_copy(update={"attempts": tampered_attempts})
    with pytest.raises(AppError, match="execution mode"):
        verify_artifact_download_integrity(
            result.model_copy(update={"run_log": tampered_run_log}),
            request,
            store,
        )

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

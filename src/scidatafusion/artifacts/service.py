"""M07 orchestration from selected locators to immutable Bronze manifests."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from threading import RLock
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from scidatafusion.artifacts.archive import ExtractedArchiveMember, SafeArchiveInspector
from scidatafusion.artifacts.checkpoints import (
    ArtifactCheckpointStore,
    MemoryArtifactCheckpointStore,
)
from scidatafusion.artifacts.downloader import (
    DownloadFailure,
    HostResolver,
    LiveHostRateLimiter,
    SafeDownloadClient,
    sanitize_url_for_manifest,
)
from scidatafusion.artifacts.integrity import (
    calculate_acquisition_hash,
    calculate_artifact_download_idempotency_key,
    calculate_artifact_download_input_hash,
    calculate_artifact_download_output_hash,
    calculate_artifact_manifest_hash,
    calculate_bronze_artifact_set_hash,
    calculate_bronze_object_metadata_hash,
    calculate_candidate_locator_hash,
    calculate_download_policy_hash,
    calculate_download_run_log_hash,
    calculate_url_locator_hash,
    verify_artifact_download_integrity,
    verify_artifact_download_request_integrity,
)
from scidatafusion.artifacts.sniffer import ContentSniffer
from scidatafusion.artifacts.storage import BronzeByteStore, MemoryBronzeStore
from scidatafusion.contracts.artifacts import (
    AcquisitionStatus,
    ArtifactAcquisition,
    ArtifactDownloadCompletedPayload,
    ArtifactDownloadMetrics,
    ArtifactDownloadRequest,
    ArtifactDownloadResult,
    ArtifactDownloadStatus,
    ArtifactKind,
    ArtifactManifest,
    ArtifactRelationship,
    ArtifactStoredPayload,
    BronzeArtifactSet,
    BronzeObject,
    ContentInspection,
    DownloadAttempt,
    DownloadAttemptStatus,
    DownloadErrorCode,
    DownloadExecutionMode,
    DownloadLocatorRecord,
    DownloadPolicy,
    DownloadResponseMetadata,
    DownloadRunLog,
    SourceDownloadApproval,
)
from scidatafusion.contracts.base import utc_now
from scidatafusion.contracts.connectors import CandidateIdentifier, IdentifierKind
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.selection import LicenseDecision, SelectedSource
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode

_ZERO_HASH = "0" * 64
_SECURITY_FAILURES = frozenset(
    {
        DownloadErrorCode.HOST_NOT_ALLOWED,
        DownloadErrorCode.DNS_NOT_PUBLIC,
        DownloadErrorCode.REDIRECT_BLOCKED,
        DownloadErrorCode.REDIRECT_LIMIT,
        DownloadErrorCode.CONTENT_ENCODING_UNSUPPORTED,
        DownloadErrorCode.ARCHIVE_REJECTED,
    }
)
_ATTACHMENT_SUFFIXES = frozenset(
    {
        ".csv",
        ".fits",
        ".fit",
        ".h5",
        ".hdf5",
        ".html",
        ".jpeg",
        ".jpg",
        ".json",
        ".parquet",
        ".pdf",
        ".png",
        ".tif",
        ".tiff",
        ".tsv",
        ".txt",
        ".vot",
        ".votable",
        ".xls",
        ".xlsx",
        ".xml",
        ".zip",
    }
)
Sleep = Callable[[float], Awaitable[None]]


class DownloadRequestAuthorizer(Protocol):
    """Trusted deployment boundary for live allowlists and human approvals."""

    def authorize(self, request: ArtifactDownloadRequest) -> None:
        """Raise a structured error unless this exact live request is trusted."""


@dataclass(slots=True)
class _ExecutionState:
    idempotency_key: str
    objects_by_hash: dict[str, BronzeObject] = field(default_factory=dict)
    acquisitions: list[ArtifactAcquisition] = field(default_factory=list)
    attempts: list[DownloadAttempt] = field(default_factory=list)
    visited_locators: set[tuple[str, str]] = field(default_factory=set)
    downloaded_bytes: int = 0


class ArtifactDownloadService:
    """Acquire approved source bytes sequentially under one deterministic budget."""

    def __init__(
        self,
        *,
        store: BronzeByteStore | None = None,
        checkpoints: ArtifactCheckpointStore | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: HostResolver | None = None,
        authorizer: DownloadRequestAuthorizer | None = None,
        rate_limiter: LiveHostRateLimiter | None = None,
        clock: Callable[[], datetime] = utc_now,
        sleep: Sleep = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        producer_version: str = "1.0.0",
    ) -> None:
        self._store = store or MemoryBronzeStore()
        self._checkpoints = checkpoints or MemoryArtifactCheckpointStore()
        self._transport = transport
        self._resolver = resolver
        self._authorizer = authorizer
        self._clock = clock
        self._sleep = sleep
        self._monotonic = monotonic
        self._rate_limiter = rate_limiter or LiveHostRateLimiter(
            sleep=sleep,
            monotonic=monotonic,
        )
        self._producer_version = producer_version
        self._cache: dict[str, ArtifactDownloadResult] = {}
        self._inflight: dict[str, Future[ArtifactDownloadResult]] = {}
        self._lock = RLock()

    async def aclose(self) -> None:
        """Close the caller-owned shared transport after all executions finish."""

        if self._transport is not None:
            await self._transport.aclose()

    async def execute(self, request: ArtifactDownloadRequest) -> ArtifactDownloadResult:
        """Return one idempotent result and coalesce concurrent identical requests."""

        verify_artifact_download_request_integrity(request)
        if request.runtime.execution_mode is DownloadExecutionMode.LIVE_NETWORK:
            if self._authorizer is None:
                raise AppError(
                    ErrorCode.SECURITY_POLICY_VIOLATION,
                    "Live M07 requests require a trusted deployment authorizer",
                )
            try:
                self._authorizer.authorize(request)
            except AppError:
                raise
            except Exception as exc:
                raise AppError(
                    ErrorCode.SECURITY_POLICY_VIOLATION,
                    "Live M07 request authorization failed closed",
                ) from exc
            if isinstance(self._store, MemoryBronzeStore) or isinstance(
                self._checkpoints,
                MemoryArtifactCheckpointStore,
            ):
                raise AppError(
                    ErrorCode.CONFIGURATION_ERROR,
                    "Live M07 execution requires durable Bronze and checkpoint stores",
                )
        input_hash = calculate_artifact_download_input_hash(request)
        idempotency_key = calculate_artifact_download_idempotency_key(
            request,
            self._producer_version,
        )
        with self._lock:
            cached = self._cache.get(idempotency_key)
            if cached is not None:
                return cached
        checkpoint = self._checkpoints.load(idempotency_key)
        if checkpoint is not None:
            if checkpoint.producer_version != self._producer_version:
                raise AppError(
                    ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "M07 checkpoint producer does not match this service",
                )
            verify_artifact_download_integrity(checkpoint, request, self._store)
            with self._lock:
                return self._cache.setdefault(idempotency_key, checkpoint)
        with self._lock:
            pending = self._inflight.get(idempotency_key)
            is_owner = pending is None
            if pending is None:
                pending = Future()
                self._inflight[idempotency_key] = pending
        if not is_owner:
            return await asyncio.shield(asyncio.wrap_future(pending))
        try:
            result = await self._execute_once(
                request,
                input_hash=input_hash,
                idempotency_key=idempotency_key,
            )
            result = self._checkpoints.save(result)
            verify_artifact_download_integrity(result, request, self._store)
        except BaseException as exc:
            with self._lock:
                self._inflight.pop(idempotency_key, None)
            pending.set_exception(exc)
            raise
        with self._lock:
            existing = self._cache.setdefault(idempotency_key, result)
            self._inflight.pop(idempotency_key, None)
        pending.set_result(existing)
        return existing

    async def _execute_once(
        self,
        request: ArtifactDownloadRequest,
        *,
        input_hash: str,
        idempotency_key: str,
    ) -> ArtifactDownloadResult:
        created_at = self._clock()
        state = _ExecutionState(idempotency_key=idempotency_key)
        approvals = {item.candidate_id: item for item in request.approvals}
        async with SafeDownloadClient(
            request.runtime,
            request.policy,
            transport=self._transport,
            resolver=self._resolver,
            rate_limiter=self._rate_limiter,
            sleep=self._sleep,
            monotonic=self._monotonic,
            wall_clock=self._clock,
        ) as client:
            for source in request.selected_source_set.sources:
                url_locators = tuple(
                    item for item in source.download_locators if item.kind is IdentifierKind.URL
                )[: request.policy.max_root_locators_per_source]
                if not url_locators:
                    locator = source.download_locators[0]
                    self._record_skipped(
                        request,
                        state,
                        source,
                        _locator_record(locator),
                        DownloadErrorCode.LOCATOR_UNSUPPORTED,
                    )
                    continue
                for locator in url_locators:
                    await self._acquire_url(
                        request,
                        client,
                        state,
                        source,
                        locator.value,
                        relationship=ArtifactRelationship.ROOT_DOWNLOAD,
                        parent_object_id=None,
                        approval=approvals.get(source.candidate_id),
                        allow_attachment_discovery=True,
                    )
        result = self._build_result(
            request,
            state=state,
            input_hash=input_hash,
            idempotency_key=idempotency_key,
            created_at=created_at,
        )
        verify_artifact_download_integrity(result, request, self._store)
        return result

    async def _acquire_url(
        self,
        request: ArtifactDownloadRequest,
        client: SafeDownloadClient,
        state: _ExecutionState,
        source: SelectedSource,
        url: str,
        *,
        relationship: ArtifactRelationship,
        parent_object_id: str | None,
        approval: SourceDownloadApproval | None,
        allow_attachment_discovery: bool,
    ) -> None:
        locator_hash = calculate_url_locator_hash(url)
        locator = DownloadLocatorRecord(
            kind=IdentifierKind.URL,
            locator_hash=locator_hash,
            safe_url=sanitize_url_for_manifest(url),
        )
        visit_key = (source.candidate_id, locator_hash)
        if visit_key in state.visited_locators:
            return
        state.visited_locators.add(visit_key)
        approval_ref = _approval_ref(source, locator_hash, approval)
        if source.license_decision is not LicenseDecision.ALLOWED and approval_ref is None:
            self._record_skipped(
                request,
                state,
                source,
                locator,
                DownloadErrorCode.LICENSE_APPROVAL_REQUIRED,
            )
            return
        for attempt_number in range(1, request.policy.max_attempts + 1):
            started_at = self._clock()
            remaining = request.policy.max_total_bytes - state.downloaded_bytes
            limit = min(request.policy.max_file_bytes, remaining)
            try:
                fetched = await client.fetch(
                    url,
                    byte_limit=limit,
                    approved_locator_hashes=(
                        None
                        if source.license_decision is LicenseDecision.ALLOWED
                        else frozenset(approval.locator_hashes)
                        if approval is not None
                        else frozenset()
                    ),
                )
                received_bytes = 0 if fetched.cache_hit else len(fetched.content)
                final_approval_ref = _approval_ref(
                    source,
                    fetched.response.final_locator_hash,
                    approval,
                )
                if (
                    source.license_decision is not LicenseDecision.ALLOWED
                    and final_approval_ref is None
                ):
                    raise DownloadFailure(
                        DownloadErrorCode.LICENSE_APPROVAL_REQUIRED,
                        "Redirect target requires exact locator-bound approval",
                        network_performed=fetched.network_performed,
                        bytes_received=received_bytes,
                        http_status=fetched.response.status_code,
                    )
                inspection = ContentSniffer.inspect(
                    fetched.content,
                    declared_media_type=fetched.response.declared_content_type,
                )
                archive_members = None
                if inspection.detected_media_type == "application/zip":
                    try:
                        archive_members = SafeArchiveInspector.extract_zip(
                            fetched.content,
                            request.policy,
                        )
                    except AppError as exc:
                        raise DownloadFailure(
                            DownloadErrorCode.ARCHIVE_REJECTED,
                            exc.message,
                            network_performed=fetched.network_performed,
                            bytes_received=received_bytes,
                            http_status=fetched.response.status_code,
                        ) from exc
                prior_object_hashes = set(state.objects_by_hash)
                prior_acquisition_count = len(state.acquisitions)
                try:
                    obj, acquisition = self._persist(
                        state,
                        source,
                        locator,
                        fetched.content,
                        inspection,
                        relationship=relationship,
                        parent_object_id=parent_object_id,
                        archive_member_path=None,
                        response=fetched.response,
                        approval_ref=approval_ref,
                    )
                    if archive_members is not None:
                        self._persist_archive_members(
                            state,
                            source,
                            locator,
                            obj,
                            archive_members,
                            approval_ref=approval_ref,
                        )
                except AppError as exc:
                    del state.acquisitions[prior_acquisition_count:]
                    for byte_hash in tuple(state.objects_by_hash):
                        if byte_hash not in prior_object_hashes:
                            del state.objects_by_hash[byte_hash]
                    raise DownloadFailure(
                        DownloadErrorCode.STORAGE_ERROR,
                        "Failed to persist the immutable Bronze acquisition",
                        network_performed=fetched.network_performed,
                        bytes_received=received_bytes,
                        http_status=fetched.response.status_code,
                    ) from exc
                state.downloaded_bytes += received_bytes
                state.attempts.append(
                    DownloadAttempt(
                        attempt_id=_stable_id(
                            "dat",
                            (
                                state.idempotency_key,
                                source.candidate_id,
                                locator_hash,
                                attempt_number,
                            ),
                            length=16,
                        ),
                        candidate_id=source.candidate_id,
                        locator=locator,
                        attempt_number=attempt_number,
                        execution_mode=request.runtime.execution_mode,
                        network_performed=fetched.network_performed,
                        status=(
                            DownloadAttemptStatus.STORED
                            if acquisition.status is AcquisitionStatus.STORED
                            else DownloadAttemptStatus.DEDUPLICATED
                        ),
                        retryable=False,
                        started_at=started_at,
                        finished_at=self._clock(),
                        bytes_received=received_bytes,
                        cache_hit=fetched.cache_hit,
                        http_status=fetched.response.status_code,
                        byte_sha256=obj.byte_sha256,
                        object_id=obj.object_id,
                        acquisition_id=acquisition.acquisition_id,
                    )
                )
                if (
                    allow_attachment_discovery
                    and inspection.artifact_kind is ArtifactKind.LANDING_PAGE
                    and request.policy.max_attachments_per_landing
                ):
                    links = _attachment_links(
                        fetched.content,
                        base_url=fetched.final_request_url,
                        allowed_hosts=request.runtime.allowed_hosts,
                        maximum=request.policy.max_attachments_per_landing,
                        byte_limit=request.policy.max_html_inspection_bytes,
                    )
                    for link in links:
                        await self._acquire_url(
                            request,
                            client,
                            state,
                            source,
                            link,
                            relationship=ArtifactRelationship.LANDING_ATTACHMENT,
                            parent_object_id=obj.object_id,
                            approval=approval,
                            allow_attachment_discovery=False,
                        )
                return
            except DownloadFailure as failure:
                state.downloaded_bytes += failure.bytes_received
                status = (
                    DownloadAttemptStatus.QUARANTINED
                    if failure.code in _SECURITY_FAILURES
                    else DownloadAttemptStatus.FAILED
                )
                will_retry = (
                    failure.retryable
                    and status is DownloadAttemptStatus.FAILED
                    and attempt_number < request.policy.max_attempts
                )
                state.attempts.append(
                    DownloadAttempt(
                        attempt_id=_stable_id(
                            "dat",
                            (
                                state.idempotency_key,
                                source.candidate_id,
                                locator_hash,
                                attempt_number,
                            ),
                            length=16,
                        ),
                        candidate_id=source.candidate_id,
                        locator=locator,
                        attempt_number=attempt_number,
                        execution_mode=request.runtime.execution_mode,
                        network_performed=failure.network_performed,
                        status=status,
                        error_code=failure.code,
                        retryable=will_retry,
                        started_at=started_at,
                        finished_at=self._clock(),
                        bytes_received=failure.bytes_received,
                        http_status=failure.http_status,
                    )
                )
                if not will_retry:
                    return
                delay = _retry_delay(
                    request.policy,
                    attempt_number=attempt_number,
                    retry_after_seconds=failure.retry_after_seconds,
                )
                if delay > 0.0:
                    await self._sleep(delay)

    def _persist(
        self,
        state: _ExecutionState,
        source: SelectedSource,
        locator: DownloadLocatorRecord,
        content: bytes,
        inspection: ContentInspection,
        *,
        relationship: ArtifactRelationship,
        parent_object_id: str | None,
        archive_member_path: str | None,
        response: DownloadResponseMetadata | None,
        approval_ref: str | None,
    ) -> tuple[BronzeObject, ArtifactAcquisition]:
        recorded_at = self._clock()
        receipt = self._store.put(content)
        obj = state.objects_by_hash.get(receipt.byte_sha256)
        if obj is None:
            draft = BronzeObject(
                object_id=f"brz_{receipt.byte_sha256[:32]}",
                byte_sha256=receipt.byte_sha256,
                size_bytes=receipt.size_bytes,
                storage_uri=receipt.storage_uri,
                media=inspection,
                recorded_at=recorded_at,
                object_metadata_hash=_ZERO_HASH,
            )
            obj = draft.model_copy(
                update={"object_metadata_hash": calculate_bronze_object_metadata_hash(draft)}
            )
            state.objects_by_hash[receipt.byte_sha256] = obj
        status = (
            AcquisitionStatus.STORED if receipt.newly_stored else AcquisitionStatus.DEDUPLICATED
        )
        draft_acquisition = ArtifactAcquisition(
            acquisition_id=f"acq_{'0' * 16}",
            candidate_id=source.candidate_id,
            candidate_hash=source.candidate_hash,
            selection_rank=source.selection_rank,
            object_id=obj.object_id,
            byte_sha256=obj.byte_sha256,
            locator=locator,
            response=response,
            status=status,
            relationship=relationship,
            parent_object_id=parent_object_id,
            archive_member_path=archive_member_path,
            acquired_at=recorded_at,
            license_decision=source.license_decision,
            approval_ref=approval_ref,
            acquisition_hash=_ZERO_HASH,
        )
        acquisition_hash = calculate_acquisition_hash(draft_acquisition)
        acquisition = draft_acquisition.model_copy(
            update={
                "acquisition_id": f"acq_{acquisition_hash[:16]}",
                "acquisition_hash": acquisition_hash,
            }
        )
        state.acquisitions.append(acquisition)
        return obj, acquisition

    def _persist_archive_members(
        self,
        state: _ExecutionState,
        source: SelectedSource,
        locator: DownloadLocatorRecord,
        parent: BronzeObject,
        members: Sequence[ExtractedArchiveMember],
        *,
        approval_ref: str | None,
    ) -> None:
        for member in members:
            if not member.content:
                continue
            inspection = ContentSniffer.inspect(member.content)
            self._persist(
                state,
                source,
                locator,
                member.content,
                inspection,
                relationship=ArtifactRelationship.ARCHIVE_MEMBER,
                parent_object_id=parent.object_id,
                archive_member_path=member.path,
                response=None,
                approval_ref=approval_ref,
            )

    def _record_skipped(
        self,
        request: ArtifactDownloadRequest,
        state: _ExecutionState,
        source: SelectedSource,
        locator: DownloadLocatorRecord,
        code: DownloadErrorCode,
    ) -> None:
        timestamp = self._clock()
        state.attempts.append(
            DownloadAttempt(
                attempt_id=_stable_id(
                    "dat",
                    (state.idempotency_key, source.candidate_id, locator.locator_hash, 1),
                    length=16,
                ),
                candidate_id=source.candidate_id,
                locator=locator,
                attempt_number=1,
                execution_mode=request.runtime.execution_mode,
                network_performed=False,
                status=DownloadAttemptStatus.SKIPPED,
                error_code=code,
                retryable=False,
                started_at=timestamp,
                finished_at=timestamp,
                bytes_received=0,
            )
        )

    def _build_result(
        self,
        request: ArtifactDownloadRequest,
        *,
        state: _ExecutionState,
        input_hash: str,
        idempotency_key: str,
        created_at: datetime,
    ) -> ArtifactDownloadResult:
        selected = request.selected_source_set
        objects = tuple(state.objects_by_hash.values())
        artifact_set = BronzeArtifactSet(
            task_id=selected.task_id,
            run_id=selected.run_id,
            contract_version=selected.contract_version,
            created_at=created_at,
            producer_version=self._producer_version,
            artifact_set_id=f"bas_{'0' * 32}",
            selection_id=selected.selection_id,
            selected_source_set_hash=selected.selected_source_set_hash,
            objects=objects,
            artifact_set_hash=_ZERO_HASH,
        )
        artifact_set_hash = calculate_bronze_artifact_set_hash(artifact_set)
        artifact_set = artifact_set.model_copy(
            update={
                "artifact_set_id": f"bas_{artifact_set_hash[:32]}",
                "artifact_set_hash": artifact_set_hash,
            }
        )
        policy_hash = calculate_download_policy_hash(request.policy)
        manifest = ArtifactManifest(
            task_id=selected.task_id,
            run_id=selected.run_id,
            contract_version=selected.contract_version,
            created_at=created_at,
            producer_version=self._producer_version,
            manifest_id=f"amf_{'0' * 32}",
            selection_id=selected.selection_id,
            selected_source_set_hash=selected.selected_source_set_hash,
            artifact_set_hash=artifact_set.artifact_set_hash,
            policy_hash=policy_hash,
            runtime_hash=request.runtime.runtime_hash,
            selected_candidate_ids=tuple(item.candidate_id for item in selected.sources),
            acquisitions=tuple(state.acquisitions),
            manifest_hash=_ZERO_HASH,
        )
        manifest_hash = calculate_artifact_manifest_hash(manifest)
        manifest = manifest.model_copy(
            update={
                "manifest_id": f"amf_{manifest_hash[:32]}",
                "manifest_hash": manifest_hash,
            }
        )
        run_log = DownloadRunLog(
            task_id=selected.task_id,
            run_id=selected.run_id,
            contract_version=selected.contract_version,
            created_at=created_at,
            producer_version=self._producer_version,
            download_run_id=f"dwr_{'0' * 32}",
            selection_id=selected.selection_id,
            selected_source_set_hash=selected.selected_source_set_hash,
            policy_hash=policy_hash,
            runtime_hash=request.runtime.runtime_hash,
            attempts=tuple(state.attempts),
            run_log_hash=_ZERO_HASH,
        )
        run_log_hash = calculate_download_run_log_hash(run_log)
        run_log = run_log.model_copy(
            update={
                "download_run_id": f"dwr_{run_log_hash[:32]}",
                "run_log_hash": run_log_hash,
            }
        )
        metrics = _metrics(manifest, artifact_set, run_log)
        status = _status(manifest, artifact_set, run_log)
        warnings = tuple(
            f"{item.error_code.value}:{item.attempt_id}"
            for item in run_log.attempts
            if item.error_code is not None
        ) + tuple(
            f"{(DownloadErrorCode.CONTENT_TYPE_MISMATCH if item.media.media_type_mismatch else DownloadErrorCode.UNSUPPORTED_MEDIA_TYPE).value}:{item.object_id}"
            for item in artifact_set.objects
            if item.media.requires_review
        )
        stored_events = tuple(
            EventEnvelope[ArtifactStoredPayload | ArtifactDownloadCompletedPayload](
                event_id=_stable_id("evt", (idempotency_key, obj.object_id), length=32),
                event_type=EventType.ARTIFACT_STORED,
                task_id=selected.task_id,
                run_id=selected.run_id,
                occurred_at=created_at,
                schema_version=selected.contract_version,
                producer=ProducerRef(
                    component="artifact_download_service",
                    version=self._producer_version,
                ),
                correlation_id=input_hash,
                payload=ArtifactStoredPayload(
                    object_id=obj.object_id,
                    byte_sha256=obj.byte_sha256,
                    artifact_set_hash=artifact_set.artifact_set_hash,
                    manifest_hash=manifest.manifest_hash,
                    acquisition_count=sum(
                        item.object_id == obj.object_id for item in manifest.acquisitions
                    ),
                    input_hash=input_hash,
                    output_hash=_ZERO_HASH,
                    idempotency_key=idempotency_key,
                ),
            )
            for obj in artifact_set.objects
        )
        completed_event = EventEnvelope[ArtifactStoredPayload | ArtifactDownloadCompletedPayload](
            event_id=_stable_id("evt", (idempotency_key, "completed"), length=32),
            event_type=EventType.ARTIFACT_DOWNLOAD_COMPLETED,
            task_id=selected.task_id,
            run_id=selected.run_id,
            occurred_at=created_at,
            schema_version=selected.contract_version,
            producer=ProducerRef(
                component="artifact_download_service",
                version=self._producer_version,
            ),
            correlation_id=input_hash,
            payload=ArtifactDownloadCompletedPayload(
                status=status,
                artifact_set_hash=artifact_set.artifact_set_hash,
                manifest_hash=manifest.manifest_hash,
                run_log_hash=run_log.run_log_hash,
                bronze_object_count=len(artifact_set.objects),
                input_hash=input_hash,
                output_hash=_ZERO_HASH,
                idempotency_key=idempotency_key,
            ),
        )
        events = (*stored_events, completed_event)
        draft = ArtifactDownloadResult(
            task_id=selected.task_id,
            run_id=selected.run_id,
            contract_version=selected.contract_version,
            created_at=created_at,
            producer_version=self._producer_version,
            status=status,
            input_hash=input_hash,
            output_hash=_ZERO_HASH,
            idempotency_key=idempotency_key,
            artifact_set=artifact_set,
            manifest=manifest,
            run_log=run_log,
            warnings=warnings,
            metrics=metrics,
            events=events,
        )
        output_hash = calculate_artifact_download_output_hash(draft)
        final_events = tuple(
            event.model_copy(
                update={"payload": event.payload.model_copy(update={"output_hash": output_hash})}
            )
            for event in events
        )
        return ArtifactDownloadResult.model_validate(
            draft.model_copy(
                update={"output_hash": output_hash, "events": final_events}
            ).model_dump(mode="python")
        )


def _metrics(
    manifest: ArtifactManifest,
    artifact_set: BronzeArtifactSet,
    run_log: DownloadRunLog,
) -> ArtifactDownloadMetrics:
    return ArtifactDownloadMetrics(
        selected_source_count=len(manifest.selected_candidate_ids),
        attempted_download_count=len(run_log.attempts),
        stored_download_count=sum(
            item.status is DownloadAttemptStatus.STORED for item in run_log.attempts
        ),
        deduplicated_download_count=sum(
            item.status is DownloadAttemptStatus.DEDUPLICATED for item in run_log.attempts
        ),
        skipped_download_count=sum(
            item.status is DownloadAttemptStatus.SKIPPED for item in run_log.attempts
        ),
        failed_download_count=sum(
            item.status is DownloadAttemptStatus.FAILED for item in run_log.attempts
        ),
        quarantined_download_count=sum(
            item.status is DownloadAttemptStatus.QUARANTINED for item in run_log.attempts
        ),
        cache_hit_count=sum(item.cache_hit for item in run_log.attempts),
        review_required_object_count=sum(
            item.media.requires_review for item in artifact_set.objects
        ),
        acquisition_count=len(manifest.acquisitions),
        archive_member_count=sum(
            item.relationship is ArtifactRelationship.ARCHIVE_MEMBER
            for item in manifest.acquisitions
        ),
        bronze_object_count=len(artifact_set.objects),
        received_bytes=sum(item.bytes_received for item in run_log.attempts),
        persisted_unique_bytes=sum(item.size_bytes for item in artifact_set.objects),
    )


def _status(
    manifest: ArtifactManifest,
    artifact_set: BronzeArtifactSet,
    run_log: DownloadRunLog,
) -> ArtifactDownloadStatus:
    terminal_attempts = _terminal_attempts(run_log)
    if not manifest.selected_candidate_ids:
        return ArtifactDownloadStatus.UNSUPPORTED
    if not manifest.acquisitions:
        if any(
            item.error_code is DownloadErrorCode.LICENSE_APPROVAL_REQUIRED
            or item.status is DownloadAttemptStatus.QUARANTINED
            for item in run_log.attempts
        ):
            return ArtifactDownloadStatus.NEEDS_REVIEW
        if any(item.status is DownloadAttemptStatus.FAILED for item in run_log.attempts):
            return ArtifactDownloadStatus.FAILED
        return ArtifactDownloadStatus.UNSUPPORTED
    if (
        any(
            item.status not in {DownloadAttemptStatus.STORED, DownloadAttemptStatus.DEDUPLICATED}
            for item in terminal_attempts
        )
        or {item.candidate_id for item in manifest.acquisitions}
        != set(manifest.selected_candidate_ids)
        or any(item.media.requires_review for item in artifact_set.objects)
    ):
        return ArtifactDownloadStatus.PARTIAL
    return ArtifactDownloadStatus.SUCCEEDED


def _terminal_attempts(run_log: DownloadRunLog) -> tuple[DownloadAttempt, ...]:
    terminal_by_locator: dict[tuple[str, str], DownloadAttempt] = {}
    for attempt in run_log.attempts:
        key = (attempt.candidate_id, attempt.locator.locator_hash)
        previous = terminal_by_locator.get(key)
        if previous is None or attempt.attempt_number > previous.attempt_number:
            terminal_by_locator[key] = attempt
    return tuple(terminal_by_locator.values())


def _retry_delay(
    policy: DownloadPolicy,
    *,
    attempt_number: int,
    retry_after_seconds: float | None,
) -> float:
    exponential = min(
        policy.max_backoff_seconds,
        policy.base_backoff_seconds * (2 ** (attempt_number - 1)),
    )
    if retry_after_seconds is None:
        return float(exponential)
    bounded_retry_after = min(
        policy.max_retry_after_seconds,
        retry_after_seconds,
    )
    return float(max(exponential, bounded_retry_after))


def _locator_record(locator: CandidateIdentifier) -> DownloadLocatorRecord:
    return DownloadLocatorRecord(
        kind=locator.kind,
        locator_hash=calculate_candidate_locator_hash(locator),
        safe_url=(
            sanitize_url_for_manifest(locator.value) if locator.kind is IdentifierKind.URL else None
        ),
    )


def _approval_ref(
    source: SelectedSource,
    locator_hash: str,
    approval: SourceDownloadApproval | None,
) -> str | None:
    if source.license_decision is LicenseDecision.ALLOWED:
        return None
    if approval is not None and locator_hash in approval.locator_hashes:
        return str(approval.approval_ref)
    return None


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, bool]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() != "a":
            return
        attributes = {key.casefold(): value for key, value in attrs}
        href = attributes.get("href")
        if href:
            self.links.append((href, "download" in attributes))


def _attachment_links(
    content: bytes,
    *,
    base_url: str,
    allowed_hosts: tuple[str, ...],
    maximum: int,
    byte_limit: int,
) -> tuple[str, ...]:
    text = content[:byte_limit].decode("utf-8-sig", errors="replace")
    parser = _LinkParser()
    parser.feed(text)
    links: list[str] = []
    seen: set[str] = set()
    for href, explicit_download in parser.links:
        resolved = urljoin(base_url, href)
        parsed = urlsplit(resolved)
        suffix = parsed.path.casefold()
        recognized = any(suffix.endswith(item) for item in _ATTACHMENT_SUFFIXES)
        host = (parsed.hostname or "").casefold().rstrip(".")
        if (
            not (explicit_download or recognized)
            or parsed.scheme.casefold() != "https"
            or host not in allowed_hosts
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or resolved in seen
        ):
            continue
        seen.add(resolved)
        links.append(resolved)
        if len(links) >= maximum:
            break
    return tuple(links)


def _stable_id(prefix: str, payload: object, *, length: int) -> str:
    return f"{prefix}_{canonical_hash(payload)[:length]}"

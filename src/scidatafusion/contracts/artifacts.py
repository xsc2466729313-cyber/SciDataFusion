"""Strict contracts for M07 download acquisition and immutable Bronze artifacts."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from enum import StrEnum
from ipaddress import ip_address
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, StringConstraints, field_validator, model_validator

from scidatafusion.contracts.base import (
    ContentHash,
    NonEmptyStr,
    RunId,
    SemanticVersion,
    StrictContract,
    TaskId,
)
from scidatafusion.contracts.connectors import CandidateId, IdentifierKind
from scidatafusion.contracts.events import EventEnvelope
from scidatafusion.contracts.selection import LicenseDecision, SelectedSourceSet

ArtifactManifestId = Annotated[str, StringConstraints(pattern=r"^amf_[0-9a-f]{32}$")]
BronzeArtifactSetId = Annotated[str, StringConstraints(pattern=r"^bas_[0-9a-f]{32}$")]
BronzeObjectId = Annotated[str, StringConstraints(pattern=r"^brz_[0-9a-f]{32}$")]
AcquisitionId = Annotated[str, StringConstraints(pattern=r"^acq_[0-9a-f]{16}$")]
DownloadRunId = Annotated[str, StringConstraints(pattern=r"^dwr_[0-9a-f]{32}$")]
DownloadAttemptId = Annotated[str, StringConstraints(pattern=r"^dat_[0-9a-f]{16}$")]
SafeHttpsUrl = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=9, max_length=4096),
]
ArchiveMemberPath = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1024),
]
BronzeStorageUri = Annotated[
    str,
    StringConstraints(pattern=r"^bronze://sha256/[0-9a-f]{64}$"),
]

_PUBLIC_HOST_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


class ArtifactDownloadStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class DownloadExecutionMode(StrEnum):
    LIVE_NETWORK = "live_network"
    MOCK_TRANSPORT = "mock_transport"
    OFFLINE_FIXTURE = "offline_fixture"


class DownloadApprovalKind(StrEnum):
    OPEN_LICENSE_METADATA = "open_license_metadata"
    HUMAN_REVIEW = "human_review"
    OFFLINE_FIXTURE = "offline_fixture"


class DownloadAttemptStatus(StrEnum):
    STORED = "stored"
    DEDUPLICATED = "deduplicated"
    SKIPPED = "skipped"
    FAILED = "failed"
    QUARANTINED = "quarantined"


class DownloadErrorCode(StrEnum):
    LICENSE_APPROVAL_REQUIRED = "license_approval_required"
    LOCATOR_UNSUPPORTED = "locator_unsupported"
    HOST_NOT_ALLOWED = "host_not_allowed"
    DNS_NOT_PUBLIC = "dns_not_public"
    REDIRECT_BLOCKED = "redirect_blocked"
    REDIRECT_LIMIT = "redirect_limit"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    RESPONSE_TOO_LARGE = "response_too_large"
    INCOMPLETE_RESPONSE = "incomplete_response"
    EMPTY_RESPONSE = "empty_response"
    CONTENT_ENCODING_UNSUPPORTED = "content_encoding_unsupported"
    CONTENT_TYPE_MISMATCH = "content_type_mismatch"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    ARCHIVE_REJECTED = "archive_rejected"
    STORAGE_ERROR = "storage_error"
    FIXTURE_MISSING = "fixture_missing"


class ContentDetectionBasis(StrEnum):
    MAGIC_BYTES = "magic_bytes"
    STRUCTURAL_PROBE = "structural_probe"
    TEXT_PROBE = "text_probe"
    UNKNOWN = "unknown"


class ArtifactKind(StrEnum):
    LANDING_PAGE = "landing_page"
    DOCUMENT = "document"
    TABLE = "table"
    IMAGE = "image"
    SCIENTIFIC_FILE = "scientific_file"
    ARCHIVE = "archive"
    ARCHIVE_MEMBER = "archive_member"
    UNKNOWN = "unknown"


class ArtifactRelationship(StrEnum):
    ROOT_DOWNLOAD = "root_download"
    LANDING_ATTACHMENT = "landing_attachment"
    ARCHIVE_MEMBER = "archive_member"


class AcquisitionStatus(StrEnum):
    STORED = "stored"
    DEDUPLICATED = "deduplicated"


class DownloadArtifact(StrictContract):
    task_id: TaskId
    run_id: RunId
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M07 artifact timestamp must include a timezone")
        return value.astimezone(UTC)


class DownloadPolicy(StrictContract):
    policy_version: SemanticVersion = "1.1.0"
    max_total_bytes: int = Field(default=100_000_000, ge=1, le=1_000_000_000)
    max_file_bytes: int = Field(default=25_000_000, ge=1, le=64_000_000)
    chunk_size_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    max_redirects: int = Field(default=3, ge=0, le=10)
    connect_timeout_seconds: float = Field(default=5.0, gt=0.0, le=60.0)
    read_timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)
    max_attempts: int = Field(default=2, ge=1, le=5)
    requests_per_second_per_host: float = Field(
        default=2.0,
        gt=0.0,
        le=100.0,
        allow_inf_nan=False,
    )
    base_backoff_seconds: float = Field(default=0.25, ge=0.0, le=60.0, allow_inf_nan=False)
    max_backoff_seconds: float = Field(default=4.0, ge=0.0, le=300.0, allow_inf_nan=False)
    max_retry_after_seconds: float = Field(
        default=30.0,
        ge=0.0,
        le=300.0,
        allow_inf_nan=False,
    )
    cache_enabled: bool = True
    max_root_locators_per_source: int = Field(default=2, ge=1, le=20)
    max_attachments_per_landing: int = Field(default=10, ge=0, le=100)
    max_html_inspection_bytes: int = Field(default=4_194_304, ge=1024, le=8_388_608)
    max_archive_entries: int = Field(default=1000, ge=1, le=10_000)
    max_archive_uncompressed_bytes: int = Field(
        default=64_000_000,
        ge=1,
        le=64_000_000,
    )
    max_archive_member_bytes: int = Field(default=25_000_000, ge=1, le=64_000_000)
    max_archive_compression_ratio: float = Field(default=100.0, ge=1.0, le=1_000.0)
    max_archive_depth: int = Field(default=1, ge=0, le=5)

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        numeric = (
            self.connect_timeout_seconds,
            self.read_timeout_seconds,
            self.requests_per_second_per_host,
            self.base_backoff_seconds,
            self.max_backoff_seconds,
            self.max_retry_after_seconds,
            self.max_archive_compression_ratio,
        )
        if any(not math.isfinite(value) for value in numeric):
            raise ValueError("download policy numeric limits must be finite")
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("per-file bytes cannot exceed the total download budget")
        if self.max_archive_member_bytes > self.max_archive_uncompressed_bytes:
            raise ValueError("archive member bytes cannot exceed the archive expansion budget")
        if self.base_backoff_seconds > self.max_backoff_seconds:
            raise ValueError("base retry backoff cannot exceed maximum backoff")
        return self


class DownloadRuntimeSnapshot(StrictContract):
    execution_mode: DownloadExecutionMode
    network_enabled: bool
    allowed_hosts: tuple[NonEmptyStr, ...] = Field(min_length=1)
    fixture_id: NonEmptyStr | None = None
    checked_at: datetime
    runtime_hash: ContentHash

    @field_validator("checked_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("download runtime timestamp must include a timezone")
        return value.astimezone(UTC)

    @field_validator("allowed_hosts")
    @classmethod
    def validate_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("download runtime hosts must be unique")
        normalized = tuple(item.casefold().rstrip(".") for item in value)
        if normalized != value or any(not _is_public_host(item) for item in value):
            raise ValueError("download runtime hosts must be normalized public DNS names")
        return value

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        live = self.execution_mode is DownloadExecutionMode.LIVE_NETWORK
        if self.network_enabled is not live:
            raise ValueError("only live download runtime may enable external network access")
        if live and any(_is_reserved_dns_name(item) for item in self.allowed_hosts):
            raise ValueError("live download runtime cannot allow reserved or local DNS names")
        if self.execution_mode is DownloadExecutionMode.OFFLINE_FIXTURE:
            if self.fixture_id is None:
                raise ValueError("offline download runtime requires a fixture id")
        elif self.fixture_id is not None:
            raise ValueError("only offline download runtime may name a fixture")
        return self


class SourceDownloadApproval(StrictContract):
    candidate_id: CandidateId
    kind: DownloadApprovalKind
    approval_ref: NonEmptyStr
    approved_by_hash: ContentHash
    locator_hashes: tuple[ContentHash, ...] = Field(min_length=1)
    approved_at: datetime
    expires_at: datetime | None = None

    @field_validator("approved_at", "expires_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("download approval timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_approval(self) -> Self:
        if len(self.locator_hashes) != len(set(self.locator_hashes)):
            raise ValueError("approved locator hashes must be unique")
        if self.expires_at is not None and self.expires_at <= self.approved_at:
            raise ValueError("download approval expiry must follow approval time")
        return self


class ArtifactDownloadRequest(StrictContract):
    selected_source_set: SelectedSourceSet
    policy: DownloadPolicy
    runtime: DownloadRuntimeSnapshot
    approvals: tuple[SourceDownloadApproval, ...] = ()
    requested_at: datetime

    @field_validator("requested_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("download request timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        selected = self.selected_source_set
        if selected.sources and self.policy.max_total_bytes > selected.reserved_download_bytes:
            raise ValueError("M07 total bytes cannot exceed the M06 reserved download budget")
        if self.policy.max_total_bytes > selected.available_download_bytes:
            raise ValueError("M07 total bytes cannot exceed the remaining search download budget")
        approval_ids = tuple(item.candidate_id for item in self.approvals)
        if len(approval_ids) != len(set(approval_ids)):
            raise ValueError("download approvals must be unique per candidate")
        selected_by_id = {item.candidate_id: item for item in selected.sources}
        if not set(approval_ids).issubset(selected_by_id):
            raise ValueError("download approvals must resolve to selected M06 candidates")
        for approval in self.approvals:
            source = selected_by_id[approval.candidate_id]
            if approval.approved_at > self.requested_at:
                raise ValueError("future download approval cannot authorize this request")
            if (
                approval.kind is DownloadApprovalKind.OPEN_LICENSE_METADATA
                and source.license_decision is not LicenseDecision.ALLOWED
            ):
                raise ValueError("open-license approval requires an allowed M06 license decision")
            if (
                approval.kind is DownloadApprovalKind.OFFLINE_FIXTURE
                and self.runtime.execution_mode is not DownloadExecutionMode.OFFLINE_FIXTURE
            ):
                raise ValueError("fixture approval is valid only in offline fixture mode")
            if approval.expires_at is not None and approval.expires_at <= self.requested_at:
                raise ValueError("expired download approval cannot authorize this request")
        return self


class DownloadLocatorRecord(StrictContract):
    kind: IdentifierKind
    locator_hash: ContentHash
    safe_url: SafeHttpsUrl | None = None

    @field_validator("safe_url")
    @classmethod
    def validate_safe_url(cls, value: str | None) -> str | None:
        if value is not None:
            _require_sanitized_https_url(value, label="download locator URL")
        return value

    @model_validator(mode="after")
    def validate_locator(self) -> Self:
        if (self.kind is IdentifierKind.URL) != (self.safe_url is not None):
            raise ValueError("only URL locators may retain a sanitized URL")
        return self


class DownloadResponseMetadata(StrictContract):
    status_code: int = Field(ge=100, le=599)
    final_url: SafeHttpsUrl
    final_locator_hash: ContentHash
    declared_content_type: NonEmptyStr | None = None
    declared_content_length: int | None = Field(default=None, ge=0)
    content_disposition_filename: NonEmptyStr | None = None
    etag: NonEmptyStr | None = None
    last_modified: NonEmptyStr | None = None

    @field_validator("final_url")
    @classmethod
    def validate_final_url(cls, value: str) -> str:
        _require_sanitized_https_url(value, label="download final URL")
        return value

    @field_validator("content_disposition_filename")
    @classmethod
    def validate_filename(cls, value: str | None) -> str | None:
        if value is not None and (
            "/" in value
            or "\\" in value
            or "\x00" in value
            or value in {".", ".."}
            or len(value) > 255
        ):
            raise ValueError("content-disposition filename must be a safe basename")
        return value


class ContentInspection(StrictContract):
    detected_media_type: NonEmptyStr
    declared_media_type: NonEmptyStr | None = None
    basis: ContentDetectionBasis
    artifact_kind: ArtifactKind
    media_type_mismatch: bool
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    requires_review: bool = False

    @model_validator(mode="after")
    def validate_mismatch(self) -> Self:
        expected = (
            self.declared_media_type is not None
            and self.declared_media_type.casefold() != self.detected_media_type.casefold()
        )
        if self.media_type_mismatch is not expected:
            raise ValueError("media-type mismatch must be derived from declared and detected types")
        if self.basis is ContentDetectionBasis.UNKNOWN and self.confidence != 0.0:
            raise ValueError("unknown content detection cannot claim confidence")
        expected_review = (
            expected
            or self.basis is ContentDetectionBasis.UNKNOWN
            or self.artifact_kind is ArtifactKind.UNKNOWN
        )
        if self.requires_review is not expected_review:
            raise ValueError("content review state must be derived from deterministic inspection")
        return self


class BronzeObject(StrictContract):
    object_id: BronzeObjectId
    byte_sha256: ContentHash
    size_bytes: int = Field(gt=0)
    storage_uri: BronzeStorageUri
    media: ContentInspection
    recorded_at: datetime
    immutable: Literal[True] = True
    object_metadata_hash: ContentHash

    @field_validator("recorded_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Bronze manifest timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_content_address(self) -> Self:
        if self.object_id != f"brz_{self.byte_sha256[:32]}":
            raise ValueError("Bronze object id must derive from the byte hash")
        if self.storage_uri != f"bronze://sha256/{self.byte_sha256}":
            raise ValueError("Bronze storage URI must be content addressed")
        return self


class ArtifactAcquisition(StrictContract):
    acquisition_id: AcquisitionId
    candidate_id: CandidateId
    candidate_hash: ContentHash
    selection_rank: int = Field(ge=1)
    object_id: BronzeObjectId
    byte_sha256: ContentHash
    locator: DownloadLocatorRecord
    response: DownloadResponseMetadata | None = None
    status: AcquisitionStatus
    relationship: ArtifactRelationship
    parent_object_id: BronzeObjectId | None = None
    archive_member_path: ArchiveMemberPath | None = None
    acquired_at: datetime
    license_decision: LicenseDecision
    approval_ref: NonEmptyStr | None = None
    acquisition_hash: ContentHash

    @field_validator("acquired_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("artifact acquisition timestamp must include a timezone")
        return value.astimezone(UTC)

    @field_validator("archive_member_path")
    @classmethod
    def validate_archive_path(cls, value: str | None) -> str | None:
        if value is not None:
            _require_safe_archive_member_path(value)
        return value

    @model_validator(mode="after")
    def validate_acquisition(self) -> Self:
        if self.relationship is ArtifactRelationship.ROOT_DOWNLOAD and (
            self.parent_object_id is not None or self.archive_member_path is not None
        ):
            raise ValueError("root acquisitions cannot claim a parent object or member path")
        if self.relationship is ArtifactRelationship.LANDING_ATTACHMENT and (
            self.parent_object_id is None or self.archive_member_path is not None
        ):
            raise ValueError("landing attachments require a parent object and no member path")
        archive_member = self.relationship is ArtifactRelationship.ARCHIVE_MEMBER
        if archive_member and (self.parent_object_id is None or self.archive_member_path is None):
            raise ValueError("archive members require a parent object and safe member path")
        if archive_member == (self.response is not None):
            raise ValueError(
                "root acquisitions require response metadata; archive members inherit it"
            )
        if self.license_decision is not LicenseDecision.ALLOWED and self.approval_ref is None:
            raise ValueError("non-open acquisitions require an explicit approval reference")
        return self


class BronzeArtifactSet(DownloadArtifact):
    artifact_set_id: BronzeArtifactSetId
    selection_id: NonEmptyStr
    selected_source_set_hash: ContentHash
    objects: tuple[BronzeObject, ...]
    artifact_set_hash: ContentHash

    @model_validator(mode="after")
    def validate_objects(self) -> Self:
        object_ids = tuple(item.object_id for item in self.objects)
        byte_hashes = tuple(item.byte_sha256 for item in self.objects)
        if len(object_ids) != len(set(object_ids)) or len(byte_hashes) != len(set(byte_hashes)):
            raise ValueError("Bronze objects must be unique by id and byte hash")
        return self


class ArtifactManifest(DownloadArtifact):
    manifest_id: ArtifactManifestId
    selection_id: NonEmptyStr
    selected_source_set_hash: ContentHash
    artifact_set_hash: ContentHash
    policy_hash: ContentHash
    runtime_hash: ContentHash
    selected_candidate_ids: tuple[CandidateId, ...]
    acquisitions: tuple[ArtifactAcquisition, ...]
    manifest_hash: ContentHash

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        for values, label in (
            (self.selected_candidate_ids, "manifest selected candidates"),
            (
                tuple(item.acquisition_id for item in self.acquisitions),
                "manifest acquisition ids",
            ),
            (
                tuple(item.acquisition_hash for item in self.acquisitions),
                "manifest acquisition hashes",
            ),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        if any(item.candidate_id not in self.selected_candidate_ids for item in self.acquisitions):
            raise ValueError("manifest acquisitions must resolve to selected candidates")
        return self


class DownloadAttempt(StrictContract):
    attempt_id: DownloadAttemptId
    candidate_id: CandidateId
    locator: DownloadLocatorRecord
    attempt_number: int = Field(ge=1)
    execution_mode: DownloadExecutionMode
    network_performed: bool | None
    status: DownloadAttemptStatus
    error_code: DownloadErrorCode | None = None
    retryable: bool
    started_at: datetime
    finished_at: datetime
    bytes_received: int = Field(ge=0)
    cache_hit: bool = False
    http_status: int | None = Field(default=None, ge=100, le=599)
    byte_sha256: ContentHash | None = None
    object_id: BronzeObjectId | None = None
    acquisition_id: AcquisitionId | None = None

    @field_validator("started_at", "finished_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("download attempt timestamps must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_attempt(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("download attempt cannot finish before it starts")
        if (
            self.network_performed is True
            and self.execution_mode is not DownloadExecutionMode.LIVE_NETWORK
        ):
            raise ValueError("only live downloads may claim an external network operation")
        if (
            self.execution_mode is not DownloadExecutionMode.LIVE_NETWORK
            and self.network_performed is not False
        ):
            raise ValueError("offline and Mock downloads must prove no network operation occurred")
        succeeded = self.status in {
            DownloadAttemptStatus.STORED,
            DownloadAttemptStatus.DEDUPLICATED,
        }
        references_complete = all(
            value is not None for value in (self.byte_sha256, self.object_id, self.acquisition_id)
        )
        if succeeded != references_complete:
            raise ValueError("successful download attempts require complete artifact references")
        if succeeded == (self.error_code is not None):
            raise ValueError("successful downloads cannot have errors and other states require one")
        if self.retryable and self.status is not DownloadAttemptStatus.FAILED:
            raise ValueError("only failed download attempts may be retryable")
        if self.cache_hit and (not succeeded or self.network_performed is not False):
            raise ValueError("cache-hit attempts must succeed without a network operation")
        return self


class DownloadRunLog(DownloadArtifact):
    download_run_id: DownloadRunId
    selection_id: NonEmptyStr
    selected_source_set_hash: ContentHash
    policy_hash: ContentHash
    runtime_hash: ContentHash
    attempts: tuple[DownloadAttempt, ...]
    run_log_hash: ContentHash

    @model_validator(mode="after")
    def validate_attempts(self) -> Self:
        attempt_ids = tuple(item.attempt_id for item in self.attempts)
        attempt_keys = tuple(
            (item.candidate_id, item.locator.locator_hash, item.attempt_number)
            for item in self.attempts
        )
        if len(attempt_ids) != len(set(attempt_ids)):
            raise ValueError("download attempt ids must be unique")
        if len(attempt_keys) != len(set(attempt_keys)):
            raise ValueError("download attempt keys must be unique")
        by_locator: dict[tuple[str, str], list[DownloadAttempt]] = {}
        for attempt in self.attempts:
            key = (attempt.candidate_id, attempt.locator.locator_hash)
            by_locator.setdefault(key, []).append(attempt)
        for attempts in by_locator.values():
            ordered = sorted(attempts, key=lambda item: item.attempt_number)
            if tuple(item.attempt_number for item in ordered) != tuple(range(1, len(ordered) + 1)):
                raise ValueError("download attempt numbers must be contiguous and one-based")
            if ordered[-1].retryable:
                raise ValueError("terminal download attempts cannot remain retryable")
            if any(not item.retryable for item in ordered[:-1]):
                raise ValueError("only retryable failures may precede another attempt")
        return self


class ArtifactDownloadMetrics(StrictContract):
    selected_source_count: int = Field(ge=0)
    attempted_download_count: int = Field(ge=0)
    stored_download_count: int = Field(ge=0)
    deduplicated_download_count: int = Field(ge=0)
    skipped_download_count: int = Field(ge=0)
    failed_download_count: int = Field(ge=0)
    quarantined_download_count: int = Field(ge=0)
    cache_hit_count: int = Field(ge=0)
    review_required_object_count: int = Field(ge=0)
    acquisition_count: int = Field(ge=0)
    archive_member_count: int = Field(ge=0)
    bronze_object_count: int = Field(ge=0)
    received_bytes: int = Field(ge=0)
    persisted_unique_bytes: int = Field(ge=0)


class ArtifactStoredPayload(StrictContract):
    object_id: BronzeObjectId
    byte_sha256: ContentHash
    artifact_set_hash: ContentHash
    manifest_hash: ContentHash
    acquisition_count: int = Field(ge=1)
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash


class ArtifactDownloadCompletedPayload(StrictContract):
    status: ArtifactDownloadStatus
    artifact_set_hash: ContentHash
    manifest_hash: ContentHash
    run_log_hash: ContentHash
    bronze_object_count: int = Field(ge=0)
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash


class ArtifactDownloadResult(DownloadArtifact):
    module_id: Literal["M07"] = "M07"
    status: ArtifactDownloadStatus
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    artifact_set: BronzeArtifactSet
    manifest: ArtifactManifest
    run_log: DownloadRunLog
    warnings: tuple[NonEmptyStr, ...] = ()
    metrics: ArtifactDownloadMetrics
    events: tuple[
        EventEnvelope[ArtifactStoredPayload | ArtifactDownloadCompletedPayload],
        ...,
    ]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        metadata = (
            self.task_id,
            self.run_id,
            self.contract_version,
            self.created_at,
            self.producer_version,
        )
        artifacts = (self.artifact_set, self.manifest, self.run_log)
        if any(
            (
                item.task_id,
                item.run_id,
                item.contract_version,
                item.created_at,
                item.producer_version,
            )
            != metadata
            for item in artifacts
        ):
            raise ValueError("M07 artifacts must share result metadata")
        artifact_set = self.artifact_set
        manifest = self.manifest
        run_log = self.run_log
        if not (
            artifact_set.selection_id == manifest.selection_id == run_log.selection_id
            and artifact_set.selected_source_set_hash
            == manifest.selected_source_set_hash
            == run_log.selected_source_set_hash
            and manifest.artifact_set_hash == artifact_set.artifact_set_hash
            and manifest.policy_hash == run_log.policy_hash
            and manifest.runtime_hash == run_log.runtime_hash
        ):
            raise ValueError("M07 artifacts must share immutable upstream references")
        objects_by_id = {item.object_id: item for item in artifact_set.objects}
        if any(
            item.object_id not in objects_by_id
            or item.byte_sha256 != objects_by_id[item.object_id].byte_sha256
            for item in manifest.acquisitions
        ):
            raise ValueError("manifest acquisitions must resolve to immutable Bronze objects")
        successful_attempts = tuple(
            item
            for item in run_log.attempts
            if item.status in {DownloadAttemptStatus.STORED, DownloadAttemptStatus.DEDUPLICATED}
        )
        network_acquisitions = tuple(
            item
            for item in manifest.acquisitions
            if item.relationship is not ArtifactRelationship.ARCHIVE_MEMBER
        )
        if {item.acquisition_id for item in successful_attempts} != {
            item.acquisition_id for item in network_acquisitions
        }:
            raise ValueError("successful attempts and non-archive acquisitions must match exactly")
        if any(
            item.parent_object_id not in objects_by_id
            for item in manifest.acquisitions
            if item.parent_object_id is not None
        ):
            raise ValueError("derived acquisitions must resolve to a parent Bronze object")
        acquisitions_by_id = {item.acquisition_id: item for item in manifest.acquisitions}
        for attempt in successful_attempts:
            acquisition_id = attempt.acquisition_id
            if acquisition_id is None:
                raise ValueError("successful attempts require an acquisition id")
            acquisition = acquisitions_by_id[acquisition_id]
            obj = objects_by_id[acquisition.object_id]
            expected_acquisition_status = (
                AcquisitionStatus.STORED
                if attempt.status is DownloadAttemptStatus.STORED
                else AcquisitionStatus.DEDUPLICATED
            )
            if not (
                attempt.object_id == acquisition.object_id == obj.object_id
                and attempt.byte_sha256 == acquisition.byte_sha256 == obj.byte_sha256
                and attempt.bytes_received == (0 if attempt.cache_hit else obj.size_bytes)
                and acquisition.status is expected_acquisition_status
            ):
                raise ValueError(
                    "successful attempt references must exactly match Bronze acquisition"
                )
        expected_metrics = ArtifactDownloadMetrics(
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
        if self.metrics != expected_metrics:
            raise ValueError("M07 metrics must be derived from artifacts and attempts")
        terminal_by_locator: dict[tuple[str, str], DownloadAttempt] = {}
        for attempt in run_log.attempts:
            key = (attempt.candidate_id, attempt.locator.locator_hash)
            previous = terminal_by_locator.get(key)
            if previous is None or attempt.attempt_number > previous.attempt_number:
                terminal_by_locator[key] = attempt
        terminal_attempts = tuple(terminal_by_locator.values())
        if not manifest.selected_candidate_ids:
            expected_status = ArtifactDownloadStatus.UNSUPPORTED
        elif not manifest.acquisitions:
            if any(
                item.error_code is DownloadErrorCode.LICENSE_APPROVAL_REQUIRED
                or item.status is DownloadAttemptStatus.QUARANTINED
                for item in run_log.attempts
            ):
                expected_status = ArtifactDownloadStatus.NEEDS_REVIEW
            elif any(item.status is DownloadAttemptStatus.FAILED for item in run_log.attempts):
                expected_status = ArtifactDownloadStatus.FAILED
            else:
                expected_status = ArtifactDownloadStatus.UNSUPPORTED
        elif (
            any(
                item.status
                not in {DownloadAttemptStatus.STORED, DownloadAttemptStatus.DEDUPLICATED}
                for item in terminal_attempts
            )
            or {item.candidate_id for item in manifest.acquisitions}
            != set(manifest.selected_candidate_ids)
            or any(item.media.requires_review for item in artifact_set.objects)
        ):
            expected_status = ArtifactDownloadStatus.PARTIAL
        else:
            expected_status = ArtifactDownloadStatus.SUCCEEDED
        expected_warnings = tuple(
            f"{item.error_code.value}:{item.attempt_id}"
            for item in run_log.attempts
            if item.error_code is not None
        ) + tuple(
            f"{(DownloadErrorCode.CONTENT_TYPE_MISMATCH if item.media.media_type_mismatch else DownloadErrorCode.UNSUPPORTED_MEDIA_TYPE).value}:{item.object_id}"
            for item in artifact_set.objects
            if item.media.requires_review
        )
        if self.status is not expected_status or self.warnings != expected_warnings:
            raise ValueError("M07 status and warnings must be artifact-derived")
        stored_events = tuple(
            event for event in self.events if event.event_type.value == "artifact.stored"
        )
        completed_events = tuple(
            event
            for event in self.events
            if event.event_type.value == "artifact.download.completed"
        )
        if len(stored_events) != len(artifact_set.objects):
            raise ValueError("M07 must emit one artifact.stored event per Bronze object")
        if len(completed_events) != 1 or len(self.events) != len(stored_events) + 1:
            raise ValueError("M07 must emit exactly one artifact.download.completed event")
        acquisitions_by_object = {
            object_id: sum(item.object_id == object_id for item in manifest.acquisitions)
            for object_id in objects_by_id
        }
        event_object_ids = tuple(
            event.payload.object_id
            for event in stored_events
            if isinstance(event.payload, ArtifactStoredPayload)
        )
        if len(event_object_ids) != len(set(event_object_ids)):
            raise ValueError("artifact.stored events must refer to unique Bronze objects")
        for event in stored_events:
            payload = event.payload
            if not isinstance(payload, ArtifactStoredPayload):
                raise ValueError("artifact.stored event requires an artifact payload")
            event_object = objects_by_id.get(payload.object_id)
            if (
                event.event_type.value != "artifact.stored"
                or event.task_id != self.task_id
                or event.run_id != self.run_id
                or event.occurred_at != self.created_at
                or event.schema_version != self.contract_version
                or event.producer.component != "artifact_download_service"
                or event.producer.version != self.producer_version
                or event.correlation_id != self.input_hash
                or event.causation_event_id is not None
                or event_object is None
                or payload.byte_sha256 != event_object.byte_sha256
                or payload.artifact_set_hash != artifact_set.artifact_set_hash
                or payload.manifest_hash != manifest.manifest_hash
                or payload.acquisition_count != acquisitions_by_object[payload.object_id]
                or payload.input_hash != self.input_hash
                or payload.output_hash != self.output_hash
                or payload.idempotency_key != self.idempotency_key
            ):
                raise ValueError("artifact.stored event must refer to this M07 result")
        completed = completed_events[0]
        completed_payload = completed.payload
        if (
            not isinstance(completed_payload, ArtifactDownloadCompletedPayload)
            or completed.event_type.value != "artifact.download.completed"
            or completed.task_id != self.task_id
            or completed.run_id != self.run_id
            or completed.occurred_at != self.created_at
            or completed.schema_version != self.contract_version
            or completed.producer.component != "artifact_download_service"
            or completed.producer.version != self.producer_version
            or completed.correlation_id != self.input_hash
            or completed.causation_event_id is not None
            or completed_payload.status is not self.status
            or completed_payload.artifact_set_hash != artifact_set.artifact_set_hash
            or completed_payload.manifest_hash != manifest.manifest_hash
            or completed_payload.run_log_hash != run_log.run_log_hash
            or completed_payload.bronze_object_count != len(artifact_set.objects)
            or completed_payload.input_hash != self.input_hash
            or completed_payload.output_hash != self.output_hash
            or completed_payload.idempotency_key != self.idempotency_key
        ):
            raise ValueError("artifact.download.completed event must refer to this M07 result")
        return self


def _require_sanitized_https_url(value: str, *, label: str) -> None:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} must use a valid HTTPS port") from exc
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
        or not _is_public_host(parsed.hostname.casefold().rstrip("."))
    ):
        raise ValueError(
            f"{label} must be a sanitized public HTTPS URL without credentials, query, "
            "fragment, or custom port"
        )


def _is_public_host(host: str) -> bool:
    try:
        ip_address(host)
    except ValueError:
        return bool(_PUBLIC_HOST_PATTERN.fullmatch(host)) and host != "localhost"
    return False


def _is_reserved_dns_name(host: str) -> bool:
    return host == "localhost" or host.endswith(
        (
            ".localhost",
            ".local",
            ".internal",
            ".home",
            ".lan",
            ".test",
            ".invalid",
            ".example",
        )
    )


def _require_safe_archive_member_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        "\\" in value
        or "\x00" in value
        or value.startswith("/")
        or path.is_absolute()
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in path.parts[0]
    ):
        raise ValueError("archive member path must be a safe normalized relative POSIX path")

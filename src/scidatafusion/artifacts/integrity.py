"""Canonical M07 hashes and end-to-end artifact integrity verification."""

from __future__ import annotations

import hmac
from typing import NoReturn
from urllib.parse import urlsplit

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.artifacts import (
    ArtifactAcquisition,
    ArtifactDownloadCompletedPayload,
    ArtifactDownloadRequest,
    ArtifactDownloadResult,
    ArtifactManifest,
    ArtifactRelationship,
    ArtifactStoredPayload,
    BronzeArtifactSet,
    BronzeObject,
    DownloadPolicy,
    DownloadRunLog,
    DownloadRuntimeSnapshot,
)
from scidatafusion.contracts.connectors import CandidateIdentifier, IdentifierKind
from scidatafusion.contracts.selection import LicenseDecision
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.selection import calculate_selected_source_set_hash


def calculate_download_policy_hash(policy: DownloadPolicy) -> str:
    """Hash every deterministic download and archive limit."""

    return canonical_hash(policy.model_dump(mode="json"))


def calculate_download_runtime_hash(runtime: DownloadRuntimeSnapshot) -> str:
    """Hash the exact execution mode, allowlist, fixture, and check time."""

    return canonical_hash(runtime.model_dump(mode="json", exclude={"runtime_hash"}))


def calculate_candidate_locator_hash(locator: CandidateIdentifier) -> str:
    """Bind authorization to the exact normalized M06 locator value."""

    return canonical_hash(locator.model_dump(mode="json"))


def calculate_url_locator_hash(url: str) -> str:
    """Hash one exact resolved URL without persisting its potentially sensitive query."""

    return canonical_hash({"kind": IdentifierKind.URL.value, "value": url})


def calculate_artifact_download_input_hash(request: ArtifactDownloadRequest) -> str:
    """Hash M06 selection, limits, runtime, timestamp, and order-insensitive approvals."""

    approvals = sorted(
        (item.model_dump(mode="json") for item in request.approvals),
        key=lambda item: str(item["candidate_id"]),
    )
    return canonical_hash(
        {
            "approvals": approvals,
            "policy_hash": calculate_download_policy_hash(request.policy),
            "requested_at": request.requested_at.isoformat(),
            "runtime_hash": request.runtime.runtime_hash,
            "selected_source_set_hash": (request.selected_source_set.selected_source_set_hash),
        }
    )


def calculate_artifact_download_idempotency_key(
    request: ArtifactDownloadRequest,
    producer_version: str,
) -> str:
    """Bind replay identity to the request, module, contract, and producer version."""

    return canonical_hash(
        {
            "contract_version": request.selected_source_set.contract_version,
            "input_hash": calculate_artifact_download_input_hash(request),
            "module_id": "M07",
            "producer_version": producer_version,
            "task_id": request.selected_source_set.task_id,
        }
    )


def calculate_bronze_object_metadata_hash(obj: BronzeObject) -> str:
    """Hash immutable object metadata while the byte hash addresses raw content."""

    return canonical_hash(obj.model_dump(mode="json", exclude={"object_metadata_hash"}))


def calculate_acquisition_hash(acquisition: ArtifactAcquisition) -> str:
    """Hash one source/locator/object provenance edge."""

    return canonical_hash(
        acquisition.model_dump(
            mode="json",
            exclude={"acquisition_hash", "acquisition_id"},
        )
    )


def calculate_bronze_artifact_set_hash(artifact_set: BronzeArtifactSet) -> str:
    """Hash the ordered unique Bronze object snapshot."""

    return canonical_hash(
        artifact_set.model_dump(
            mode="json",
            exclude={"artifact_set_hash", "artifact_set_id", "created_at"},
        )
    )


def calculate_artifact_manifest_hash(manifest: ArtifactManifest) -> str:
    """Hash all source acquisitions and immutable policy/runtime references."""

    return canonical_hash(
        manifest.model_dump(
            mode="json",
            exclude={"created_at", "manifest_hash", "manifest_id"},
        )
    )


def calculate_download_run_log_hash(run_log: DownloadRunLog) -> str:
    """Hash every attempt and its exact execution accounting."""

    return canonical_hash(
        run_log.model_dump(
            mode="json",
            exclude={"created_at", "download_run_id", "run_log_hash"},
        )
    )


def calculate_artifact_download_output_hash(result: ArtifactDownloadResult) -> str:
    """Hash the complete semantic M07 result apart from event transport identifiers."""

    return canonical_hash(
        {
            "artifact_set_hash": result.artifact_set.artifact_set_hash,
            "contract_version": result.contract_version,
            "created_at": result.created_at.isoformat(),
            "events": [
                event.model_dump(
                    mode="json",
                    exclude={"payload": {"output_hash"}},
                )
                for event in result.events
            ],
            "idempotency_key": result.idempotency_key,
            "input_hash": result.input_hash,
            "manifest_hash": result.manifest.manifest_hash,
            "metrics": result.metrics.model_dump(mode="json"),
            "producer_version": result.producer_version,
            "run_id": result.run_id,
            "run_log_hash": result.run_log.run_log_hash,
            "status": result.status.value,
            "task_id": result.task_id,
            "warnings": list(result.warnings),
        }
    )


def verify_artifact_download_request_integrity(request: ArtifactDownloadRequest) -> None:
    """Reject a tampered M06 selection or forged M07 runtime snapshot."""

    selected = request.selected_source_set
    selected_hash = calculate_selected_source_set_hash(selected)
    if not (
        hmac.compare_digest(selected.selected_source_set_hash, selected_hash)
        and hmac.compare_digest(selected.selection_id, f"sel_{selected_hash[:32]}")
    ):
        _fail("M07 selected source set does not match its immutable M06 hash")
    runtime_hash = calculate_download_runtime_hash(request.runtime)
    if not hmac.compare_digest(request.runtime.runtime_hash, runtime_hash):
        _fail("M07 runtime snapshot does not match its immutable hash")


def verify_artifact_download_integrity(
    result: ArtifactDownloadResult,
    request: ArtifactDownloadRequest,
    store: BronzeByteStore,
) -> None:
    """Verify M07 content, provenance, storage replay, hashes, and authorization."""

    verify_artifact_download_request_integrity(request)
    expected_input_hash = calculate_artifact_download_input_hash(request)
    expected_idempotency_key = calculate_artifact_download_idempotency_key(
        request,
        result.producer_version,
    )
    policy_hash = calculate_download_policy_hash(request.policy)
    selected = request.selected_source_set
    selected_by_id = {item.candidate_id: item for item in selected.sources}
    if not (
        hmac.compare_digest(result.input_hash, expected_input_hash)
        and hmac.compare_digest(result.idempotency_key, expected_idempotency_key)
        and result.task_id == selected.task_id
        and result.run_id == selected.run_id
        and result.contract_version == selected.contract_version
        and result.artifact_set.selection_id == selected.selection_id
        and result.artifact_set.selected_source_set_hash == selected.selected_source_set_hash
        and result.manifest.policy_hash == policy_hash
        and result.manifest.runtime_hash == request.runtime.runtime_hash
        and result.manifest.selected_candidate_ids
        == tuple(item.candidate_id for item in selected.sources)
    ):
        _fail("M07 result does not match its immutable request snapshot")
    if any(
        attempt.execution_mode is not request.runtime.execution_mode
        for attempt in result.run_log.attempts
    ):
        _fail("M07 attempt execution mode does not match the runtime snapshot")

    approvals = {item.candidate_id: item for item in request.approvals}
    selected_locator_hashes = {
        candidate_id: {
            calculate_candidate_locator_hash(locator) for locator in source.download_locators
        }
        for candidate_id, source in selected_by_id.items()
    }
    acquisitions_by_object: dict[str, list[ArtifactAcquisition]] = {}
    for acquisition in result.manifest.acquisitions:
        source = selected_by_id.get(acquisition.candidate_id)
        if source is None:
            _fail("M07 acquisition refers to a source outside the M06 selection")
        if not (
            acquisition.candidate_hash == source.candidate_hash
            and acquisition.selection_rank == source.selection_rank
            and acquisition.license_decision is source.license_decision
        ):
            _fail("M07 acquisition source metadata is not an exact M06 projection")
        if acquisition.relationship is ArtifactRelationship.ROOT_DOWNLOAD and (
            acquisition.locator.locator_hash
            not in selected_locator_hashes[acquisition.candidate_id]
        ):
            _fail("M07 root acquisition must use an exact selected locator")
        approval = approvals.get(acquisition.candidate_id)
        required_approval_hashes = {acquisition.locator.locator_hash}
        if acquisition.response is not None:
            required_approval_hashes.add(acquisition.response.final_locator_hash)
        if source.license_decision is not LicenseDecision.ALLOWED and (
            approval is None
            or acquisition.approval_ref != approval.approval_ref
            or not required_approval_hashes.issubset(approval.locator_hashes)
        ):
            _fail("M07 acquisition lacks exact locator-bound license approval")
        for url in (
            acquisition.locator.safe_url,
            acquisition.response.final_url if acquisition.response is not None else None,
        ):
            if url is not None and urlsplit(url).hostname not in request.runtime.allowed_hosts:
                _fail("M07 acquisition URL host is outside the runtime allowlist")
        acquisitions_by_object.setdefault(acquisition.object_id, []).append(acquisition)

    objects_by_id = {item.object_id: item for item in result.artifact_set.objects}
    for event in result.events:
        if isinstance(event.payload, ArtifactStoredPayload):
            identity = event.payload.object_id
        elif isinstance(event.payload, ArtifactDownloadCompletedPayload):
            identity = "completed"
        else:
            _fail("M07 event has an unsupported payload")
        expected_event_id = f"evt_{canonical_hash((result.idempotency_key, identity))[:32]}"
        if not hmac.compare_digest(event.event_id, expected_event_id):
            _fail("M07 artifact event id is not the deterministic object event id")
    for obj in result.artifact_set.objects:
        if not hmac.compare_digest(
            obj.object_metadata_hash,
            calculate_bronze_object_metadata_hash(obj),
        ):
            _fail("M07 Bronze object metadata hash is invalid")
        content = store.read(obj.byte_sha256)
        if len(content) != obj.size_bytes:
            _fail("M07 Bronze replay size differs from the manifest")
    for acquisition in result.manifest.acquisitions:
        expected_hash = calculate_acquisition_hash(acquisition)
        if not (
            hmac.compare_digest(acquisition.acquisition_hash, expected_hash)
            and hmac.compare_digest(
                acquisition.acquisition_id,
                f"acq_{expected_hash[:16]}",
            )
            and acquisition.object_id in objects_by_id
        ):
            _fail("M07 acquisition content does not match its immutable hash")

    artifact_set_hash = calculate_bronze_artifact_set_hash(result.artifact_set)
    manifest_hash = calculate_artifact_manifest_hash(result.manifest)
    run_log_hash = calculate_download_run_log_hash(result.run_log)
    output_hash = calculate_artifact_download_output_hash(result)
    if not (
        hmac.compare_digest(result.artifact_set.artifact_set_hash, artifact_set_hash)
        and hmac.compare_digest(
            result.artifact_set.artifact_set_id,
            f"bas_{artifact_set_hash[:32]}",
        )
        and hmac.compare_digest(result.manifest.manifest_hash, manifest_hash)
        and hmac.compare_digest(result.manifest.manifest_id, f"amf_{manifest_hash[:32]}")
        and hmac.compare_digest(result.run_log.run_log_hash, run_log_hash)
        and hmac.compare_digest(
            result.run_log.download_run_id,
            f"dwr_{run_log_hash[:32]}",
        )
        and hmac.compare_digest(result.output_hash, output_hash)
    ):
        _fail("M07 result content does not match its immutable hashes")


def _fail(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)

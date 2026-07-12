from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from scidatafusion.cli import _build_search_planning, _execute_offline_connectors
from scidatafusion.contracts.artifacts import (
    AcquisitionStatus,
    ArtifactAcquisition,
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
    ContentDetectionBasis,
    ContentInspection,
    DownloadApprovalKind,
    DownloadAttempt,
    DownloadAttemptStatus,
    DownloadErrorCode,
    DownloadExecutionMode,
    DownloadLocatorRecord,
    DownloadPolicy,
    DownloadResponseMetadata,
    DownloadRunLog,
    DownloadRuntimeSnapshot,
    SourceDownloadApproval,
)
from scidatafusion.contracts.connectors import IdentifierKind
from scidatafusion.contracts.events import EventEnvelope, EventType, ProducerRef
from scidatafusion.contracts.selection import (
    LicenseDecision,
    SelectedSourceSet,
    SourceSelectionRequest,
)
from scidatafusion.selection import SourceSelectionService

NOW = datetime(2026, 7, 12, 7, 0, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
BYTE_HASH = "c" * 64
CANDIDATE_ID = "src_11111111111111111111111111111111"
OBJECT_ID = f"brz_{BYTE_HASH[:32]}"
ACQUISITION_ID = "acq_1111111111111111"


@pytest.fixture(scope="module")
def selected_source_set() -> SelectedSourceSet:
    goal = "Study Type Ia supernova light curves using multi-source integration into CSV."
    phase1, planning = _build_search_planning(goal, "authenticated-m07-reviewer")
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


def _media() -> ContentInspection:
    return ContentInspection(
        detected_media_type="application/pdf",
        declared_media_type="application/pdf",
        basis=ContentDetectionBasis.MAGIC_BYTES,
        artifact_kind=ArtifactKind.DOCUMENT,
        media_type_mismatch=False,
        confidence=1.0,
    )


def _object() -> BronzeObject:
    return BronzeObject(
        object_id=OBJECT_ID,
        byte_sha256=BYTE_HASH,
        size_bytes=128,
        storage_uri=f"bronze://sha256/{BYTE_HASH}",
        media=_media(),
        recorded_at=NOW,
        object_metadata_hash=HASH_A,
    )


def _locator() -> DownloadLocatorRecord:
    return DownloadLocatorRecord(
        kind=IdentifierKind.URL,
        locator_hash=HASH_A,
        safe_url="https://example.org/paper.pdf",
    )


def _acquisition() -> ArtifactAcquisition:
    return ArtifactAcquisition(
        acquisition_id=ACQUISITION_ID,
        candidate_id=CANDIDATE_ID,
        candidate_hash=HASH_A,
        selection_rank=1,
        object_id=OBJECT_ID,
        byte_sha256=BYTE_HASH,
        locator=_locator(),
        response=DownloadResponseMetadata(
            status_code=200,
            final_url="https://example.org/paper.pdf",
            final_locator_hash=HASH_A,
            declared_content_type="application/pdf",
            declared_content_length=128,
            content_disposition_filename="paper.pdf",
        ),
        status=AcquisitionStatus.STORED,
        relationship=ArtifactRelationship.ROOT_DOWNLOAD,
        acquired_at=NOW,
        license_decision=LicenseDecision.ALLOWED,
        acquisition_hash=HASH_B,
    )


def _artifact_set() -> BronzeArtifactSet:
    return BronzeArtifactSet(
        task_id="tsk_11111111111111111111111111111111",
        run_id="run_11111111111111111111111111111111",
        contract_version="1.0.0",
        created_at=NOW,
        producer_version="1.0.0",
        artifact_set_id="bas_11111111111111111111111111111111",
        selection_id="sel_11111111111111111111111111111111",
        selected_source_set_hash=HASH_A,
        objects=(_object(),),
        artifact_set_hash=HASH_B,
    )


def _manifest() -> ArtifactManifest:
    return ArtifactManifest(
        task_id="tsk_11111111111111111111111111111111",
        run_id="run_11111111111111111111111111111111",
        contract_version="1.0.0",
        created_at=NOW,
        producer_version="1.0.0",
        manifest_id="amf_11111111111111111111111111111111",
        selection_id="sel_11111111111111111111111111111111",
        selected_source_set_hash=HASH_A,
        artifact_set_hash=HASH_B,
        policy_hash=HASH_A,
        runtime_hash=HASH_B,
        selected_candidate_ids=(CANDIDATE_ID,),
        acquisitions=(_acquisition(),),
        manifest_hash=HASH_A,
    )


def _attempt() -> DownloadAttempt:
    return DownloadAttempt(
        attempt_id="dat_1111111111111111",
        candidate_id=CANDIDATE_ID,
        locator=_locator(),
        attempt_number=1,
        execution_mode=DownloadExecutionMode.OFFLINE_FIXTURE,
        network_performed=False,
        status=DownloadAttemptStatus.STORED,
        retryable=False,
        started_at=NOW,
        finished_at=NOW,
        bytes_received=128,
        http_status=200,
        byte_sha256=BYTE_HASH,
        object_id=OBJECT_ID,
        acquisition_id=ACQUISITION_ID,
    )


def _run_log() -> DownloadRunLog:
    return DownloadRunLog(
        task_id="tsk_11111111111111111111111111111111",
        run_id="run_11111111111111111111111111111111",
        contract_version="1.0.0",
        created_at=NOW,
        producer_version="1.0.0",
        download_run_id="dwr_11111111111111111111111111111111",
        selection_id="sel_11111111111111111111111111111111",
        selected_source_set_hash=HASH_A,
        policy_hash=HASH_A,
        runtime_hash=HASH_B,
        attempts=(_attempt(),),
        run_log_hash=HASH_A,
    )


def _result() -> ArtifactDownloadResult:
    metrics = ArtifactDownloadMetrics(
        selected_source_count=1,
        attempted_download_count=1,
        stored_download_count=1,
        deduplicated_download_count=0,
        skipped_download_count=0,
        failed_download_count=0,
        quarantined_download_count=0,
        acquisition_count=1,
        archive_member_count=0,
        bronze_object_count=1,
        received_bytes=128,
        persisted_unique_bytes=128,
    )
    payload = ArtifactStoredPayload(
        object_id=OBJECT_ID,
        byte_sha256=BYTE_HASH,
        artifact_set_hash=HASH_B,
        manifest_hash=HASH_A,
        acquisition_count=1,
        input_hash=HASH_A,
        output_hash=HASH_B,
        idempotency_key=HASH_A,
    )
    return ArtifactDownloadResult(
        task_id="tsk_11111111111111111111111111111111",
        run_id="run_11111111111111111111111111111111",
        contract_version="1.0.0",
        created_at=NOW,
        producer_version="1.0.0",
        status=ArtifactDownloadStatus.SUCCEEDED,
        input_hash=HASH_A,
        output_hash=HASH_B,
        idempotency_key=HASH_A,
        artifact_set=_artifact_set(),
        manifest=_manifest(),
        run_log=_run_log(),
        metrics=metrics,
        events=(
            EventEnvelope[ArtifactStoredPayload](
                event_type=EventType.ARTIFACT_STORED,
                task_id="tsk_11111111111111111111111111111111",
                run_id="run_11111111111111111111111111111111",
                occurred_at=NOW,
                producer=ProducerRef(component="artifact_download_service", version="1.0.0"),
                correlation_id=HASH_A,
                payload=payload,
            ),
        ),
    )


def test_m07_contracts_form_a_strict_cross_linked_result() -> None:
    result = _result()

    assert result.module_id == "M07"
    assert result.metrics.bronze_object_count == 1
    assert result.artifact_set.objects[0].immutable

    payload = result.model_dump(mode="python")
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        ArtifactDownloadResult.model_validate(payload)


def test_content_addresses_media_and_attempt_metrics_are_derived() -> None:
    obj = _object().model_dump(mode="python")
    obj["storage_uri"] = f"bronze://sha256/{HASH_A}"
    with pytest.raises(ValidationError, match="content addressed"):
        BronzeObject.model_validate(obj)

    media = _media().model_dump(mode="python")
    media["declared_media_type"] = "text/html"
    with pytest.raises(ValidationError, match="mismatch"):
        ContentInspection.model_validate(media)

    attempt = _attempt().model_dump(mode="python")
    attempt["error_code"] = DownloadErrorCode.HTTP_ERROR
    with pytest.raises(ValidationError, match="cannot have errors"):
        DownloadAttempt.model_validate(attempt)

    attempt = _attempt().model_dump(mode="python")
    attempt["network_performed"] = None
    with pytest.raises(ValidationError, match="must prove no network"):
        DownloadAttempt.model_validate(attempt)

    result = _result().model_dump(mode="python")
    result["metrics"]["received_bytes"] = 127
    with pytest.raises(ValidationError, match="metrics"):
        ArtifactDownloadResult.model_validate(result)

    result = _result().model_dump(mode="python")
    result["run_log"]["attempts"][0]["bytes_received"] = 127
    result["metrics"]["received_bytes"] = 127
    with pytest.raises(ValidationError, match="exactly match Bronze"):
        ArtifactDownloadResult.model_validate(result)


def test_urls_archive_paths_runtime_and_response_names_fail_closed() -> None:
    with pytest.raises(ValidationError, match="sanitized public HTTPS"):
        DownloadLocatorRecord(
            kind=IdentifierKind.URL,
            locator_hash=HASH_A,
            safe_url="https://127.0.0.1/file?token=secret",
        )
    with pytest.raises(ValidationError, match="safe basename"):
        DownloadResponseMetadata(
            status_code=200,
            final_url="https://example.org/file",
            final_locator_hash=HASH_A,
            content_disposition_filename="../escape.csv",
        )
    acquisition = _acquisition().model_dump(mode="python")
    acquisition.update(
        {
            "relationship": ArtifactRelationship.ARCHIVE_MEMBER,
            "parent_object_id": OBJECT_ID,
            "archive_member_path": "../../escape.csv",
        }
    )
    with pytest.raises(ValidationError, match="safe normalized"):
        ArtifactAcquisition.model_validate(acquisition)
    root = _acquisition().model_dump(mode="python")
    root["response"] = None
    with pytest.raises(ValidationError, match="root acquisitions require"):
        ArtifactAcquisition.model_validate(root)
    with pytest.raises(ValidationError, match="only live"):
        DownloadRuntimeSnapshot(
            execution_mode=DownloadExecutionMode.OFFLINE_FIXTURE,
            network_enabled=True,
            allowed_hosts=("example.org",),
            fixture_id="fixture-v1",
            checked_at=NOW,
            runtime_hash=HASH_A,
        )
    with pytest.raises(ValidationError, match="public DNS"):
        DownloadRuntimeSnapshot(
            execution_mode=DownloadExecutionMode.LIVE_NETWORK,
            network_enabled=True,
            allowed_hosts=("localhost",),
            checked_at=NOW,
            runtime_hash=HASH_A,
        )
    with pytest.raises(ValidationError, match="reserved or local"):
        DownloadRuntimeSnapshot(
            execution_mode=DownloadExecutionMode.LIVE_NETWORK,
            network_enabled=True,
            allowed_hosts=("api.example",),
            checked_at=NOW,
            runtime_hash=HASH_A,
        )


def test_download_request_binds_budget_approvals_and_runtime(
    selected_source_set: SelectedSourceSet,
) -> None:
    source = selected_source_set.sources[0]
    runtime = DownloadRuntimeSnapshot(
        execution_mode=DownloadExecutionMode.OFFLINE_FIXTURE,
        network_enabled=False,
        allowed_hosts=("data.example",),
        fixture_id="ia-m07-v1",
        checked_at=NOW,
        runtime_hash=HASH_A,
    )
    approval = SourceDownloadApproval(
        candidate_id=source.candidate_id,
        kind=DownloadApprovalKind.OFFLINE_FIXTURE,
        approval_ref="fixture-license-review:m07-v1",
        approved_by_hash=HASH_A,
        locator_hashes=(HASH_B,),
        approved_at=NOW - timedelta(minutes=1),
    )
    request = ArtifactDownloadRequest(
        selected_source_set=selected_source_set,
        policy=DownloadPolicy(
            max_total_bytes=selected_source_set.reserved_download_bytes,
            max_file_bytes=selected_source_set.policy.unknown_size_reservation_bytes,
        ),
        runtime=runtime,
        approvals=(approval,),
        requested_at=NOW,
    )
    assert request.runtime.execution_mode is DownloadExecutionMode.OFFLINE_FIXTURE

    excessive = request.model_dump(mode="python")
    excessive["policy"]["max_total_bytes"] = selected_source_set.reserved_download_bytes + 1
    with pytest.raises(ValidationError, match="reserved download budget"):
        ArtifactDownloadRequest.model_validate(excessive)

    invalid_approval = approval.model_copy(
        update={"kind": DownloadApprovalKind.OPEN_LICENSE_METADATA}
    )
    with pytest.raises(ValidationError, match="allowed M06 license"):
        ArtifactDownloadRequest(
            selected_source_set=selected_source_set,
            policy=request.policy,
            runtime=runtime,
            approvals=(invalid_approval,),
            requested_at=NOW,
        )

    expired = approval.model_copy(update={"expires_at": NOW - timedelta(seconds=1)})
    with pytest.raises(ValidationError, match="expired"):
        ArtifactDownloadRequest(
            selected_source_set=selected_source_set,
            policy=request.policy,
            runtime=runtime,
            approvals=(expired,),
            requested_at=NOW,
        )


def test_result_rejects_event_and_manifest_projection_tampering() -> None:
    result = _result()
    payload = result.model_dump(mode="python")
    payload["events"][0]["payload"]["acquisition_count"] = 2
    with pytest.raises(ValidationError, match=r"artifact\.stored"):
        ArtifactDownloadResult.model_validate(payload)

    payload = result.model_dump(mode="python")
    payload["manifest"]["acquisitions"][0]["byte_sha256"] = HASH_A
    with pytest.raises(ValidationError, match="resolve to immutable"):
        ArtifactDownloadResult.model_validate(payload)

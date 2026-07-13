"""M20 acceptance tests for quality-gated exports and reproduction packages."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import polars as pl
import pytest
from pydantic import ValidationError

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.cli import _build_search_planning, _execute_offline_knowledge
from scidatafusion.contracts.delivery import (
    DeliveryArtifactKind,
    DeliveryRequest,
    DeliveryResult,
    DeliveryStatus,
)
from scidatafusion.delivery.checkpoints import MemoryDeliveryCheckpointStore
from scidatafusion.delivery.downloads import DownloadTicketSigner
from scidatafusion.delivery.exporters import ProvenanceExporter, TabularExporter
from scidatafusion.delivery.fixtures import build_offline_delivery_bundle
from scidatafusion.delivery.integrity import verify_delivery_request, verify_delivery_result
from scidatafusion.delivery.packager import (
    NotebookGenerator,
    ReproducibilityPackager,
    parse_notebook,
)
from scidatafusion.delivery.service import (
    DeliveryOrchestrator,
    _kind_for,
    _media_type_for,
)
from scidatafusion.delivery.storage import MemoryDeliveryStore
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.quality.service import _formal_gold_dataset

GOAL = "Study Type Ia supernova light curves using multi-source data integration into CSV."
QUERY = "quality evidence observation time magnitude"


@pytest.fixture(scope="module")
def delivery_chain() -> tuple[
    DeliveryRequest,
    DeliveryResult,
    BronzeByteStore,
    DeliveryOrchestrator,
]:
    phase1, planning = _build_search_planning(GOAL, "m20-reviewer")
    assert phase1.confirmation is not None
    assert planning is not None
    knowledge_request, knowledge_result, store = asyncio.run(
        _execute_offline_knowledge(phase1.confirmation.contract, planning, QUERY)
    )
    bundle = build_offline_delivery_bundle(
        not_before=knowledge_result.created_at,
        code_revision="test-revision",
    )
    request = DeliveryRequest(
        knowledge_request=knowledge_request,
        knowledge_result=knowledge_result,
        policy=bundle.policy,
        runtime=bundle.runtime,
        requested_at=bundle.runtime.checked_at,
    )
    service = DeliveryOrchestrator(bronze_store=store)
    result = asyncio.run(service.execute(request))
    return request, result, store, service


def test_review_delivery_is_traceable_downloadable_and_withholds_gold(
    delivery_chain: tuple[DeliveryRequest, DeliveryResult, BronzeByteStore, DeliveryOrchestrator],
) -> None:
    request, result, bronze_store, service = delivery_chain
    verify_delivery_result(result, request, bronze_store, service.delivery_store)
    assert result.status is DeliveryStatus.NEEDS_REVIEW
    assert result.metrics.formal_gold_record_count == 0
    assert result.metrics.quality_issue_count == 3
    assert result.metrics.csv_parquet_consistency == 0.0
    assert result.metrics.scientific_value_mutation_count == 0
    assert result.metrics.model_attempt_count == result.metrics.network_attempt_count == 0
    assert result.event.event_type.value == "delivery.completed"
    assert result.event.causation_event_id == request.knowledge_result.event.event_id
    kinds = {item.kind for item in result.manifest.files}
    assert DeliveryArtifactKind.CSV not in kinds
    assert DeliveryArtifactKind.PARQUET not in kinds
    assert DeliveryArtifactKind.QUALITY_REPORT in kinds
    assert DeliveryArtifactKind.EVIDENCE_GRAPH in kinds
    assert result.manifest.known_limitations


def test_package_has_canonical_manifest_and_independently_verifiable_hashes(
    delivery_chain: tuple[DeliveryRequest, DeliveryResult, BronzeByteStore, DeliveryOrchestrator],
    tmp_path: Path,
) -> None:
    _, result, _, service = delivery_chain
    package = service.delivery_store.get(result.package.sha256)
    assert package is not None
    assert hashlib.sha256(package).hexdigest() == result.package.sha256
    with zipfile.ZipFile(BytesIO(package)) as archive:
        assert archive.namelist() == sorted(archive.namelist())
        assert "manifest.json" in archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["manifest_hash"] == result.manifest.manifest_hash
        for item in manifest["files"]:
            payload = archive.read(item["filename"])
            assert hashlib.sha256(payload).hexdigest() == item["sha256"]
            target = tmp_path / item["filename"]
            target.write_bytes(payload)
        (tmp_path / "manifest.json").write_bytes(archive.read("manifest.json"))
    notebook = json.loads((tmp_path / "verify_delivery.ipynb").read_text(encoding="utf-8"))
    source = "".join(notebook["cells"][1]["source"])
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", source],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        env={
            key: value
            for key, value in os.environ.items()
            if key != "COVERAGE_PROCESS_START" and not key.startswith("COV_CORE_")
        },
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert "verified" in completed.stdout


def test_replay_and_force_recompute_are_byte_identical(
    delivery_chain: tuple[DeliveryRequest, DeliveryResult, BronzeByteStore, DeliveryOrchestrator],
) -> None:
    request, first, _, service = delivery_chain
    replay = asyncio.run(service.execute(request))
    forced = asyncio.run(service.execute(request.model_copy(update={"force_recompute": True})))
    assert replay == first
    assert forced == first
    assert service.delivery_store.get(first.package.sha256) is not None


def test_checkpoint_replays_strict_contract_when_artifact_store_is_retained(
    delivery_chain: tuple[DeliveryRequest, DeliveryResult, BronzeByteStore, DeliveryOrchestrator],
) -> None:
    request, first, bronze_store, original = delivery_chain
    checkpoints = MemoryDeliveryCheckpointStore()
    checkpoints.save(first)
    resumed = DeliveryOrchestrator(
        bronze_store=bronze_store,
        delivery_store=original.delivery_store,
        checkpoints=checkpoints,
    )
    assert asyncio.run(resumed.execute(request)) == first
    with pytest.raises(AppError) as captured:
        checkpoints.load("not-a-hash")
    assert captured.value.code is ErrorCode.INVALID_REQUEST


def test_tabular_exporter_preserves_formal_gold_strings_across_csv_and_parquet(
    delivery_chain: tuple[DeliveryRequest, DeliveryResult, BronzeByteStore, DeliveryOrchestrator],
) -> None:
    request, _, _, _ = delivery_chain
    quality = request.knowledge_request.quality_result
    quality_request = request.knowledge_request.quality_request
    comparison = quality.quality_report.comparison.model_copy(
        update={
            "before_score": 1.0,
            "after_score": 1.0,
            "before_issue_count": 0,
            "after_issue_count": 0,
        }
    )
    report = quality.quality_report.model_copy(
        update={
            "passed_gate_count": quality.quality_report.gate_count,
            "blocking_failure_count": 0,
            "quality_score": 1.0,
            "quality_gate_passed": True,
            "formal_gold_eligible": True,
            "comparison": comparison,
            "quality_report_hash": "f" * 64,
        }
    )
    formal = _formal_gold_dataset(quality_request, report, "1.0.0")
    contract = quality_request.fusion_request.entity_request.normalization_request.mapping_request.extraction_request.contract
    exported = TabularExporter().export(formal, contract)
    csv_frame = pl.read_csv(BytesIO(exported.csv_bytes))
    parquet_frame = pl.read_parquet(BytesIO(exported.parquet_bytes))
    assert exported.row_count == len(formal.records) == 1
    assert csv_frame.rows() == parquet_frame.rows()
    selected = {field.field_name: field.value for field in formal.records[0].fields}
    row = csv_frame.row(0, named=True)
    assert all(row[name] == value for name, value in selected.items())
    provenance, count = ProvenanceExporter().build(formal)
    assert count == sum(len(record.fields) for record in formal.records)
    assert all(item["evidence_ids"] for item in map(json.loads, provenance.splitlines()))


def test_delivery_contract_and_integrity_fail_closed(
    delivery_chain: tuple[DeliveryRequest, DeliveryResult, BronzeByteStore, DeliveryOrchestrator],
) -> None:
    request, result, bronze_store, service = delivery_chain
    payload = request.model_dump(mode="python")
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        DeliveryRequest.model_validate(payload)
    with pytest.raises(AppError) as captured:
        verify_delivery_result(
            result.model_copy(update={"output_hash": "f" * 64}),
            request,
            bronze_store,
            service.delivery_store,
        )
    assert captured.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_runtime_rejects_predating_m19(
    delivery_chain: tuple[DeliveryRequest, DeliveryResult, BronzeByteStore, DeliveryOrchestrator],
) -> None:
    request, _, _, _ = delivery_chain
    with pytest.raises(ValueError, match="cannot predate"):
        build_offline_delivery_bundle(
            not_before=request.knowledge_result.created_at,
            clock=lambda: request.knowledge_result.created_at.replace(year=2000),
        )


def test_download_tickets_are_content_bound_and_expire(
    delivery_chain: tuple[DeliveryRequest, DeliveryResult, BronzeByteStore, DeliveryOrchestrator],
) -> None:
    _, result, _, _ = delivery_chain
    now = [1_000.0]
    with pytest.raises(AppError, match="at least 32"):
        DownloadTicketSigner(b"short")
    with pytest.raises(AppError, match="lifetime"):
        DownloadTicketSigner(b"x" * 32, lifetime_seconds=9)
    signer = DownloadTicketSigner(b"x" * 32, lifetime_seconds=10, clock=lambda: now[0])
    token, expires_at = signer.issue(result.package)
    signer.verify(result.package, token, expires_at)
    now[0] = 1_011.0
    with pytest.raises(AppError) as expired:
        signer.verify(result.package, token, expires_at)
    assert expired.value.code is ErrorCode.SECURITY_POLICY_VIOLATION


def test_delivery_storage_rejects_limits_bad_keys_and_corruption() -> None:
    with pytest.raises(AppError, match="store size"):
        MemoryDeliveryStore(maximum_bytes=1)
    store = MemoryDeliveryStore(maximum_bytes=1_024)
    with pytest.raises(AppError, match="exceeds"):
        store.put(b"x" * 1_025)
    with pytest.raises(AppError, match="SHA-256"):
        store.get("bad")
    digest = store.put(b"expected")
    store._values[digest] = b"corrupt"
    with pytest.raises(AppError, match="corrupt"):
        store.get(digest)


def test_packager_rejects_collisions_size_limits_and_corrupt_archives() -> None:
    packager = ReproducibilityPackager()
    with pytest.raises(AppError, match="collision"):
        packager.build({"manifest.json": b"collision"}, b"manifest", 10_000)
    with pytest.raises(AppError, match="too large"):
        packager.build({"file.json": b"content"}, b"manifest", 1)
    with pytest.raises(AppError, match="unsafe"):
        packager.build({"/absolute.json": b"content"}, b"manifest", 10_000)
    with pytest.raises(AppError, match="invalid M20 ZIP"):
        packager.verify(b"not-a-zip", {})
    payload = packager.build({"file.json": b"content"}, b"manifest", 10_000)
    with pytest.raises(AppError, match="bytes changed"):
        packager.verify(payload, {"file.json": b"different", "manifest.json": b"manifest"})
    with pytest.raises(AppError, match="invalid M20 notebook"):
        parse_notebook(b'{"nbformat": 3}')
    assert parse_notebook(NotebookGenerator().build())["nbformat"] == 4


def test_checkpoint_rejects_invalid_limits_corruption_oversize_and_conflicts(
    delivery_chain: tuple[DeliveryRequest, DeliveryResult, BronzeByteStore, DeliveryOrchestrator],
) -> None:
    _, result, _, _ = delivery_chain
    with pytest.raises(AppError, match="checkpoint size"):
        MemoryDeliveryCheckpointStore(maximum_bytes=1)
    assert MemoryDeliveryCheckpointStore().load(result.idempotency_key) is None
    too_small = MemoryDeliveryCheckpointStore(maximum_bytes=1_024)
    with pytest.raises(AppError, match="exceeds"):
        too_small.save(result)
    corrupted = MemoryDeliveryCheckpointStore()
    corrupted._values[result.idempotency_key] = b"{}"
    with pytest.raises(AppError, match="strict validation"):
        corrupted.load(result.idempotency_key)
    conflict = MemoryDeliveryCheckpointStore()
    conflict.save(result)
    changed = result.model_copy(update={"output_hash": "f" * 64})
    with pytest.raises(AppError, match="different checkpoint"):
        conflict.save(changed)


def test_delivery_helpers_and_missing_artifact_fail_closed(
    delivery_chain: tuple[DeliveryRequest, DeliveryResult, BronzeByteStore, DeliveryOrchestrator],
) -> None:
    request, result, bronze_store, _ = delivery_chain
    assert _kind_for("gold.csv") is DeliveryArtifactKind.CSV
    assert _kind_for("gold.parquet") is DeliveryArtifactKind.PARQUET
    assert _media_type_for("data.bin") == "application/octet-stream"
    assert _media_type_for("gold.parquet") == "application/vnd.apache.parquet"
    with pytest.raises(AppError, match="unsupported"):
        _kind_for("unknown.bin")
    with pytest.raises(AppError, match="missing"):
        verify_delivery_result(
            result,
            request,
            bronze_store,
            MemoryDeliveryStore(),
        )


def test_request_result_and_artifact_tampering_each_fail_at_their_boundary(
    delivery_chain: tuple[DeliveryRequest, DeliveryResult, BronzeByteStore, DeliveryOrchestrator],
) -> None:
    request, result, bronze_store, _ = delivery_chain
    bad_rule = request.runtime.rule.model_copy(update={"rule_hash": "f" * 64})
    with pytest.raises(AppError, match="rule hash"):
        verify_delivery_request(
            request.model_copy(
                update={"runtime": request.runtime.model_copy(update={"rule": bad_rule})}
            ),
            bronze_store,
        )
    with pytest.raises(AppError, match="runtime hash"):
        verify_delivery_request(
            request.model_copy(
                update={"runtime": request.runtime.model_copy(update={"runtime_hash": "f" * 64})}
            ),
            bronze_store,
        )
    with pytest.raises(AppError, match="immutable request"):
        verify_delivery_result(
            result.model_copy(update={"run_id": "run_" + "f" * 32}),
            request,
            bronze_store,
            _AlwaysWrongDeliveryStore(),
        )
    with pytest.raises(AppError, match="identity or bytes"):
        verify_delivery_result(
            result,
            request,
            bronze_store,
            _AlwaysWrongDeliveryStore(),
        )


class _AlwaysWrongDeliveryStore:
    def put(self, payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    def get(self, sha256: str) -> bytes | None:
        return b"wrong"

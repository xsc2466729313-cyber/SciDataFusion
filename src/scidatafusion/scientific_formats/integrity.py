"""Canonical identities and replay verification for M12."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import NoReturn

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.base import StrictContract
from scidatafusion.contracts.datasets import (
    CoordinateIR,
    DatasetIR,
    DatasetIRRef,
    FormatMetadata,
    ScientificArtifact,
    ScientificParserDescriptor,
    ScientificParsingRequest,
    ScientificParsingResult,
    ScientificQualityReport,
    ScientificRuntimeSnapshot,
    ScientificScalar,
    VariableIR,
)
from scidatafusion.contracts.events import EventType
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode


def calculate_scientific_artifact_hash(value: ScientificArtifact) -> str:
    return _artifact_hash(value, {"artifact_hash"})


def calculate_scientific_descriptor_hash(value: ScientificParserDescriptor) -> str:
    return _artifact_hash(value, {"descriptor_hash"})


def calculate_scientific_runtime_hash(value: ScientificRuntimeSnapshot) -> str:
    return _artifact_hash(value, {"runtime_hash", "checked_at"})


def calculate_scientific_policy_hash(request: ScientificParsingRequest) -> str:
    return canonical_hash(request.policy.model_dump(mode="json"))


def calculate_scientific_input_hash(request: ScientificParsingRequest) -> str:
    return canonical_hash(
        request.model_dump(mode="json", exclude={"requested_at", "force_recompute"})
    )


def calculate_scientific_idempotency_key(
    request: ScientificParsingRequest, producer_version: str
) -> str:
    return canonical_hash(
        {
            "task_id": request.artifact.task_id,
            "module_id": "M12",
            "contract_version": request.artifact.contract_version,
            "input_hash": calculate_scientific_input_hash(request),
            "producer_version": producer_version,
        }
    )


def calculate_scalar_hash(value: ScientificScalar) -> str:
    return _artifact_hash(value, {"scalar_hash"})


def calculate_coordinate_hash(value: CoordinateIR) -> str:
    return _artifact_hash(value, {"coordinate_id", "coordinate_hash"})


def calculate_variable_hash(value: VariableIR) -> str:
    return _artifact_hash(value, {"variable_id", "variable_hash"})


def calculate_format_metadata_hash(value: FormatMetadata) -> str:
    return _artifact_hash(value, {"metadata_id", "metadata_hash"})


def calculate_dataset_hash(value: DatasetIR) -> str:
    return _artifact_hash(value, {"dataset_id", "dataset_hash", "created_at"})


def calculate_quality_hash(value: ScientificQualityReport) -> str:
    return _artifact_hash(value, {"report_hash"})


def calculate_scientific_output_hash(value: ScientificParsingResult) -> str:
    return canonical_hash(
        value.model_dump(
            mode="json", exclude={"output_hash": True, "event": {"payload": {"output_hash"}}}
        )
    )


def calculate_scientific_event_id(idempotency_key: str) -> str:
    return f"evt_{canonical_hash({'key': idempotency_key, 'type': 'dataset.parsed'})[:32]}"


def serialize_dataset_ir(dataset: DatasetIR) -> bytes:
    return json.dumps(
        dataset.model_dump(mode="json"),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def build_dataset_ref(dataset: DatasetIR) -> DatasetIRRef:
    payload = serialize_dataset_ir(dataset)
    digest = hashlib.sha256(payload).hexdigest()
    return DatasetIRRef(
        dataset_id=dataset.dataset_id,
        dataset_hash=dataset.dataset_hash,
        artifact_sha256=digest,
        uri=f"silver://dataset-ir/sha256/{digest}",
        size_bytes=len(payload),
        object_id=dataset.artifact.object_id,
        route_id=dataset.artifact.route_id,
        variable_count=len(dataset.variables),
        row_count=len(dataset.coordinates[0].values),
    )


def verify_scientific_request(request: ScientificParsingRequest, store: BronzeByteStore) -> None:
    artifact = request.artifact
    content = store.read(artifact.byte_sha256)
    if not (
        len(content) == artifact.size_bytes
        and len(content) <= request.policy.max_input_bytes
        and hashlib.sha256(content).hexdigest() == artifact.byte_sha256
        and hmac.compare_digest(
            artifact.artifact_hash, calculate_scientific_artifact_hash(artifact)
        )
        and hmac.compare_digest(
            request.runtime.parser.descriptor_hash,
            calculate_scientific_descriptor_hash(request.runtime.parser),
        )
        and hmac.compare_digest(
            request.runtime.runtime_hash, calculate_scientific_runtime_hash(request.runtime)
        )
        and artifact.capability_registry_hash == request.runtime.capability_registry_hash
    ):
        _integrity_error("M12 request source, route, or runtime integrity is invalid")


def verify_dataset_ir(dataset: DatasetIR) -> None:
    for coordinate in dataset.coordinates:
        expected = calculate_coordinate_hash(coordinate)
        if (
            coordinate.coordinate_id != f"cor_{expected[:32]}"
            or coordinate.coordinate_hash != expected
        ):
            _integrity_error("M12 coordinate identity is invalid")
    for variable in dataset.variables:
        for scalar in variable.values:
            if scalar.scalar_hash != calculate_scalar_hash(scalar):
                _integrity_error("M12 scalar identity is invalid")
        expected = calculate_variable_hash(variable)
        if variable.variable_id != f"var_{expected[:32]}" or variable.variable_hash != expected:
            _integrity_error("M12 variable identity is invalid")
    metadata_hash = calculate_format_metadata_hash(dataset.format_metadata)
    if not (
        dataset.format_metadata.metadata_id == f"fmt_{metadata_hash[:32]}"
        and dataset.format_metadata.metadata_hash == metadata_hash
    ):
        _integrity_error("M12 format metadata identity is invalid")
    dataset_hash = calculate_dataset_hash(dataset)
    if dataset.dataset_id != f"dsr_{dataset_hash[:32]}" or dataset.dataset_hash != dataset_hash:
        _integrity_error("M12 DatasetIR identity is invalid")


def verify_scientific_result_hashes(result: ScientificParsingResult) -> None:
    if not (
        result.policy_hash == canonical_hash(result.policy.model_dump(mode="json"))
        and result.quality.report_hash == calculate_quality_hash(result.quality)
        and result.output_hash == calculate_scientific_output_hash(result)
        and result.event.event_id == calculate_scientific_event_id(result.idempotency_key)
        and result.event.event_type is EventType.DATASET_PARSED
        and result.event.causation_event_id is None
    ):
        _integrity_error("M12 result aggregate hash or event identity is invalid")


def verify_scientific_result(
    result: ScientificParsingResult,
    request: ScientificParsingRequest,
    dataset: DatasetIR,
    store: BronzeByteStore,
) -> None:
    verify_scientific_request(request, store)
    verify_dataset_ir(dataset)
    verify_scientific_result_hashes(result)
    if not (
        result.task_id == request.artifact.task_id
        and result.run_id == request.artifact.run_id
        and result.contract_version == request.artifact.contract_version
        and result.contract_id == request.artifact.contract_id
        and result.policy == request.policy
        and result.policy_hash == calculate_scientific_policy_hash(request)
        and result.runtime == request.runtime
        and result.input_hash == calculate_scientific_input_hash(request)
        and result.idempotency_key
        == calculate_scientific_idempotency_key(request, result.producer_version)
        and result.dataset_ref == build_dataset_ref(dataset)
        and dataset.artifact == request.artifact
    ):
        _integrity_error("M12 result does not match its immutable request and DatasetIR")


def _artifact_hash(value: StrictContract, excluded: set[str]) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude=excluded))


def _integrity_error(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)

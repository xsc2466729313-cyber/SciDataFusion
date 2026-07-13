"""Strict M20 delivery, export, and reproduction contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from scidatafusion.contracts.base import (
    ContentHash,
    NonEmptyStr,
    RunId,
    SemanticVersion,
    StrictContract,
    TaskId,
)
from scidatafusion.contracts.events import EventEnvelope
from scidatafusion.contracts.knowledge import KnowledgeRequest, KnowledgeResult
from scidatafusion.contracts.scientific import ContractId

DeliveryArtifactId = Annotated[str, StringConstraints(pattern=r"^dlf_[0-9a-f]{32}$")]
DeliveryManifestId = Annotated[str, StringConstraints(pattern=r"^dmf_[0-9a-f]{32}$")]
SafeFilename = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.-]{0,127}$"),
]


class DeliveryStatus(StrEnum):
    SUCCEEDED = "succeeded"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"


class DeliveryExecutionMode(StrEnum):
    OFFLINE = "offline"


class DeliveryArtifactKind(StrEnum):
    CSV = "csv"
    PARQUET = "parquet"
    DATA_DICTIONARY = "data_dictionary"
    PROVENANCE = "provenance"
    QUALITY_REPORT = "quality_report"
    EVIDENCE_GRAPH = "evidence_graph"
    NOTEBOOK = "notebook"
    RUN_METRICS = "run_metrics"
    REPRODUCTION = "reproduction"
    PACKAGE_MANIFEST = "package_manifest"
    REPRODUCTION_PACKAGE = "reproduction_package"


class DeliveryPolicy(StrictContract):
    policy_version: SemanticVersion = "1.0.0"
    include_csv: bool = True
    include_parquet: bool = True
    include_notebook: bool = True
    include_evidence_graph: bool = True
    maximum_package_bytes: int = Field(default=100_000_000, ge=1_024, le=1_000_000_000)
    require_formal_gold_for_tabular: Literal[True] = True
    allow_unresolved_quality_export: Literal[False] = False
    allow_external_network: Literal[False] = False


class DeliveryRuleDescriptor(StrictContract):
    rule_id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")]
    rule_version: SemanticVersion
    rule_hash: ContentHash


class DeliveryRuntimeSnapshot(StrictContract):
    execution_mode: Literal[DeliveryExecutionMode.OFFLINE]
    rule: DeliveryRuleDescriptor
    code_revision: NonEmptyStr
    parser_version: SemanticVersion
    model_execution_enabled: Literal[False] = False
    external_network_enabled: Literal[False] = False
    checked_at: datetime
    runtime_hash: ContentHash

    @field_validator("checked_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M20 runtime timestamp must include a timezone")
        return value.astimezone(UTC)


class DeliveryRequest(StrictContract):
    knowledge_request: KnowledgeRequest
    knowledge_result: KnowledgeResult
    policy: DeliveryPolicy
    runtime: DeliveryRuntimeSnapshot
    requested_at: datetime
    force_recompute: bool = False

    @field_validator("requested_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M20 request timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.requested_at != self.runtime.checked_at:
            raise ValueError("M20 request must use the immutable runtime timestamp")
        if self.runtime.checked_at < self.knowledge_result.created_at:
            raise ValueError("M20 runtime cannot predate M19")
        return self


class DeliveryArtifact(StrictContract):
    artifact_id: DeliveryArtifactId
    task_id: TaskId
    run_id: RunId
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion
    filename: SafeFilename
    kind: DeliveryArtifactKind
    media_type: NonEmptyStr
    sha256: ContentHash
    size_bytes: int = Field(ge=0)

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M20 artifact timestamp must include a timezone")
        return value.astimezone(UTC)


class DeliveryManifest(StrictContract):
    manifest_id: DeliveryManifestId
    task_id: TaskId
    run_id: RunId
    contract_id: ContractId
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion
    status: DeliveryStatus
    files: tuple[DeliveryArtifact, ...] = Field(min_length=1, max_length=64)
    known_limitations: tuple[NonEmptyStr, ...] = Field(max_length=64)
    manifest_hash: ContentHash

    @field_validator("created_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("M20 manifest timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_files(self) -> Self:
        if len({item.filename for item in self.files}) != len(self.files):
            raise ValueError("M20 filenames must be unique")
        if any(
            item.task_id != self.task_id
            or item.run_id != self.run_id
            or item.contract_version != self.contract_version
            for item in self.files
        ):
            raise ValueError("M20 manifest files must share task, run, and contract version")
        return self


class DeliveryMetrics(StrictContract):
    formal_gold_record_count: int = Field(ge=0)
    artifact_count: int = Field(ge=0)
    package_entry_count: int = Field(ge=0)
    package_size_bytes: int = Field(ge=0)
    provenance_record_count: int = Field(ge=0)
    quality_issue_count: int = Field(ge=0)
    csv_parquet_consistency: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    notebook_validation_passed: bool
    scientific_value_mutation_count: Literal[0] = 0
    model_attempt_count: Literal[0] = 0
    network_attempt_count: Literal[0] = 0
    actual_cost_micro_usd: Literal[0] = 0


class DeliveryCompletedPayload(StrictContract):
    status: DeliveryStatus
    contract_id: ContractId
    upstream_knowledge_output_hash: ContentHash
    manifest_hash: ContentHash
    package_sha256: ContentHash
    artifact_count: int = Field(ge=0)
    formal_gold_record_count: int = Field(ge=0)
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash


class DeliveryResult(StrictContract):
    module_id: Literal["M20"] = "M20"
    task_id: TaskId
    run_id: RunId
    contract_id: ContractId
    contract_version: SemanticVersion
    created_at: datetime
    producer_version: SemanticVersion
    status: DeliveryStatus
    policy: DeliveryPolicy
    policy_hash: ContentHash
    runtime: DeliveryRuntimeSnapshot
    input_hash: ContentHash
    output_hash: ContentHash
    idempotency_key: ContentHash
    manifest: DeliveryManifest
    package: DeliveryArtifact
    warnings: tuple[NonEmptyStr, ...] = Field(max_length=64)
    metrics: DeliveryMetrics
    event: EventEnvelope[DeliveryCompletedPayload]

    @model_validator(mode="after")
    def validate_aggregate(self) -> Self:
        payload = self.event.payload
        if not (
            self.manifest.task_id == self.task_id
            and self.manifest.run_id == self.run_id
            and self.manifest.contract_id == self.contract_id
            and self.package.kind is DeliveryArtifactKind.REPRODUCTION_PACKAGE
            and self.metrics.artifact_count == len(self.manifest.files) + 1
            and self.metrics.package_entry_count == len(self.manifest.files) + 1
            and self.metrics.package_size_bytes == self.package.size_bytes
            and payload.status is self.status
            and payload.contract_id == self.contract_id
            and payload.manifest_hash == self.manifest.manifest_hash
            and payload.package_sha256 == self.package.sha256
            and payload.artifact_count == self.metrics.artifact_count
            and payload.formal_gold_record_count == self.metrics.formal_gold_record_count
            and payload.input_hash == self.input_hash
            and payload.output_hash == self.output_hash
            and payload.idempotency_key == self.idempotency_key
        ):
            raise ValueError("M20 completion event must describe the aggregate result")
        return self

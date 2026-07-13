"""Canonical identities and end-to-end integrity for M16 entity resolution."""

from __future__ import annotations

import hmac
from typing import NoReturn

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.base import StrictContract
from scidatafusion.contracts.entity_resolution import (
    DuplicateGroup,
    DuplicateGroupSet,
    EntityCluster,
    EntityClusterSet,
    EntityResolutionEvidence,
    EntityResolutionEvidenceSet,
    EntityResolutionPolicy,
    EntityResolutionRequest,
    EntityResolutionResult,
    EntityResolutionStatus,
    EntityRuleDescriptor,
    EntityRuntimeSnapshot,
)
from scidatafusion.contracts.events import EventType
from scidatafusion.contracts.normalization import NormalizationStatus
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.entity_resolution.rules import (
    entity_fingerprint,
    entity_key_fields,
    exact_record_fingerprint,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.normalization.integrity import verify_normalization_result


def calculate_entity_policy_hash(value: EntityResolutionPolicy) -> str:
    return canonical_hash(value.model_dump(mode="json"))


def calculate_entity_rule_hash(value: EntityRuleDescriptor) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude={"rule_hash"}))


def calculate_entity_runtime_hash(value: EntityRuntimeSnapshot) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude={"runtime_hash"}))


def calculate_entity_input_hash(request: EntityResolutionRequest) -> str:
    return canonical_hash(
        {
            "contract_hash": request.normalization_result.contract_hash,
            "normalization_input_hash": request.normalization_result.input_hash,
            "normalization_output_hash": request.normalization_result.output_hash,
            "policy_hash": calculate_entity_policy_hash(request.policy),
            "runtime_hash": request.runtime.runtime_hash,
        }
    )


def calculate_entity_idempotency_key(
    request: EntityResolutionRequest, producer_version: str
) -> str:
    return canonical_hash(
        {
            "contract_version": request.normalization_result.contract_version,
            "input_hash": calculate_entity_input_hash(request),
            "module_id": "M16",
            "producer_version": producer_version,
            "task_id": request.normalization_result.task_id,
        }
    )


def _artifact_hash(value: StrictContract, *, excluded: set[str]) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude=excluded))


def calculate_resolution_evidence_hash(value: EntityResolutionEvidence) -> str:
    return _artifact_hash(
        value,
        excluded={"resolution_evidence_id", "resolution_evidence_hash", "created_at"},
    )


def calculate_resolution_evidence_set_hash(value: EntityResolutionEvidenceSet) -> str:
    return _artifact_hash(value, excluded={"evidence_set_id", "evidence_set_hash", "created_at"})


def calculate_entity_cluster_hash(value: EntityCluster) -> str:
    return _artifact_hash(value, excluded={"entity_cluster_id", "cluster_hash", "created_at"})


def calculate_entity_cluster_set_hash(value: EntityClusterSet) -> str:
    return _artifact_hash(value, excluded={"cluster_set_id", "cluster_set_hash", "created_at"})


def calculate_duplicate_group_hash(value: DuplicateGroup) -> str:
    return _artifact_hash(
        value, excluded={"duplicate_group_id", "duplicate_group_hash", "created_at"}
    )


def calculate_duplicate_group_set_hash(value: DuplicateGroupSet) -> str:
    return _artifact_hash(
        value,
        excluded={"duplicate_group_set_id", "duplicate_group_set_hash", "created_at"},
    )


def calculate_entity_output_hash(value: EntityResolutionResult) -> str:
    return canonical_hash(
        value.model_dump(
            mode="json", exclude={"output_hash": True, "event": {"payload": {"output_hash"}}}
        )
    )


def calculate_entity_event_id(key: str) -> str:
    return f"evt_{canonical_hash({'idempotency_key': key, 'type': 'entity.resolved'})[:32]}"


def verify_entity_request(request: EntityResolutionRequest, store: BronzeByteStore) -> None:
    verify_normalization_result(request.normalization_result, request.normalization_request, store)
    if not hmac.compare_digest(
        request.runtime.rule.rule_hash, calculate_entity_rule_hash(request.runtime.rule)
    ):
        _fail("M16 rule hash is invalid")
    if not hmac.compare_digest(
        request.runtime.runtime_hash, calculate_entity_runtime_hash(request.runtime)
    ):
        _fail("M16 runtime hash is invalid")


def verify_entity_result_hashes(result: EntityResolutionResult) -> None:
    groups = (
        (
            (
                item.resolution_evidence_id,
                item.resolution_evidence_hash,
                "ere_",
                calculate_resolution_evidence_hash(item),
            )
            for item in result.resolution_evidence_set.records
        ),
        (
            (
                item.entity_cluster_id,
                item.cluster_hash,
                "ecl_",
                calculate_entity_cluster_hash(item),
            )
            for item in result.cluster_set.clusters
        ),
        (
            (
                item.duplicate_group_id,
                item.duplicate_group_hash,
                "dpg_",
                calculate_duplicate_group_hash(item),
            )
            for item in result.duplicate_group_set.groups
        ),
    )
    for group in groups:
        for identity, stored_hash, prefix, expected in group:
            if not hmac.compare_digest(stored_hash, expected) or identity != prefix + expected[:32]:
                _fail("M16 content-addressed identity is invalid")
    aggregates = (
        (
            result.resolution_evidence_set.evidence_set_id,
            result.resolution_evidence_set.evidence_set_hash,
            "ers_",
            calculate_resolution_evidence_set_hash(result.resolution_evidence_set),
        ),
        (
            result.cluster_set.cluster_set_id,
            result.cluster_set.cluster_set_hash,
            "ecs_",
            calculate_entity_cluster_set_hash(result.cluster_set),
        ),
        (
            result.duplicate_group_set.duplicate_group_set_id,
            result.duplicate_group_set.duplicate_group_set_hash,
            "dgs_",
            calculate_duplicate_group_set_hash(result.duplicate_group_set),
        ),
    )
    for identity, stored_hash, prefix, expected in aggregates:
        if not hmac.compare_digest(stored_hash, expected) or identity != prefix + expected[:32]:
            _fail("M16 aggregate identity is invalid")
    if not (
        result.output_hash == calculate_entity_output_hash(result)
        and result.event.event_id == calculate_entity_event_id(result.idempotency_key)
        and result.event.event_type is EventType.ENTITY_RESOLVED
        and result.event.causation_event_id is not None
    ):
        _fail("M16 output hash or event identity is invalid")


def verify_entity_result(
    result: EntityResolutionResult,
    request: EntityResolutionRequest,
    store: BronzeByteStore,
) -> None:
    verify_entity_request(request, store)
    upstream = request.normalization_result
    if not (
        result.task_id == upstream.task_id
        and result.run_id == upstream.run_id
        and result.contract_id == upstream.contract_id
        and result.contract_hash == upstream.contract_hash
        and result.upstream_normalization_input_hash == upstream.input_hash
        and result.upstream_normalization_output_hash == upstream.output_hash
        and result.policy == request.policy
        and result.policy_hash == calculate_entity_policy_hash(request.policy)
        and result.runtime == request.runtime
        and result.input_hash == calculate_entity_input_hash(request)
        and result.idempotency_key
        == calculate_entity_idempotency_key(request, result.producer_version)
        and result.event.causation_event_id == upstream.event.event_id
    ):
        _fail("M16 result does not match its immutable request")
    verify_entity_result_hashes(result)
    records = {item.normalized_record_id: item for item in upstream.record_set.records}
    keys = request.normalization_request.mapping_request.extraction_request.contract.entity_keys
    evidence_by_record = {
        item.normalized_record_id: item for item in result.resolution_evidence_set.records
    }
    resolved_ids = {
        record_id
        for cluster in result.cluster_set.clusters
        for record_id in cluster.member_record_ids
    }
    if resolved_ids | set(result.unresolved_record_ids) != set(records):
        _fail("M16 must account for every normalized record exactly once")
    for record_id, evidence in evidence_by_record.items():
        record = records.get(record_id)
        selected = entity_key_fields(record, keys) if record is not None else None
        fields = {item.normalized_field_id: item for item in record.fields} if record else {}
        expected_evidence_ids = (
            tuple(
                dict.fromkeys(
                    evidence_id
                    for _, field_id, _ in selected
                    for evidence_id in (
                        *fields[field_id].evidence_ids,
                        *fields[field_id].entity_evidence_ids,
                    )
                )
            )
            if selected is not None
            else ()
        )
        if (
            record is None
            or selected is None
            or not (
                evidence.normalized_record_hash == record.record_hash
                and evidence.entity_key_fields == tuple(item[0] for item in selected)
                and evidence.entity_key_field_ids == tuple(item[1] for item in selected)
                and evidence.entity_key_value_hashes == tuple(item[2] for item in selected)
                and evidence.entity_key_fingerprint == entity_fingerprint(selected)
                and evidence.evidence_ids == expected_evidence_ids
            )
        ):
            _fail("M16 resolution evidence does not replay to eligible entity keys")
    clusters = {item.entity_cluster_id: item for item in result.cluster_set.clusters}
    for cluster in clusters.values():
        member_records = tuple(records.get(item) for item in cluster.member_record_ids)
        member_evidence = tuple(evidence_by_record.get(item) for item in cluster.member_record_ids)
        if any(item is None for item in (*member_records, *member_evidence)) or not (
            cluster.member_record_hashes
            == tuple(item.record_hash for item in member_records if item is not None)
            and cluster.resolution_evidence_ids
            == tuple(item.resolution_evidence_id for item in member_evidence if item is not None)
            and all(
                item is not None and item.entity_key_fingerprint == cluster.entity_key_fingerprint
                for item in member_evidence
            )
        ):
            _fail("M16 cluster does not replay to exact records and resolution evidence")
    for group in result.duplicate_group_set.groups:
        resolved_cluster = clusters.get(group.entity_cluster_id)
        if resolved_cluster is None or not set(group.member_record_ids) <= set(
            resolved_cluster.member_record_ids
        ):
            _fail("M16 duplicate group is outside its entity cluster")
        member_records = tuple(records.get(item) for item in group.member_record_ids)
        if any(item is None for item in member_records) or any(
            exact_record_fingerprint(item) != group.exact_record_fingerprint
            for item in member_records
            if item is not None
        ):
            _fail("M16 duplicate group does not replay to exact record fingerprints")
    resolved_count = len(resolved_ids)
    expected_status = (
        EntityResolutionStatus.UNSUPPORTED
        if not records
        else EntityResolutionStatus.NEEDS_REVIEW
        if not resolved_count
        else EntityResolutionStatus.PARTIAL
        if result.unresolved_record_ids or upstream.status is not NormalizationStatus.SUCCEEDED
        else EntityResolutionStatus.SUCCEEDED
    )
    expected_warnings = tuple(
        [
            f"upstream_normalization_status:{upstream.status.value}"
            if upstream.status is not NormalizationStatus.SUCCEEDED
            else ""
        ]
        + [f"unresolved_record:{item}" for item in result.unresolved_record_ids]
    )
    if result.status is not expected_status or result.warnings != tuple(
        item for item in expected_warnings if item
    ):
        _fail("M16 status or warnings do not derive from verified inputs")


def _fail(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)

"""Canonical identities and end-to-end integrity for M15 normalization."""

from __future__ import annotations

import hmac
from typing import NoReturn

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.base import StrictContract
from scidatafusion.contracts.events import EventType
from scidatafusion.contracts.normalization import (
    NormalizationIssue,
    NormalizationIssueSet,
    NormalizationPolicy,
    NormalizationRequest,
    NormalizationResult,
    NormalizationRuleDescriptor,
    NormalizationRuntimeSnapshot,
    NormalizedField,
    NormalizedRecord,
    NormalizedRecordSet,
    TransformationRecord,
    TransformationRecordSet,
)
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.mapping.integrity import verify_mapping_result


def calculate_normalization_policy_hash(value: NormalizationPolicy) -> str:
    return canonical_hash(value.model_dump(mode="json"))


def calculate_normalization_rule_hash(value: NormalizationRuleDescriptor) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude={"rule_hash"}))


def calculate_normalization_runtime_hash(value: NormalizationRuntimeSnapshot) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude={"runtime_hash"}))


def calculate_normalization_input_hash(request: NormalizationRequest) -> str:
    return canonical_hash(
        {
            "contract_hash": request.mapping_result.contract_hash,
            "mapping_input_hash": request.mapping_result.input_hash,
            "mapping_output_hash": request.mapping_result.output_hash,
            "policy_hash": calculate_normalization_policy_hash(request.policy),
            "runtime_hash": request.runtime.runtime_hash,
            "context_evidence_enabled": request.context_evidence_enabled,
            "jd_to_mjd_conversion_enabled": request.jd_to_mjd_conversion_enabled,
        }
    )


def calculate_normalization_idempotency_key(
    request: NormalizationRequest, producer_version: str
) -> str:
    return canonical_hash(
        {
            "contract_version": request.mapping_result.contract_version,
            "input_hash": calculate_normalization_input_hash(request),
            "module_id": "M15",
            "producer_version": producer_version,
            "task_id": request.mapping_result.task_id,
        }
    )


def _artifact_hash(value: StrictContract, *, excluded: set[str]) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude=excluded))


def calculate_transformation_hash(value: TransformationRecord) -> str:
    return _artifact_hash(
        value, excluded={"transformation_id", "transformation_hash", "created_at"}
    )


def calculate_issue_hash(value: NormalizationIssue) -> str:
    return _artifact_hash(value, excluded={"issue_id", "issue_hash", "created_at"})


def calculate_normalized_field_hash(value: NormalizedField) -> str:
    return _artifact_hash(
        value, excluded={"normalized_field_id", "normalized_field_hash", "created_at"}
    )


def calculate_record_hash(value: NormalizedRecord) -> str:
    return _artifact_hash(value, excluded={"normalized_record_id", "record_hash", "created_at"})


def calculate_record_set_hash(value: NormalizedRecordSet) -> str:
    return _artifact_hash(value, excluded={"record_set_id", "record_set_hash", "created_at"})


def calculate_transformation_set_hash(value: TransformationRecordSet) -> str:
    return _artifact_hash(
        value, excluded={"transformation_set_id", "transformation_set_hash", "created_at"}
    )


def calculate_issue_set_hash(value: NormalizationIssueSet) -> str:
    return _artifact_hash(value, excluded={"issue_set_id", "issue_set_hash", "created_at"})


def calculate_normalization_output_hash(value: NormalizationResult) -> str:
    return canonical_hash(
        value.model_dump(
            mode="json", exclude={"output_hash": True, "event": {"payload": {"output_hash"}}}
        )
    )


def calculate_normalization_event_id(key: str) -> str:
    return f"evt_{canonical_hash({'idempotency_key': key, 'type': 'record.normalized'})[:32]}"


def verify_normalization_request(request: NormalizationRequest, store: BronzeByteStore) -> None:
    verify_mapping_result(request.mapping_result, request.mapping_request, store)
    if not hmac.compare_digest(
        request.runtime.rule.rule_hash, calculate_normalization_rule_hash(request.runtime.rule)
    ):
        _fail("M15 rule hash is invalid")
    if not hmac.compare_digest(
        request.runtime.runtime_hash, calculate_normalization_runtime_hash(request.runtime)
    ):
        _fail("M15 runtime hash is invalid")


def verify_normalization_result_hashes(result: NormalizationResult) -> None:
    pairs = (
        (
            (
                item.transformation_id,
                item.transformation_hash,
                "trn_",
                calculate_transformation_hash(item),
            )
            for item in result.transformation_set.records
        ),
        (
            (item.issue_id, item.issue_hash, "nis_", calculate_issue_hash(item))
            for item in result.issue_set.issues
        ),
        (
            (
                item.normalized_field_id,
                item.normalized_field_hash,
                "nfd_",
                calculate_normalized_field_hash(item),
            )
            for record in result.record_set.records
            for item in record.fields
        ),
        (
            (item.normalized_record_id, item.record_hash, "nrc_", calculate_record_hash(item))
            for item in result.record_set.records
        ),
    )
    for group in pairs:
        for identity, stored_hash, prefix, expected in group:
            if not hmac.compare_digest(stored_hash, expected) or identity != prefix + expected[:32]:
                _fail("M15 content-addressed identity is invalid")
    aggregates = (
        (
            result.record_set.record_set_id,
            result.record_set.record_set_hash,
            "nrs_",
            calculate_record_set_hash(result.record_set),
        ),
        (
            result.transformation_set.transformation_set_id,
            result.transformation_set.transformation_set_hash,
            "trs_",
            calculate_transformation_set_hash(result.transformation_set),
        ),
        (
            result.issue_set.issue_set_id,
            result.issue_set.issue_set_hash,
            "nss_",
            calculate_issue_set_hash(result.issue_set),
        ),
    )
    for identity, stored_hash, prefix, expected in aggregates:
        if not hmac.compare_digest(stored_hash, expected) or identity != prefix + expected[:32]:
            _fail("M15 aggregate identity is invalid")
    if not (
        result.output_hash == calculate_normalization_output_hash(result)
        and result.event.event_id == calculate_normalization_event_id(result.idempotency_key)
        and result.event.event_type is EventType.RECORD_NORMALIZED
        and result.event.causation_event_id is not None
    ):
        _fail("M15 output hash or event identity is invalid")


def verify_normalization_result(
    result: NormalizationResult, request: NormalizationRequest, store: BronzeByteStore
) -> None:
    verify_normalization_request(request, store)
    mapping = request.mapping_result
    if not (
        result.task_id == mapping.task_id
        and result.run_id == mapping.run_id
        and result.contract_id == mapping.contract_id
        and result.contract_hash == mapping.contract_hash
        and result.upstream_mapping_input_hash == mapping.input_hash
        and result.upstream_mapping_output_hash == mapping.output_hash
        and result.policy == request.policy
        and result.policy_hash == calculate_normalization_policy_hash(request.policy)
        and result.runtime == request.runtime
        and result.input_hash == calculate_normalization_input_hash(request)
        and result.idempotency_key
        == calculate_normalization_idempotency_key(request, result.producer_version)
        and result.event.causation_event_id == mapping.event.event_id
    ):
        _fail("M15 result does not match its immutable request")
    verify_normalization_result_hashes(result)
    input_mappings = {item.mapping_id: item for item in mapping.mapping_set.mappings}
    candidates = {
        item.candidate_id: item
        for item in request.mapping_request.extraction_result.candidate_set.candidates
    }
    fields = tuple(field for record in result.record_set.records for field in record.fields)
    if len(fields) != len(input_mappings):
        _fail("M15 must retain every input mapping")
    for field in fields:
        source_mapping = input_mappings.get(field.mapping_id)
        candidate = candidates.get(field.source_candidate_id)
        if (
            source_mapping is None
            or candidate is None
            or not (
                field.mapping_hash == source_mapping.mapping_hash
                and field.source_candidate_id == source_mapping.source_candidate_id
                and field.source_candidate_hash == candidate.candidate_hash
                and field.field_name == source_mapping.target_field_name
                and field.raw_value == candidate.raw_value
                and field.raw_value_sha256 == candidate.raw_value_sha256
                and field.evidence_ids == candidate.evidence_ids
                and field.entity_evidence_ids == candidate.entity_evidence_ids
            )
        ):
            _fail("M15 field does not exactly derive from M13/M14 lineage")
        if any(
            evidence_id
            not in {
                item.evidence_id
                for item in request.mapping_request.extraction_result.evidence_set.atoms
            }
            for evidence_id in field.context_evidence_ids
        ):
            _fail("M15 context evidence must resolve to an upstream EvidenceAtom")


def _fail(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)

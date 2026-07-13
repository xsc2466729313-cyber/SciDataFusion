"""Canonical identities and end-to-end integrity for M17 fusion."""

from __future__ import annotations

import hmac
from typing import NoReturn

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.base import StrictContract
from scidatafusion.contracts.events import EventType
from scidatafusion.contracts.fusion import (
    Conflict,
    ConflictSet,
    FusedField,
    FusedRecord,
    FusedRecordSet,
    FusionCandidate,
    FusionCandidateSet,
    FusionPolicy,
    FusionRequest,
    FusionResult,
    FusionRuleDescriptor,
    FusionRuntimeSnapshot,
    FusionStatus,
    GoldCandidateDataset,
    GoldRecordCandidate,
    ResolutionDecision,
    ResolutionDecisionSet,
)
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.entity_resolution.integrity import verify_entity_result
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.fusion.rules import candidate_comparison_hash, decide_candidates


def calculate_fusion_policy_hash(value: FusionPolicy) -> str:
    return canonical_hash(value.model_dump(mode="json"))


def calculate_fusion_rule_hash(value: FusionRuleDescriptor) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude={"rule_hash"}))


def calculate_fusion_runtime_hash(value: FusionRuntimeSnapshot) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude={"runtime_hash"}))


def calculate_fusion_input_hash(request: FusionRequest) -> str:
    return canonical_hash(
        {
            "contract_hash": request.entity_result.contract_hash,
            "entity_input_hash": request.entity_result.input_hash,
            "entity_output_hash": request.entity_result.output_hash,
            "policy_hash": calculate_fusion_policy_hash(request.policy),
            "runtime_hash": request.runtime.runtime_hash,
        }
    )


def calculate_fusion_idempotency_key(request: FusionRequest, producer_version: str) -> str:
    return canonical_hash(
        {
            "contract_version": request.entity_result.contract_version,
            "input_hash": calculate_fusion_input_hash(request),
            "module_id": "M17",
            "producer_version": producer_version,
            "task_id": request.entity_result.task_id,
        }
    )


def _artifact_hash(value: StrictContract, *, excluded: set[str]) -> str:
    return canonical_hash(value.model_dump(mode="json", exclude=excluded))


def calculate_fusion_candidate_hash(value: FusionCandidate) -> str:
    return _artifact_hash(value, excluded={"fusion_candidate_id", "candidate_hash", "created_at"})


def calculate_fusion_candidate_set_hash(value: FusionCandidateSet) -> str:
    return _artifact_hash(value, excluded={"candidate_set_id", "candidate_set_hash", "created_at"})


def calculate_conflict_hash(value: Conflict) -> str:
    return _artifact_hash(value, excluded={"conflict_id", "conflict_hash", "created_at"})


def calculate_conflict_set_hash(value: ConflictSet) -> str:
    return _artifact_hash(value, excluded={"conflict_set_id", "conflict_set_hash", "created_at"})


def calculate_resolution_decision_hash(value: ResolutionDecision) -> str:
    return _artifact_hash(value, excluded={"decision_id", "decision_hash", "created_at"})


def calculate_resolution_decision_set_hash(value: ResolutionDecisionSet) -> str:
    return _artifact_hash(value, excluded={"decision_set_id", "decision_set_hash", "created_at"})


def calculate_fused_field_hash(value: FusedField) -> str:
    return _artifact_hash(value, excluded={"fused_field_id", "fused_field_hash", "created_at"})


def calculate_fused_record_hash(value: FusedRecord) -> str:
    return _artifact_hash(value, excluded={"fused_record_id", "fused_record_hash", "created_at"})


def calculate_fused_record_set_hash(value: FusedRecordSet) -> str:
    return _artifact_hash(
        value, excluded={"fused_record_set_id", "fused_record_set_hash", "created_at"}
    )


def calculate_gold_record_hash(value: GoldRecordCandidate) -> str:
    return _artifact_hash(value, excluded={"gold_record_id", "gold_record_hash", "created_at"})


def calculate_gold_dataset_hash(value: GoldCandidateDataset) -> str:
    return _artifact_hash(value, excluded={"dataset_id", "dataset_hash", "created_at"})


def calculate_fusion_output_hash(value: FusionResult) -> str:
    return canonical_hash(
        value.model_dump(
            mode="json", exclude={"output_hash": True, "event": {"payload": {"output_hash"}}}
        )
    )


def calculate_fusion_event_id(key: str) -> str:
    return f"evt_{canonical_hash({'idempotency_key': key, 'type': 'fusion.completed'})[:32]}"


def verify_fusion_request(request: FusionRequest, store: BronzeByteStore) -> None:
    verify_entity_result(request.entity_result, request.entity_request, store)
    if not hmac.compare_digest(
        request.runtime.rule.rule_hash, calculate_fusion_rule_hash(request.runtime.rule)
    ):
        _fail("M17 rule hash is invalid")
    if not hmac.compare_digest(
        request.runtime.runtime_hash, calculate_fusion_runtime_hash(request.runtime)
    ):
        _fail("M17 runtime hash is invalid")


def verify_fusion_result_hashes(result: FusionResult) -> None:
    groups = (
        (
            (
                item.fusion_candidate_id,
                item.candidate_hash,
                "fca_",
                calculate_fusion_candidate_hash(item),
            )
            for item in result.candidate_set.candidates
        ),
        (
            (item.conflict_id, item.conflict_hash, "cfl_", calculate_conflict_hash(item))
            for item in result.conflict_set.conflicts
        ),
        (
            (
                item.decision_id,
                item.decision_hash,
                "fdr_",
                calculate_resolution_decision_hash(item),
            )
            for item in result.decision_set.decisions
        ),
        (
            (
                field.fused_field_id,
                field.fused_field_hash,
                "ffd_",
                calculate_fused_field_hash(field),
            )
            for record in result.fused_record_set.records
            for field in record.fields
        ),
        (
            (
                item.fused_record_id,
                item.fused_record_hash,
                "frc_",
                calculate_fused_record_hash(item),
            )
            for item in result.fused_record_set.records
        ),
        (
            (
                item.gold_record_id,
                item.gold_record_hash,
                "gcr_",
                calculate_gold_record_hash(item),
            )
            for item in result.gold_dataset.records
        ),
    )
    for group in groups:
        for identity, stored_hash, prefix, expected in group:
            if not hmac.compare_digest(stored_hash, expected) or identity != prefix + expected[:32]:
                _fail("M17 content-addressed identity is invalid")
    aggregates = (
        (
            result.candidate_set.candidate_set_id,
            result.candidate_set.candidate_set_hash,
            "fcs_",
            calculate_fusion_candidate_set_hash(result.candidate_set),
        ),
        (
            result.fused_record_set.fused_record_set_id,
            result.fused_record_set.fused_record_set_hash,
            "frs_",
            calculate_fused_record_set_hash(result.fused_record_set),
        ),
        (
            result.conflict_set.conflict_set_id,
            result.conflict_set.conflict_set_hash,
            "cfs_",
            calculate_conflict_set_hash(result.conflict_set),
        ),
        (
            result.decision_set.decision_set_id,
            result.decision_set.decision_set_hash,
            "fds_",
            calculate_resolution_decision_set_hash(result.decision_set),
        ),
        (
            result.gold_dataset.dataset_id,
            result.gold_dataset.dataset_hash,
            "gds_",
            calculate_gold_dataset_hash(result.gold_dataset),
        ),
    )
    for identity, stored_hash, prefix, expected in aggregates:
        if not hmac.compare_digest(stored_hash, expected) or identity != prefix + expected[:32]:
            _fail("M17 aggregate identity is invalid")
    if not (
        result.output_hash == calculate_fusion_output_hash(result)
        and result.event.event_id == calculate_fusion_event_id(result.idempotency_key)
        and result.event.event_type is EventType.FUSION_COMPLETED
        and result.event.causation_event_id is not None
    ):
        _fail("M17 output hash or event identity is invalid")


def verify_fusion_result(
    result: FusionResult, request: FusionRequest, store: BronzeByteStore
) -> None:
    verify_fusion_request(request, store)
    upstream = request.entity_result
    if not (
        result.task_id == upstream.task_id
        and result.run_id == upstream.run_id
        and result.contract_id == upstream.contract_id
        and result.contract_hash == upstream.contract_hash
        and result.upstream_entity_input_hash == upstream.input_hash
        and result.upstream_entity_output_hash == upstream.output_hash
        and result.policy == request.policy
        and result.policy_hash == calculate_fusion_policy_hash(request.policy)
        and result.runtime == request.runtime
        and result.input_hash == calculate_fusion_input_hash(request)
        and result.idempotency_key
        == calculate_fusion_idempotency_key(request, result.producer_version)
        and result.event.causation_event_id == upstream.event.event_id
    ):
        _fail("M17 result does not match its immutable request")
    verify_fusion_result_hashes(result)
    records = {
        item.normalized_record_id: item
        for item in request.entity_request.normalization_result.record_set.records
    }
    clusters = {item.entity_cluster_id: item for item in upstream.cluster_set.clusters}
    expected_fields = {
        (cluster.entity_cluster_id, field.normalized_field_id): field
        for cluster in clusters.values()
        for record_id in cluster.member_record_ids
        for field in records[record_id].fields
    }
    actual_fields = {
        (item.entity_cluster_id, item.normalized_field_id): item
        for item in result.candidate_set.candidates
    }
    if set(actual_fields) != set(expected_fields):
        _fail("M17 must retain every field candidate from every resolved record")
    for key, candidate in actual_fields.items():
        field = expected_fields[key]
        if not (
            candidate.normalized_record_id
            in clusters[candidate.entity_cluster_id].member_record_ids
            and candidate.normalized_field_hash == field.normalized_field_hash
            and candidate.field_name == field.field_name
            and candidate.raw_value == field.raw_value
            and candidate.raw_value_sha256 == field.raw_value_sha256
            and candidate.normalized_value == field.normalized_value
            and candidate.normalized_value_sha256 == field.normalized_value_sha256
            and candidate.evidence_ids == field.evidence_ids
            and candidate.upstream_issue_count == len(field.issue_ids)
            and candidate.eligible_for_gold == field.eligible_for_m16
        ):
            _fail("M17 candidate does not replay to its immutable normalized field")
    candidates = {item.fusion_candidate_id: item for item in result.candidate_set.candidates}
    for decision in result.decision_set.decisions:
        items = tuple(candidates[item] for item in decision.candidate_ids)
        expected_decision, selected, conflicted = decide_candidates(items)
        if not (
            decision.decision is expected_decision
            and decision.selected_candidate_id
            == (selected.fusion_candidate_id if selected is not None else None)
            and (decision.conflict_id is not None) is conflicted
        ):
            _fail("M17 decision does not replay to the conservative rule")
        if conflicted:
            conflict = next(
                (
                    item
                    for item in result.conflict_set.conflicts
                    if item.conflict_id == decision.conflict_id
                ),
                None,
            )
            if conflict is None or conflict.candidate_value_hashes != tuple(
                candidate_comparison_hash(item) for item in items
            ):
                _fail("M17 conflict does not preserve every distinct candidate value")
    selected_count = result.metrics.selected_field_count
    expected_status = (
        FusionStatus.UNSUPPORTED
        if not clusters
        else FusionStatus.NEEDS_REVIEW
        if not selected_count
        else FusionStatus.PARTIAL
        if result.metrics.withheld_field_count
        or result.metrics.conflict_count
        or upstream.status.value != "succeeded"
        else FusionStatus.SUCCEEDED
    )
    expected_warnings = tuple(
        item
        for item in (
            f"upstream_entity_status:{upstream.status.value}"
            if upstream.status.value != "succeeded"
            else "",
            f"withheld_field_count:{result.metrics.withheld_field_count}"
            if result.metrics.withheld_field_count
            else "",
            f"unresolved_conflict_count:{result.metrics.conflict_count}"
            if result.metrics.conflict_count
            else "",
        )
        if item
    )
    if result.status is not expected_status or result.warnings != expected_warnings:
        _fail("M17 status or warnings do not derive from verified inputs")


def _fail(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)

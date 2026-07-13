"""Canonical identities and end-to-end integrity for M14 mapping."""

from __future__ import annotations

import hmac
from typing import NoReturn

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.events import EventType
from scidatafusion.contracts.extraction import ExtractionGapCode
from scidatafusion.contracts.mapping import (
    FieldMapping,
    FieldMappingSet,
    MappingEvidence,
    MappingPolicy,
    MappingRequest,
    MappingResult,
    MappingRuleDescriptor,
    MappingRuntimeSnapshot,
    MappingStatus,
    UnmappedField,
    UnmappedFieldSet,
)
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.extraction.integrity import (
    calculate_field_contract_hash,
    verify_extraction_result,
)
from scidatafusion.mapping.rules import is_value_kind_compatible, registered_alias_suggestions


def calculate_mapping_policy_hash(policy: MappingPolicy) -> str:
    return canonical_hash(policy.model_dump(mode="json"))


def calculate_mapping_rule_hash(rule: MappingRuleDescriptor) -> str:
    return canonical_hash(rule.model_dump(mode="json", exclude={"rule_hash"}))


def calculate_mapping_runtime_hash(runtime: MappingRuntimeSnapshot) -> str:
    return canonical_hash(runtime.model_dump(mode="json", exclude={"runtime_hash"}))


def calculate_mapping_input_hash(request: MappingRequest) -> str:
    return canonical_hash(
        {
            "contract_hash": request.extraction_request.contract.contract_hash,
            "m13_input_hash": request.extraction_result.input_hash,
            "m13_output_hash": request.extraction_result.output_hash,
            "policy_hash": calculate_mapping_policy_hash(request.policy),
            "runtime_hash": request.runtime.runtime_hash,
        }
    )


def calculate_mapping_idempotency_key(request: MappingRequest, producer_version: str) -> str:
    return canonical_hash(
        {
            "contract_version": request.extraction_request.contract.version,
            "input_hash": calculate_mapping_input_hash(request),
            "module_id": "M14",
            "producer_version": producer_version,
            "task_id": request.extraction_request.contract.task_id,
        }
    )


def calculate_mapping_evidence_hash(value: MappingEvidence) -> str:
    return canonical_hash(
        value.model_dump(
            mode="json",
            exclude={"mapping_evidence_id", "evidence_hash", "created_at"},
        )
    )


def calculate_field_mapping_hash(value: FieldMapping) -> str:
    return canonical_hash(
        value.model_dump(mode="json", exclude={"mapping_id", "mapping_hash", "created_at"})
    )


def calculate_mapping_set_hash(value: FieldMappingSet) -> str:
    return canonical_hash(
        value.model_dump(
            mode="json",
            exclude={"mapping_set_id", "mapping_set_hash", "created_at"},
        )
    )


def calculate_unmapped_field_hash(value: UnmappedField) -> str:
    return canonical_hash(
        value.model_dump(
            mode="json",
            exclude={"unmapped_field_id", "unmapped_hash", "created_at"},
        )
    )


def calculate_unmapped_set_hash(value: UnmappedFieldSet) -> str:
    return canonical_hash(
        value.model_dump(
            mode="json",
            exclude={"unmapped_set_id", "unmapped_set_hash", "created_at"},
        )
    )


def calculate_mapping_output_hash(result: MappingResult) -> str:
    return canonical_hash(
        result.model_dump(
            mode="json",
            exclude={"output_hash": True, "event": {"payload": {"output_hash"}}},
        )
    )


def calculate_mapping_event_id(idempotency_key: str) -> str:
    value = canonical_hash({"idempotency_key": idempotency_key, "type": "field.mapped"})
    return f"evt_{value[:32]}"


def verify_mapping_request(request: MappingRequest, store: BronzeByteStore) -> None:
    verify_extraction_result(request.extraction_result, request.extraction_request, store)
    if not hmac.compare_digest(
        request.runtime.rule.rule_hash,
        calculate_mapping_rule_hash(request.runtime.rule),
    ):
        _fail("M14 mapping rule hash is invalid")
    if not hmac.compare_digest(
        request.runtime.runtime_hash,
        calculate_mapping_runtime_hash(request.runtime),
    ):
        _fail("M14 runtime hash is invalid")


def verify_mapping_result_hashes(result: MappingResult) -> None:
    for evidence in result.mapping_evidence:
        value = calculate_mapping_evidence_hash(evidence)
        if not hmac.compare_digest(evidence.evidence_hash, value) or (
            evidence.mapping_evidence_id != f"mpe_{value[:32]}"
        ):
            _fail("M14 MappingEvidence identity is invalid")
    for mapping in result.mapping_set.mappings:
        value = calculate_field_mapping_hash(mapping)
        if not hmac.compare_digest(mapping.mapping_hash, value) or (
            mapping.mapping_id != f"fmp_{value[:32]}"
        ):
            _fail("M14 FieldMapping identity is invalid")
    mapping_hash = calculate_mapping_set_hash(result.mapping_set)
    if not hmac.compare_digest(result.mapping_set.mapping_set_hash, mapping_hash) or (
        result.mapping_set.mapping_set_id != f"fms_{mapping_hash[:32]}"
    ):
        _fail("M14 mapping set identity is invalid")
    for field in result.unmapped_set.fields:
        value = calculate_unmapped_field_hash(field)
        if not hmac.compare_digest(field.unmapped_hash, value) or (
            field.unmapped_field_id != f"umf_{value[:32]}"
        ):
            _fail("M14 UnmappedField identity is invalid")
    unmapped_hash = calculate_unmapped_set_hash(result.unmapped_set)
    if not hmac.compare_digest(result.unmapped_set.unmapped_set_hash, unmapped_hash) or (
        result.unmapped_set.unmapped_set_id != f"ums_{unmapped_hash[:32]}"
    ):
        _fail("M14 unmapped set identity is invalid")
    if not (
        result.output_hash == calculate_mapping_output_hash(result)
        and result.event.event_id == calculate_mapping_event_id(result.idempotency_key)
        and result.event.event_type is EventType.FIELD_MAPPED
        and result.event.causation_event_id is not None
    ):
        _fail("M14 output hash or event identity is invalid")


def verify_mapping_result(
    result: MappingResult,
    request: MappingRequest,
    store: BronzeByteStore,
) -> None:
    verify_mapping_request(request, store)
    extraction = request.extraction_result
    contract = request.extraction_request.contract
    if not (
        result.task_id == contract.task_id
        and result.run_id == contract.run_id
        and result.contract_version == contract.version
        and result.contract_id == contract.contract_id
        and result.contract_hash == contract.contract_hash
        and result.upstream_extraction_input_hash == extraction.input_hash
        and result.upstream_extraction_output_hash == extraction.output_hash
        and result.policy == request.policy
        and result.policy_hash == calculate_mapping_policy_hash(request.policy)
        and result.runtime == request.runtime
        and result.input_hash == calculate_mapping_input_hash(request)
        and result.idempotency_key
        == calculate_mapping_idempotency_key(request, result.producer_version)
        and result.event.causation_event_id == extraction.event.event_id
    ):
        _fail("M14 result does not match its immutable request")
    verify_mapping_result_hashes(result)
    candidates = {item.candidate_id: item for item in extraction.candidate_set.candidates}
    atoms = {item.evidence_id: item for item in extraction.evidence_set.atoms}
    fields = {item.name: item for item in contract.fields}
    evidence_by_id = {item.mapping_evidence_id: item for item in result.mapping_evidence}
    for mapping in result.mapping_set.mappings:
        candidate = candidates.get(mapping.source_candidate_id)
        field = fields.get(mapping.target_field_name)
        evidence = evidence_by_id.get(mapping.mapping_evidence_id)
        if candidate is None or field is None or evidence is None:
            _fail("M14 mapping references are incomplete")
        compatible = is_value_kind_compatible(candidate.value_kind, field)
        if not (
            candidate.field_name == field.name == mapping.source_field_name
            and mapping.source_candidate_hash == candidate.candidate_hash
            and mapping.target_field_contract_hash == calculate_field_contract_hash(field)
            and mapping.source_evidence_ids == candidate.evidence_ids
            and mapping.entity_evidence_ids == candidate.entity_evidence_ids
            and all(item in atoms for item in mapping.source_evidence_ids)
            and mapping.type_compatible == compatible
            and mapping.score == 1.0
            and mapping.threshold == request.policy.auto_accept_threshold
            and evidence.source_candidate_id == candidate.candidate_id
            and evidence.source_header_cell_id == candidate.source_header_cell_id
            and evidence.source_header_cell_hash == candidate.source_header_cell_hash
            and evidence.source_value_kind is candidate.value_kind
            and evidence.source_evidence_ids == candidate.evidence_ids
            and evidence.entity_evidence_ids == candidate.entity_evidence_ids
            and evidence.target_field_name == field.name
            and evidence.target_field_contract_hash == calculate_field_contract_hash(field)
            and evidence.rule_id == request.runtime.rule.rule_id
            and evidence.rule_hash == request.runtime.rule.rule_hash
        ):
            _fail("M14 mapping does not exactly derive from candidate, evidence, and contract")
    gaps = {item.gap_id: item for item in extraction.gaps}
    tables = {
        item.table_id: item for item in request.extraction_request.table_parsing_result.tables
    }
    for unmapped in result.unmapped_set.fields:
        gap = gaps.get(unmapped.upstream_gap_id)
        table = tables.get(unmapped.source_table_id)
        cell = (
            next(
                (item for item in table.cells if item.cell_id == unmapped.source_header_cell_id),
                None,
            )
            if table is not None
            else None
        )
        if (
            gap is None
            or cell is None
            or not (
                gap.code is ExtractionGapCode.UNMAPPED_HEADER
                and gap.table_id == unmapped.source_table_id
                and gap.source_cell_id == unmapped.source_header_cell_id
                and cell.cell_hash == unmapped.source_header_cell_hash
                and unmapped.suggested_field_names
                == registered_alias_suggestions(cell.decoded_text, contract.fields)
            )
        ):
            _fail("M14 unmapped field does not replay to its exact upstream header gap")
    mapping_count = len(result.mapping_set.mappings)
    accepted = sum(item.eligible_for_m15 for item in result.mapping_set.mappings)
    expected_status = (
        MappingStatus.SUCCEEDED
        if mapping_count and accepted == mapping_count and not extraction.gaps
        else MappingStatus.PARTIAL
        if accepted
        else MappingStatus.NEEDS_REVIEW
        if mapping_count or extraction.gaps
        else MappingStatus.UNSUPPORTED
    )
    if not (
        len(candidates) == mapping_count
        and result.status is expected_status
        and result.upstream_gap_ids == tuple(item.gap_id for item in extraction.gaps)
        and result.metrics.input_candidate_count == len(candidates)
        and result.mapping_set.upstream_extraction_output_hash == extraction.output_hash
        and result.unmapped_set.upstream_extraction_output_hash == extraction.output_hash
    ):
        _fail("M14 status, metrics, or aggregate lineage does not derive from verified inputs")


def _fail(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)

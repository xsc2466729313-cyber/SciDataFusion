"""Canonical identities and end-to-end integrity for M13 extraction."""

from __future__ import annotations

import hashlib
import hmac
from typing import NoReturn

from scidatafusion.artifacts.storage import BronzeByteStore
from scidatafusion.contracts.events import EventType
from scidatafusion.contracts.extraction import (
    EvidenceAtom,
    EvidenceAtomSet,
    ExtractedFieldCandidate,
    ExtractedFieldCandidateSet,
    ExtractionGap,
    ExtractionPolicy,
    ExtractionRequest,
    ExtractionResult,
    ExtractionRuleDescriptor,
    ExtractionRuntimeSnapshot,
    ExtractionStatus,
)
from scidatafusion.contracts.scientific import (
    ContractStatus,
    FieldContract,
    FieldRequirement,
)
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.schema import ContractCompiler
from scidatafusion.tables.integrity import verify_table_result_integrity


def calculate_extraction_policy_hash(policy: ExtractionPolicy) -> str:
    return canonical_hash(policy.model_dump(mode="json"))


def calculate_extraction_rule_hash(rule: ExtractionRuleDescriptor) -> str:
    return canonical_hash(rule.model_dump(mode="json", exclude={"rule_hash"}))


def calculate_extraction_runtime_hash(runtime: ExtractionRuntimeSnapshot) -> str:
    return canonical_hash(runtime.model_dump(mode="json", exclude={"runtime_hash"}))


def calculate_field_contract_hash(field: FieldContract) -> str:
    return canonical_hash(field.model_dump(mode="json"))


def calculate_extraction_input_hash(request: ExtractionRequest) -> str:
    return canonical_hash(
        {
            "contract_hash": request.contract.contract_hash,
            "m10_input_hash": request.table_parsing_result.input_hash,
            "m10_output_hash": request.table_parsing_result.output_hash,
            "policy_hash": calculate_extraction_policy_hash(request.policy),
            "runtime_hash": request.runtime.runtime_hash,
        }
    )


def calculate_extraction_idempotency_key(request: ExtractionRequest, producer_version: str) -> str:
    return canonical_hash(
        {
            "contract_version": request.contract.version,
            "input_hash": calculate_extraction_input_hash(request),
            "module_id": "M13",
            "producer_version": producer_version,
            "task_id": request.contract.task_id,
        }
    )


def calculate_evidence_hash(atom: EvidenceAtom) -> str:
    return canonical_hash(
        atom.model_dump(mode="json", exclude={"evidence_hash", "evidence_id", "created_at"})
    )


def calculate_candidate_hash(candidate: ExtractedFieldCandidate) -> str:
    return canonical_hash(
        candidate.model_dump(mode="json", exclude={"candidate_hash", "candidate_id", "created_at"})
    )


def calculate_evidence_set_hash(value: EvidenceAtomSet) -> str:
    return canonical_hash(
        value.model_dump(
            mode="json", exclude={"evidence_set_hash", "evidence_set_id", "created_at"}
        )
    )


def calculate_candidate_set_hash(value: ExtractedFieldCandidateSet) -> str:
    return canonical_hash(
        value.model_dump(
            mode="json", exclude={"candidate_set_hash", "candidate_set_id", "created_at"}
        )
    )


def calculate_extraction_gap_id(gap: ExtractionGap) -> str:
    value = canonical_hash(gap.model_dump(mode="json", exclude={"gap_id"}))
    return f"xgp_{value[:16]}"


def calculate_extraction_output_hash(result: ExtractionResult) -> str:
    return canonical_hash(
        result.model_dump(
            mode="json",
            exclude={"output_hash": True, "event": {"payload": {"output_hash"}}},
        )
    )


def calculate_extraction_event_id(idempotency_key: str) -> str:
    value = canonical_hash({"idempotency_key": idempotency_key, "type": "field.extracted"})
    return f"evt_{value[:32]}"


def verify_extraction_request(request: ExtractionRequest, store: BronzeByteStore) -> None:
    ContractCompiler.verify_integrity(request.contract)
    if request.contract.status is not ContractStatus.CONFIRMED:
        _fail("M13 requires an explicitly confirmed scientific data contract")
    verify_table_result_integrity(
        request.table_parsing_result,
        request.table_parsing_request,
        store,
    )
    upstream_contract = request.table_parsing_request.parse_planning_request.contract
    if request.contract != upstream_contract:
        _fail("M13 contract must be the exact contract used by M08 and M10")
    if not hmac.compare_digest(
        request.runtime.rule.rule_hash,
        calculate_extraction_rule_hash(request.runtime.rule),
    ):
        _fail("M13 extraction rule hash is invalid")
    if not hmac.compare_digest(
        request.runtime.runtime_hash,
        calculate_extraction_runtime_hash(request.runtime),
    ):
        _fail("M13 runtime hash is invalid")


def verify_extraction_result_hashes(result: ExtractionResult) -> None:
    for atom in result.evidence_set.atoms:
        value = calculate_evidence_hash(atom)
        if not hmac.compare_digest(atom.evidence_hash, value) or atom.evidence_id != (
            f"evi_{value[:32]}"
        ):
            _fail("M13 EvidenceAtom identity is invalid")
    for candidate in result.candidate_set.candidates:
        value = calculate_candidate_hash(candidate)
        if not hmac.compare_digest(candidate.candidate_hash, value) or candidate.candidate_id != (
            f"fcd_{value[:32]}"
        ):
            _fail("M13 field candidate identity is invalid")
    evidence_hash = calculate_evidence_set_hash(result.evidence_set)
    if not hmac.compare_digest(result.evidence_set.evidence_set_hash, evidence_hash) or (
        result.evidence_set.evidence_set_id != f"evs_{evidence_hash[:32]}"
    ):
        _fail("M13 evidence set identity is invalid")
    candidate_hash = calculate_candidate_set_hash(result.candidate_set)
    if not hmac.compare_digest(result.candidate_set.candidate_set_hash, candidate_hash) or (
        result.candidate_set.candidate_set_id != f"fcs_{candidate_hash[:32]}"
    ):
        _fail("M13 candidate set identity is invalid")
    for gap in result.gaps:
        if gap.gap_id != calculate_extraction_gap_id(gap):
            _fail("M13 gap identity is invalid")
    if not (
        result.output_hash == calculate_extraction_output_hash(result)
        and result.event.event_id == calculate_extraction_event_id(result.idempotency_key)
        and result.event.event_type is EventType.FIELD_EXTRACTED
        and result.event.causation_event_id is not None
    ):
        _fail("M13 output hash or event identity is invalid")


def verify_extraction_result(
    result: ExtractionResult,
    request: ExtractionRequest,
    store: BronzeByteStore,
) -> None:
    verify_extraction_request(request, store)
    if not (
        result.task_id == request.contract.task_id
        and result.run_id == request.contract.run_id
        and result.contract_version == request.contract.version
        and result.contract_id == request.contract.contract_id
        and result.contract_hash == request.contract.contract_hash
        and result.upstream_table_input_hash == request.table_parsing_result.input_hash
        and result.upstream_table_output_hash == request.table_parsing_result.output_hash
        and result.policy == request.policy
        and result.policy_hash == calculate_extraction_policy_hash(request.policy)
        and result.runtime == request.runtime
        and result.input_hash == calculate_extraction_input_hash(request)
        and result.idempotency_key
        == calculate_extraction_idempotency_key(request, result.producer_version)
        and result.event.causation_event_id == request.table_parsing_result.event.event_id
    ):
        _fail("M13 result does not match its immutable request")
    verify_extraction_result_hashes(result)
    table_by_id = {item.table_id: item for item in request.table_parsing_result.tables}
    fields = {item.name: item for item in request.contract.fields}
    evidence_by_id = {item.evidence_id: item for item in result.evidence_set.atoms}
    required_fields = tuple(
        item.name
        for item in request.contract.fields
        if item.requirement is FieldRequirement.REQUIRED
    )
    candidate_fields = {item.field_name for item in result.candidate_set.candidates}
    if not (
        result.evidence_set.upstream_table_output_hash == request.table_parsing_result.output_hash
        and result.candidate_set.upstream_table_output_hash
        == request.table_parsing_result.output_hash
        and result.required_field_names == required_fields
        and result.extracted_required_field_names
        == tuple(item for item in required_fields if item in candidate_fields)
    ):
        _fail("M13 sets or required-field report do not match upstream and contract")
    for atom in result.evidence_set.atoms:
        table = table_by_id.get(atom.table_id)
        if table is None:
            _fail("M13 evidence references an unknown TableIR")
        cell = next((item for item in table.cells if item.cell_id == atom.cell_id), None)
        if cell is None or not (
            atom.artifact_hash == table.source_byte_sha256
            and atom.table_hash == table.table_hash
            and atom.cell_hash == cell.cell_hash
            and atom.row_index == cell.row_index
            and atom.column_index == cell.column_index
            and atom.start_byte == cell.source.start_byte
            and atom.end_byte == cell.source.end_byte
            and atom.raw_lexeme == cell.raw_text
            and atom.raw_value == cell.decoded_text
        ):
            _fail("M13 evidence does not match its exact source cell")
        content = store.read(table.source_byte_sha256)
        if content[atom.start_byte : atom.end_byte].decode("utf-8") != atom.raw_lexeme:
            _fail("M13 evidence cannot replay its exact Bronze bytes")
    for gap in result.gaps:
        if gap.source_cell_id is None:
            continue
        table = table_by_id.get(gap.table_id) if gap.table_id is not None else None
        cell = (
            next((item for item in table.cells if item.cell_id == gap.source_cell_id), None)
            if table is not None
            else None
        )
        if cell is None:
            _fail("M13 gap source cell does not resolve to its TableIR")
    for candidate in result.candidate_set.candidates:
        field = fields.get(candidate.field_name)
        table = table_by_id.get(candidate.source_table_id)
        evidence = evidence_by_id.get(candidate.evidence_ids[0])
        if field is None or table is None or evidence is None:
            _fail("M13 candidate references are incomplete")
        header = next(
            (item for item in table.cells if item.cell_id == candidate.source_header_cell_id),
            None,
        )
        if header is None or not (
            candidate.field_contract_hash == calculate_field_contract_hash(field)
            and header.row_index == 0
            and header.decoded_text == field.name
            and header.cell_hash == candidate.source_header_cell_hash
            and candidate.source_value_cell_id == evidence.cell_id
            and candidate.source_value_cell_hash == evidence.cell_hash
            and candidate.evidence_ids == (evidence.evidence_id,)
        ):
            _fail("M13 candidate does not match its contract field and source header")
        entity_atoms = tuple(evidence_by_id.get(item) for item in candidate.entity_evidence_ids)
        if any(item is None for item in entity_atoms):
            _fail("M13 candidate entity evidence is unresolved")
        resolved_entity_atoms = tuple(item for item in entity_atoms if item is not None)
        entity_fields = tuple(
            table.cells[item.column_index].decoded_text for item in resolved_entity_atoms
        )
        if not (
            entity_fields == request.contract.entity_keys
            and all(
                item.table_id == candidate.source_table_id
                and item.row_index == candidate.source_row_index
                for item in resolved_entity_atoms
            )
        ):
            _fail("M13 entity evidence does not bind the candidate row and entity keys")
    accepted_tables = sum(
        item.quality.passed and item.header_hierarchy.header_row_count == 1
        for item in request.table_parsing_result.tables
    )
    input_rows = sum(
        max(0, item.row_count - item.header_hierarchy.header_row_count)
        for item in request.table_parsing_result.tables
    )
    extracted_rows = len({item.row_group_id for item in result.candidate_set.candidates})
    required_coverage = (
        1.0
        if not required_fields
        else len(result.extracted_required_field_names) / len(required_fields)
    )
    candidate_count = len(result.candidate_set.candidates)
    expected_status = (
        ExtractionStatus.SUCCEEDED
        if candidate_count and not result.gaps
        else ExtractionStatus.PARTIAL
        if candidate_count
        else ExtractionStatus.NEEDS_REVIEW
        if result.gaps
        else ExtractionStatus.UNSUPPORTED
    )
    if not (
        result.status is expected_status
        and result.metrics.input_table_count == len(request.table_parsing_result.tables)
        and result.metrics.accepted_table_count == accepted_tables
        and result.metrics.input_data_row_count == input_rows
        and result.metrics.extracted_row_count == extracted_rows
        and result.metrics.evidence_atom_count == len(result.evidence_set.atoms)
        and result.metrics.candidate_count == candidate_count
        and result.metrics.explicit_candidate_count == candidate_count
        and result.metrics.evidence_coverage == 1.0
        and result.metrics.required_field_coverage == required_coverage
        and result.metrics.entity_bound_candidate_count == candidate_count
        and result.metrics.gap_count == len(result.gaps)
    ):
        _fail("M13 status or metrics do not derive from verified inputs and outputs")


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fail(message: str) -> NoReturn:
    raise AppError(ErrorCode.ARTIFACT_INTEGRITY_ERROR, message)

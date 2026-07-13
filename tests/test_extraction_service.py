from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError

from scidatafusion.artifacts.fixtures import build_offline_ia_artifact_bundle
from scidatafusion.artifacts.service import ArtifactDownloadService
from scidatafusion.artifacts.storage import MemoryBronzeStore
from scidatafusion.cli import _build_search_planning, _execute_offline_connectors
from scidatafusion.contracts.artifacts import ArtifactDownloadRequest, ArtifactDownloadResult
from scidatafusion.contracts.extraction import (
    CandidateOrigin,
    EvidenceAtom,
    EvidenceAtomSet,
    EvidenceSourceKind,
    ExtractedFieldCandidate,
    ExtractedFieldCandidateSet,
    ExtractionExecutionMode,
    ExtractionGapCode,
    ExtractionRequest,
    ExtractionResult,
    ExtractionRuntimeSnapshot,
    ExtractionStatus,
)
from scidatafusion.contracts.mapping import MappingDecision, MappingRequest, MappingStatus
from scidatafusion.contracts.parsing import ParsePlanningRequest
from scidatafusion.contracts.selection import SourceSelectionRequest
from scidatafusion.contracts.tables import TableParsingRequest, TableValueKind
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.extraction.checkpoints import MemoryExtractionCheckpointStore
from scidatafusion.extraction.fixtures import build_offline_extraction_bundle
from scidatafusion.extraction.integrity import (
    calculate_extraction_runtime_hash,
    verify_extraction_result,
    verify_extraction_result_hashes,
)
from scidatafusion.extraction.service import EvidenceFirstExtractionService
from scidatafusion.mapping.fixtures import build_offline_mapping_bundle
from scidatafusion.mapping.service import FieldMappingService
from scidatafusion.parsing.fixtures import build_offline_parse_planning_bundle
from scidatafusion.parsing.service import ParsePlanningService
from scidatafusion.selection import SourceSelectionService
from scidatafusion.tables.fixtures import build_offline_table_parsing_bundle
from scidatafusion.tables.service import TableParsingService


@dataclass(frozen=True)
class _ExtractionChain:
    store: MemoryBronzeStore
    request: ExtractionRequest


def _build_chain(*, replacement_csv: bytes | None = None) -> _ExtractionChain:
    phase1, planning = _build_search_planning(
        "Study Type Ia supernova light curves using multi-source integration into CSV.",
        "m13-service-tests",
    )
    assert planning is not None
    assert phase1.confirmation is not None
    contract = phase1.confirmation.contract
    connector_result = asyncio.run(_execute_offline_connectors(planning))
    selection_time = max(contract.created_at, planning.created_at, connector_result.created_at) + (
        timedelta(seconds=1)
    )
    selection = SourceSelectionService(clock=lambda: selection_time).select(
        SourceSelectionRequest(
            contract=contract,
            search_plan=planning.plan,
            connector_result=connector_result,
        )
    )
    artifact_time = selection.created_at + timedelta(seconds=1)
    artifact_bundle = build_offline_ia_artifact_bundle(
        selection.selected_source_set,
        clock=lambda: artifact_time,
    )
    transport = artifact_bundle.transport
    if replacement_csv is not None:

        async def replace_csv(request: httpx.Request) -> httpx.Response:
            response = await artifact_bundle.transport.handle_async_request(request)
            media_type = response.headers.get("content-type", "").split(";", 1)[0].casefold()
            if media_type != "text/csv":
                return response
            headers = {
                key: value
                for key, value in response.headers.items()
                if key.casefold() != "content-length"
            }
            return httpx.Response(
                response.status_code,
                content=replacement_csv,
                headers=headers,
                request=request,
            )

        transport = httpx.MockTransport(replace_csv)
    download_request = ArtifactDownloadRequest(
        selected_source_set=selection.selected_source_set,
        policy=artifact_bundle.policy,
        runtime=artifact_bundle.runtime,
        approvals=artifact_bundle.approvals,
        requested_at=artifact_time,
    )
    store = MemoryBronzeStore()
    download_service = ArtifactDownloadService(
        store=store,
        transport=transport,
        clock=lambda: artifact_time,
    )

    async def download() -> ArtifactDownloadResult:
        try:
            return await download_service.execute(download_request)
        finally:
            await download_service.aclose()

    download_result = asyncio.run(download())
    parse_time = download_result.created_at + timedelta(seconds=1)
    parse_bundle = build_offline_parse_planning_bundle(clock=lambda: parse_time)
    parse_request = ParsePlanningRequest(
        contract=contract,
        download_request=download_request,
        download_result=download_result,
        capability_registry=parse_bundle.registry,
        policy=parse_bundle.policy,
        runtime=parse_bundle.runtime,
        requested_at=parse_time,
    )
    parse_result = asyncio.run(ParsePlanningService(store=store).execute(parse_request))
    table_time = parse_result.created_at + timedelta(seconds=1)
    table_bundle = build_offline_table_parsing_bundle(
        parse_result.plan.capability_registry,
        parse_result.plan.runtime,
        clock=lambda: table_time,
        engine_version="3.11.9",
    )
    table_request = TableParsingRequest(
        parse_planning_request=parse_request,
        parse_planning_result=parse_result,
        policy=table_bundle.policy,
        runtime=table_bundle.runtime,
        requested_at=table_time,
    )
    table_result = asyncio.run(TableParsingService(bronze_store=store).execute(table_request))
    extraction_time = table_result.created_at + timedelta(seconds=1)
    extraction_bundle = build_offline_extraction_bundle(
        not_before=table_result.created_at,
        clock=lambda: extraction_time,
    )
    request = ExtractionRequest(
        contract=contract,
        table_parsing_request=table_request,
        table_parsing_result=table_result,
        policy=extraction_bundle.policy,
        runtime=extraction_bundle.runtime,
        requested_at=extraction_time,
    )
    return _ExtractionChain(store=store, request=request)


@pytest.fixture(scope="module")
def chain() -> _ExtractionChain:
    return _build_chain()


@pytest.fixture(scope="module")
def result(chain: _ExtractionChain) -> ExtractionResult:
    return asyncio.run(
        EvidenceFirstExtractionService(bronze_store=chain.store).execute(chain.request)
    )


def test_ia_table_extracts_only_explicit_evidence_bound_candidates(
    chain: _ExtractionChain,
    result: ExtractionResult,
) -> None:
    verify_extraction_result(result, chain.request, chain.store)
    assert result.status is ExtractionStatus.PARTIAL
    assert result.metrics.input_table_count == 1
    assert result.metrics.accepted_table_count == 1
    assert result.metrics.input_data_row_count == 1
    assert result.metrics.extracted_row_count == 1
    assert result.metrics.evidence_atom_count == 4
    assert result.metrics.candidate_count == 4
    assert result.metrics.explicit_candidate_count == 4
    assert result.metrics.inferred_candidate_count == 0
    assert result.metrics.derived_candidate_count == 0
    assert result.metrics.evidence_coverage == 1.0
    assert result.metrics.required_field_coverage == 0.75
    assert result.metrics.entity_bound_candidate_count == 4
    assert result.metrics.model_attempt_count == result.metrics.network_attempt_count == 0
    assert result.metrics.actual_cost_micro_usd == 0
    assert {item.field_name for item in result.candidate_set.candidates} == {
        "object_id",
        "observation_time",
        "band",
        "magnitude",
    }
    assert all(item.origin is CandidateOrigin.EXPLICIT for item in result.candidate_set.candidates)
    assert all(item.evidence_ids for item in result.candidate_set.candidates)
    assert all(item.entity_evidence_ids for item in result.candidate_set.candidates)
    assert all(item.confidence == 1.0 for item in result.candidate_set.candidates)
    assert all(
        item.source_kind is EvidenceSourceKind.TABLE_CELL for item in result.evidence_set.atoms
    )
    assert all(item.confidence == 1.0 for item in result.evidence_set.atoms)
    assert result.gaps[0].code is ExtractionGapCode.REQUIRED_FIELD_HEADER_MISSING
    assert result.gaps[0].field_name == "source_record_id"
    assert result.event.event_type.value == "field.extracted"
    assert result.event.causation_event_id == chain.request.table_parsing_result.event.event_id

    candidates = {item.field_name: item for item in result.candidate_set.candidates}
    assert candidates["observation_time"].raw_value == "59000.1"
    assert candidates["observation_time"].value_kind is TableValueKind.DECIMAL_CANDIDATE
    assert candidates["magnitude"].raw_value == "12.3"
    entity_evidence = candidates["object_id"].evidence_ids[0]
    assert all(item.entity_evidence_ids == (entity_evidence,) for item in candidates.values())
    assert len({item.row_group_id for item in candidates.values()}) == 1


def test_evidence_replays_exact_bronze_lexemes(
    chain: _ExtractionChain,
    result: ExtractionResult,
) -> None:
    for atom in result.evidence_set.atoms:
        content = chain.store.read(atom.artifact_hash)
        assert content[atom.start_byte : atom.end_byte].decode() == atom.raw_lexeme
        assert atom.raw_value


def test_identical_and_concurrent_calls_replay_one_result(chain: _ExtractionChain) -> None:
    service = EvidenceFirstExtractionService(bronze_store=chain.store)
    first = asyncio.run(service.execute(chain.request))
    second = asyncio.run(service.execute(chain.request))

    async def concurrent() -> tuple[ExtractionResult, ExtractionResult]:
        left, right = await asyncio.gather(
            service.execute(chain.request),
            service.execute(chain.request),
        )
        return left, right

    pair = asyncio.run(concurrent())
    assert first == second == pair[0] == pair[1]
    assert first.event.event_id == second.event.event_id


def test_missing_entity_key_blocks_the_entire_row() -> None:
    chain = _build_chain(
        replacement_csv=b"object_id,observation_time,band,magnitude\n,59000.1,B,12.3\n"
    )
    result = asyncio.run(
        EvidenceFirstExtractionService(bronze_store=chain.store).execute(chain.request)
    )

    assert result.status is ExtractionStatus.PARTIAL
    gap = next(
        item for item in result.gaps if item.code is ExtractionGapCode.ENTITY_BINDING_MISSING
    )
    assert gap.table_id is not None
    assert not any(item.source_table_id == gap.table_id for item in result.candidate_set.candidates)


def test_empty_required_value_retains_other_explicit_candidates() -> None:
    chain = _build_chain(
        replacement_csv=b"object_id,observation_time,band,magnitude\nSN-A,,B,12.3\n"
    )
    result = asyncio.run(
        EvidenceFirstExtractionService(bronze_store=chain.store).execute(chain.request)
    )

    assert result.status is ExtractionStatus.PARTIAL
    gap = next(item for item in result.gaps if item.code is ExtractionGapCode.REQUIRED_VALUE_EMPTY)
    assert gap.table_id is not None
    affected = {
        item.field_name
        for item in result.candidate_set.candidates
        if item.source_table_id == gap.table_id
    }
    assert affected == {
        "object_id",
        "band",
        "magnitude",
    }
    assert not any(
        item.raw_value == "" and item.source_table_id == gap.table_id
        for item in result.candidate_set.candidates
    )


def test_alias_headers_are_not_silently_mapped_and_have_distinct_gap_evidence() -> None:
    chain = _build_chain(replacement_csv=b"object_id,mjd,filter,magnitude\nSN-A,59000.1,B,12.3\n")
    result = asyncio.run(
        EvidenceFirstExtractionService(bronze_store=chain.store).execute(chain.request)
    )

    gap = next(
        item
        for item in result.gaps
        if item.code is ExtractionGapCode.REQUIRED_FIELD_HEADER_MISSING
        and item.field_name == "observation_time"
    )
    assert gap.table_id is not None
    assert "observation_time" not in {
        item.field_name
        for item in result.candidate_set.candidates
        if item.source_table_id == gap.table_id
    }
    assert any(
        item.code is ExtractionGapCode.REQUIRED_FIELD_HEADER_MISSING
        and item.field_name == "observation_time"
        for item in result.gaps
    )
    unmapped = tuple(item for item in result.gaps if item.code is ExtractionGapCode.UNMAPPED_HEADER)
    assert len(unmapped) == 2
    assert len({item.gap_id for item in unmapped}) == 2
    assert all(item.source_cell_id is not None for item in unmapped)


def test_m14_retains_alias_headers_but_blocks_mapping_without_value_evidence() -> None:
    chain = _build_chain(replacement_csv=b"object_id,mjd,filter,magnitude\nSN-A,59000.1,B,12.3\n")
    extraction = asyncio.run(
        EvidenceFirstExtractionService(bronze_store=chain.store).execute(chain.request)
    )
    mapping_time = extraction.created_at + timedelta(seconds=1)
    bundle = build_offline_mapping_bundle(
        not_before=extraction.created_at,
        clock=lambda: mapping_time,
    )
    request = MappingRequest(
        extraction_request=chain.request,
        extraction_result=extraction,
        policy=bundle.policy,
        runtime=bundle.runtime,
        requested_at=bundle.runtime.checked_at,
    )
    result = asyncio.run(FieldMappingService(bronze_store=chain.store).execute(request))

    assert result.status is MappingStatus.PARTIAL
    assert result.metrics.input_candidate_count == result.metrics.auto_accepted_count
    assert result.metrics.input_candidate_count > 0
    assert result.metrics.unmapped_field_count >= 2
    assert result.metrics.alias_suggestion_count == result.metrics.unmapped_field_count
    unmapped_table_ids = {item.source_table_id for item in result.unmapped_set.fields}
    candidates = {item.candidate_id: item for item in extraction.candidate_set.candidates}
    alias_table_targets = {
        item.target_field_name
        for item in result.mapping_set.mappings
        if candidates[item.source_candidate_id].source_table_id in unmapped_table_ids
    }
    assert alias_table_targets == {
        "object_id",
        "magnitude",
    }
    assert {item.suggested_field_names for item in result.unmapped_set.fields} == {
        ("observation_time",),
        ("band",),
    }
    assert len({item.unmapped_field_id for item in result.unmapped_set.fields}) == len(
        result.unmapped_set.fields
    )
    assert all(item.source_header_cell_id for item in result.unmapped_set.fields)


def test_m14_blocks_numeric_field_with_text_value_kind_from_m15() -> None:
    chain = _build_chain(
        replacement_csv=(b"object_id,observation_time,band,magnitude\nSN-A,not-a-number,B,12.3\n")
    )
    extraction = asyncio.run(
        EvidenceFirstExtractionService(bronze_store=chain.store).execute(chain.request)
    )
    mapping_time = extraction.created_at + timedelta(seconds=1)
    bundle = build_offline_mapping_bundle(
        not_before=extraction.created_at,
        clock=lambda: mapping_time,
    )
    request = MappingRequest(
        extraction_request=chain.request,
        extraction_result=extraction,
        policy=bundle.policy,
        runtime=bundle.runtime,
        requested_at=bundle.runtime.checked_at,
    )
    result = asyncio.run(FieldMappingService(bronze_store=chain.store).execute(request))
    candidates = {item.candidate_id: item for item in extraction.candidate_set.candidates}
    blocked = tuple(
        item
        for item in result.mapping_set.mappings
        if item.target_field_name == "observation_time"
        and candidates[item.source_candidate_id].value_kind is TableValueKind.TEXT
    )

    assert blocked
    assert all(not item.type_compatible for item in blocked)
    assert all(item.decision is MappingDecision.BLOCKED_TYPE_CONFLICT for item in blocked)
    assert all(not item.eligible_for_m15 for item in blocked)


def test_runtime_and_result_tampering_fail_closed(
    chain: _ExtractionChain,
    result: ExtractionResult,
) -> None:
    runtime = chain.request.runtime.model_copy(update={"runtime_hash": "f" * 64})
    request = chain.request.model_copy(update={"runtime": runtime})
    with pytest.raises(AppError) as captured:
        asyncio.run(EvidenceFirstExtractionService(bronze_store=chain.store).execute(request))
    assert captured.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR

    atoms = list(result.evidence_set.atoms)
    atoms[0] = atoms[0].model_copy(update={"raw_value": "invented"})
    evidence_set = result.evidence_set.model_copy(update={"atoms": tuple(atoms)})
    tampered = result.model_copy(update={"evidence_set": evidence_set})
    with pytest.raises(AppError):
        verify_extraction_result_hashes(tampered)


def test_checkpoint_is_canonical_bounded_and_tamper_evident(result: ExtractionResult) -> None:
    checkpoints = MemoryExtractionCheckpointStore()
    assert checkpoints.load(result.idempotency_key) is None
    assert checkpoints.save(result) == result
    assert checkpoints.save(result) == result
    with pytest.raises(AppError):
        checkpoints.load("invalid")
    with pytest.raises(AppError):
        MemoryExtractionCheckpointStore(max_checkpoint_bytes=0)
    with pytest.raises(AppError) as captured:
        MemoryExtractionCheckpointStore(max_checkpoint_bytes=1).save(result)
    assert captured.value.code is ErrorCode.VALIDATION_FAILED
    checkpoints._values[result.idempotency_key] = b"{}"
    with pytest.raises(AppError) as captured:
        checkpoints.load(result.idempotency_key)
    assert captured.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_contracts_reject_missing_evidence_and_value_drift(result: ExtractionResult) -> None:
    candidate = result.candidate_set.candidates[0]
    payload = candidate.model_dump(mode="python")
    payload["evidence_ids"] = ()
    with pytest.raises(ValidationError):
        ExtractedFieldCandidate.model_validate(payload)
    payload = candidate.model_dump(mode="python")
    payload["raw_value"] = "changed"
    with pytest.raises(ValidationError):
        ExtractedFieldCandidate.model_validate(payload)

    atom = result.evidence_set.atoms[0]
    payload = atom.model_dump(mode="python")
    payload["end_byte"] = payload["start_byte"] - 1
    with pytest.raises(ValidationError):
        EvidenceAtom.model_validate(payload)
    payload = atom.model_dump(mode="python")
    payload["raw_lexeme"] = "changed"
    with pytest.raises(ValidationError):
        EvidenceAtom.model_validate(payload)


def test_runtime_fixture_is_content_addressed_and_monotonic(chain: _ExtractionChain) -> None:
    runtime = chain.request.runtime
    assert runtime.runtime_hash == calculate_extraction_runtime_hash(runtime)
    with pytest.raises(ValueError, match="cannot predate"):
        build_offline_extraction_bundle(
            not_before=runtime.checked_at,
            clock=lambda: runtime.checked_at - timedelta(seconds=1),
        )


def test_nested_contracts_reject_confidence_duplicates_and_metadata_drift(
    result: ExtractionResult,
) -> None:
    atom = result.evidence_set.atoms[0]
    payload = atom.model_dump(mode="python")
    payload["confidence"] = 0.5
    with pytest.raises(ValidationError):
        EvidenceAtom.model_validate(payload)
    payload = atom.model_dump(mode="python")
    payload["raw_value_sha256"] = "f" * 64
    with pytest.raises(ValidationError):
        EvidenceAtom.model_validate(payload)

    candidate = result.candidate_set.candidates[0]
    payload = candidate.model_dump(mode="python")
    payload["confidence"] = 0.5
    with pytest.raises(ValidationError):
        ExtractedFieldCandidate.model_validate(payload)
    payload = candidate.model_dump(mode="python")
    payload["evidence_ids"] = (candidate.evidence_ids[0], candidate.evidence_ids[0])
    with pytest.raises(ValidationError):
        ExtractedFieldCandidate.model_validate(payload)
    payload = candidate.model_dump(mode="python")
    payload["entity_evidence_ids"] = (
        candidate.entity_evidence_ids[0],
        candidate.entity_evidence_ids[0],
    )
    with pytest.raises(ValidationError):
        ExtractedFieldCandidate.model_validate(payload)

    payload = result.evidence_set.model_dump(mode="python")
    payload["atoms"] = (atom, atom)
    with pytest.raises(ValidationError):
        EvidenceAtomSet.model_validate(payload)
    payload = result.evidence_set.model_dump(mode="python")
    payload["atoms"] = (atom.model_copy(update={"producer_version": "9.9.9"}),)
    with pytest.raises(ValidationError):
        EvidenceAtomSet.model_validate(payload)

    payload = result.candidate_set.model_dump(mode="python")
    payload["candidates"] = (candidate, candidate)
    with pytest.raises(ValidationError):
        ExtractedFieldCandidateSet.model_validate(payload)
    payload = result.candidate_set.model_dump(mode="python")
    payload["candidates"] = (candidate.model_copy(update={"contract_hash": "f" * 64}),)
    with pytest.raises(ValidationError):
        ExtractedFieldCandidateSet.model_validate(payload)


def test_result_contract_rejects_all_cross_link_and_metric_drift(
    result: ExtractionResult,
) -> None:
    payload = result.model_dump(mode="python")
    payload["required_field_names"] = (*result.required_field_names, result.required_field_names[0])
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate(payload)

    payload = result.model_dump(mode="python")
    payload["gaps"] = (result.gaps[0], result.gaps[0])
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate(payload)

    payload = result.model_dump(mode="python")
    payload["extracted_required_field_names"] = ()
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate(payload)

    candidate = result.candidate_set.candidates[0]
    bad_candidate = candidate.model_copy(update={"evidence_ids": ("evi_" + "f" * 32,)})
    payload = result.model_dump(mode="python")
    payload["candidate_set"] = result.candidate_set.model_copy(
        update={"candidates": (bad_candidate, *result.candidate_set.candidates[1:])}
    )
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate(payload)

    evidence = result.evidence_set.atoms[0]
    bad_candidate = candidate.model_copy(update={"raw_value": evidence.raw_value + "x"})
    payload = result.model_dump(mode="python")
    payload["candidate_set"] = result.candidate_set.model_copy(
        update={"candidates": (bad_candidate, *result.candidate_set.candidates[1:])}
    )
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate(payload)

    payload = result.model_dump(mode="python")
    payload["metrics"] = result.metrics.model_copy(update={"candidate_count": 99})
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate(payload)

    payload = result.model_dump(mode="python")
    payload["warnings"] = ()
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate(payload)

    payload = result.model_dump(mode="python")
    payload["event"] = result.event.model_copy(
        update={"payload": result.event.payload.model_copy(update={"candidate_count": 99})}
    )
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate(payload)


def test_request_and_runtime_contracts_reject_time_and_mode_drift(
    chain: _ExtractionChain,
) -> None:
    runtime_payload = chain.request.runtime.model_dump(mode="python")
    runtime_payload["execution_mode"] = ExtractionExecutionMode.LIVE
    with pytest.raises(ValidationError):
        ExtractionRuntimeSnapshot.model_validate(runtime_payload)

    request_payload = chain.request.model_dump(mode="python")
    request_payload["requested_at"] = chain.request.requested_at + timedelta(seconds=1)
    with pytest.raises(ValidationError):
        ExtractionRequest.model_validate(request_payload)

    request_payload = chain.request.model_dump(mode="python")
    request_payload["runtime"] = chain.request.runtime.model_copy(
        update={"checked_at": chain.request.table_parsing_result.created_at - timedelta(seconds=1)}
    )
    request_payload["requested_at"] = request_payload["runtime"].checked_at
    with pytest.raises(ValidationError):
        ExtractionRequest.model_validate(request_payload)


def test_output_and_table_budgets_fail_before_unbounded_extraction(
    chain: _ExtractionChain,
) -> None:
    policy = chain.request.policy.model_copy(update={"max_candidates": 1})
    request = chain.request.model_copy(update={"policy": policy})
    with pytest.raises(AppError) as captured:
        asyncio.run(EvidenceFirstExtractionService(bronze_store=chain.store).execute(request))
    assert captured.value.code is ErrorCode.BUDGET_EXCEEDED


def test_fresh_service_replays_shared_checkpoint(
    chain: _ExtractionChain,
) -> None:
    checkpoints = MemoryExtractionCheckpointStore()
    first = asyncio.run(
        EvidenceFirstExtractionService(
            bronze_store=chain.store,
            checkpoints=checkpoints,
        ).execute(chain.request)
    )
    second = asyncio.run(
        EvidenceFirstExtractionService(
            bronze_store=chain.store,
            checkpoints=checkpoints,
        ).execute(chain.request)
    )
    assert first == second

    checkpoints._values[first.idempotency_key] = b"conflict"
    with pytest.raises(AppError, match="different checkpoint"):
        checkpoints.save(first)

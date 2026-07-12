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
from scidatafusion.contracts.artifacts import ArtifactDownloadRequest
from scidatafusion.contracts.parsing import (
    ParsePlanningExecutionMode,
    ParsePlanningRequest,
    ParsePlanningResult,
)
from scidatafusion.contracts.selection import SourceSelectionRequest
from scidatafusion.contracts.tables import (
    HeaderHierarchy,
    HeaderNode,
    TableAttemptStatus,
    TableByteSpan,
    TableGapCode,
    TableIR,
    TableParseAttempt,
    TableParserRuntimeDescriptor,
    TableParsingPolicy,
    TableParsingRequest,
    TableParsingResult,
    TableParsingRuntimeSnapshot,
    TableParsingStatus,
    TableQualityCheck,
    TableQualityReport,
    TableRouteResult,
    TableValueKind,
)
from scidatafusion.errors import AppError, ErrorCode
from scidatafusion.parsing.fixtures import build_offline_parse_planning_bundle
from scidatafusion.parsing.integrity import calculate_parse_runtime_hash
from scidatafusion.parsing.registry import calculate_parser_registry_hash
from scidatafusion.parsing.service import ParsePlanningService
from scidatafusion.selection import SourceSelectionService
from scidatafusion.tables.checkpoints import MemoryTableCheckpointStore
from scidatafusion.tables.csv import (
    CsvAdapterLimits,
    CsvTableAdapter,
    RawTable,
    TableAdapterError,
    TableAdapterErrorCode,
)
from scidatafusion.tables.fixtures import build_offline_table_parsing_bundle
from scidatafusion.tables.integrity import (
    _infer_kind,
    calculate_table_runtime_hash,
    verify_table_result_hashes,
    verify_table_result_integrity,
)
from scidatafusion.tables.projection import table_to_polars, table_to_rows
from scidatafusion.tables.service import TableParsingService
from scidatafusion.tables.storage import MemoryTableIRStore


@dataclass(frozen=True)
class _TableChain:
    bronze_store: MemoryBronzeStore
    parse_request: ParsePlanningRequest
    parse_result: ParsePlanningResult
    table_request: TableParsingRequest


def _build_chain(*, replacement_csv: bytes | None = None) -> _TableChain:
    phase1, planning = _build_search_planning(
        "Study Type Ia supernova light curves using multi-source integration into CSV.",
        "m10-service-tests",
    )
    assert planning is not None
    assert phase1.confirmation is not None
    contract = phase1.confirmation.contract
    connector_result = asyncio.run(_execute_offline_connectors(planning))
    selection_time = max(contract.created_at, planning.created_at, connector_result.created_at) + (
        timedelta(seconds=1)
    )
    selected = SourceSelectionService(clock=lambda: selection_time).select(
        SourceSelectionRequest(
            contract=contract,
            search_plan=planning.plan,
            connector_result=connector_result,
        )
    )
    artifact_time = selected.created_at + timedelta(seconds=1)
    artifact_bundle = build_offline_ia_artifact_bundle(
        selected.selected_source_set,
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
        selected_source_set=selected.selected_source_set,
        policy=artifact_bundle.policy,
        runtime=artifact_bundle.runtime,
        approvals=artifact_bundle.approvals,
        requested_at=artifact_time,
    )
    bronze_store = MemoryBronzeStore()
    download_service = ArtifactDownloadService(
        store=bronze_store,
        transport=transport,
        clock=lambda: artifact_time,
    )

    async def download() -> object:
        try:
            return await download_service.execute(download_request)
        finally:
            await download_service.aclose()

    download_result = asyncio.run(download())
    from scidatafusion.contracts.artifacts import ArtifactDownloadResult

    assert isinstance(download_result, ArtifactDownloadResult)
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
    parse_result = asyncio.run(ParsePlanningService(store=bronze_store).execute(parse_request))
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
    return _TableChain(bronze_store, parse_request, parse_result, table_request)


@pytest.fixture(scope="module")
def chain() -> _TableChain:
    return _build_chain()


@pytest.fixture(scope="module")
def parsed_result(chain: _TableChain) -> TableParsingResult:
    return asyncio.run(
        TableParsingService(
            bronze_store=chain.bronze_store,
            adapter=CsvTableAdapter(engine_version="3.11.9"),
        ).execute(chain.table_request)
    )


def test_offline_table_runtime_is_exact_and_deterministic(chain: _TableChain) -> None:
    first = build_offline_table_parsing_bundle(
        chain.parse_result.plan.capability_registry,
        chain.parse_result.plan.runtime,
        clock=lambda: chain.table_request.runtime.checked_at,
        engine_version="3.11.9",
    )
    second = build_offline_table_parsing_bundle(
        chain.parse_result.plan.capability_registry,
        chain.parse_result.plan.runtime,
        clock=lambda: chain.table_request.runtime.checked_at,
        engine_version="3.11.9",
    )

    assert first == second
    assert first.runtime.available_parser_ids == ("m10.csv",)
    assert first.runtime.runtime_hash == calculate_table_runtime_hash(first.runtime)
    assert first.runtime.model_execution_enabled is False
    assert first.runtime.external_network_enabled is False


def test_service_parses_ia_csv_with_cell_level_evidence(chain: _TableChain) -> None:
    table_store = MemoryTableIRStore()
    result = asyncio.run(
        TableParsingService(
            bronze_store=chain.bronze_store,
            table_store=table_store,
            adapter=CsvTableAdapter(engine_version="3.11.9"),
        ).execute(chain.table_request)
    )

    verify_table_result_integrity(result, chain.table_request, chain.bronze_store)
    assert result.status is TableParsingStatus.SUCCEEDED
    assert len(result.route_results) == len(result.attempts) == len(result.tables) == 1
    assert result.metrics.model_attempt_count == result.metrics.network_attempt_count == 0
    assert result.metrics.actual_cost_micro_usd == 0
    assert result.metrics.row_count == 2
    assert result.metrics.column_count == 4
    assert result.metrics.cell_count == result.metrics.exact_cell_evidence_count == 8
    table = result.tables[0]
    assert table.row_count == 2
    assert table.column_count == 4
    assert table.quality.passed is True
    assert [cell.decoded_text for cell in table.cells[:4]] == [
        "object_id",
        "observation_time",
        "band",
        "magnitude",
    ]
    source = chain.bronze_store.read(table.source_byte_sha256)
    for cell in table.cells:
        assert source[cell.source.start_byte : cell.source.end_byte].decode() == cell.raw_text
    assert table.cells[4].decoded_text == "SN-A"
    assert table.cells[5].inferred_kind is TableValueKind.DECIMAL_CANDIDATE
    assert table.cells[7].inferred_kind is TableValueKind.DECIMAL_CANDIDATE
    reference = result.route_results[0].table_ref
    assert reference is not None
    assert table_store.read(reference.artifact_sha256) == table
    assert result.event.event_type.value == "table.parsed"
    assert result.event.causation_event_id == chain.parse_result.event.event_id

    rows = table_to_rows(table)
    assert rows == (
        ("object_id", "observation_time", "band", "magnitude"),
        ("SN-A", "59000.1", "B", "12.3"),
    )
    frame = table_to_polars(table)
    assert frame.columns == list(rows[0])
    assert frame.shape == (1, 4)
    assert frame.row(0) == rows[1]
    assert all(str(dtype) == "String" for dtype in frame.dtypes)


def test_identical_requests_replay_one_result(chain: _TableChain) -> None:
    service = TableParsingService(
        bronze_store=chain.bronze_store,
        adapter=CsvTableAdapter(engine_version="3.11.9"),
    )
    first = asyncio.run(service.execute(chain.table_request))
    second = asyncio.run(service.execute(chain.table_request))

    async def execute_concurrently() -> tuple[TableParsingResult, TableParsingResult]:
        first_concurrent, second_concurrent = await asyncio.gather(
            service.execute(chain.table_request),
            service.execute(chain.table_request),
        )
        return first_concurrent, second_concurrent

    concurrent = asyncio.run(execute_concurrently())

    assert first == second == concurrent[0] == concurrent[1]
    assert first.event.event_id == second.event.event_id


def test_duplicate_header_is_retained_but_requires_review() -> None:
    chain = _build_chain(
        replacement_csv=(b"object_id,object_id,band,magnitude\nSN-A,59000.1,B,12.3\n")
    )
    result = asyncio.run(
        TableParsingService(
            bronze_store=chain.bronze_store,
            adapter=CsvTableAdapter(engine_version="3.11.9"),
        ).execute(chain.table_request)
    )

    assert result.status in {TableParsingStatus.NEEDS_REVIEW, TableParsingStatus.PARTIAL}
    reviewed = next(item for item in result.tables if not item.quality.passed)
    assert any(item.status is TableAttemptStatus.QUALITY_FAILED for item in result.attempts)
    assert [cell.decoded_text for cell in reviewed.cells[:4]] == [
        "object_id",
        "object_id",
        "band",
        "magnitude",
    ]
    assert any(item.code is TableGapCode.QUALITY_UNSATISFIED for item in result.gaps)
    with pytest.raises(AppError, match="unique header"):
        table_to_polars(reviewed)


def test_malformed_csv_returns_structured_review_without_table() -> None:
    chain = _build_chain()

    class FailingCsvAdapter(CsvTableAdapter):
        def parse(
            self,
            content: bytes,
            *,
            media_type: str,
            limits: CsvAdapterLimits,
        ) -> RawTable:
            raise TableAdapterError(
                TableAdapterErrorCode.MALFORMED_TABLE,
                "injected malformed CSV",
            )

    result = asyncio.run(
        TableParsingService(
            bronze_store=chain.bronze_store,
            adapter=FailingCsvAdapter(engine_version="3.11.9"),
        ).execute(chain.table_request)
    )

    assert result.status is TableParsingStatus.NEEDS_REVIEW
    assert result.tables == ()
    assert result.attempts[0].status is TableAttemptStatus.FAILED
    assert result.attempts[0].error_code is TableGapCode.ADAPTER_ERROR
    assert result.gaps[0].code is TableGapCode.ADAPTER_ERROR


def test_unavailable_runtime_records_blocked_attempt(chain: _TableChain) -> None:
    runtime_draft = chain.table_request.runtime.model_copy(
        update={
            "available_parser_ids": (),
            "parser_descriptors": (),
            "runtime_hash": "0" * 64,
        }
    )
    runtime = runtime_draft.model_copy(
        update={"runtime_hash": calculate_table_runtime_hash(runtime_draft)}
    )
    request = chain.table_request.model_copy(update={"runtime": runtime})
    result = asyncio.run(TableParsingService(bronze_store=chain.bronze_store).execute(request))

    assert result.status is TableParsingStatus.NEEDS_REVIEW
    assert result.attempts[0].status is TableAttemptStatus.BLOCKED
    assert result.attempts[0].engine_name is None
    assert result.gaps[0].code is TableGapCode.PARSER_UNAVAILABLE


def test_service_rejects_adapter_runtime_mismatch(chain: _TableChain) -> None:
    with pytest.raises(AppError) as captured:
        asyncio.run(
            TableParsingService(
                bronze_store=chain.bronze_store,
                adapter=CsvTableAdapter(engine_version="3.12.0"),
            ).execute(chain.table_request)
        )
    assert captured.value.code is ErrorCode.CONFIGURATION_ERROR


def test_result_tampering_fails_closed(chain: _TableChain) -> None:
    result = asyncio.run(
        TableParsingService(
            bronze_store=chain.bronze_store,
            adapter=CsvTableAdapter(engine_version="3.11.9"),
        ).execute(chain.table_request)
    )
    payload = result.model_dump(mode="json")
    del payload
    cells = list(result.tables[0].cells)
    cells[7] = cells[7].model_copy(update={"decoded_text": "99.9"})
    table = result.tables[0].model_copy(update={"cells": tuple(cells)})
    tampered = result.model_copy(update={"tables": (table,)})

    with pytest.raises(AppError):
        verify_table_result_integrity(tampered, chain.table_request, chain.bronze_store)


def test_offline_bundle_rejects_tampered_m08_runtime(chain: _TableChain) -> None:
    runtime = chain.parse_result.plan.runtime.model_copy(update={"runtime_hash": "f" * 64})
    with pytest.raises(ValueError, match="integrity-valid M08 runtime"):
        build_offline_table_parsing_bundle(
            chain.parse_result.plan.capability_registry,
            runtime,
        )


def test_offline_bundle_can_shrink_missing_m08_csv_capability(chain: _TableChain) -> None:
    available = tuple(
        item for item in chain.parse_result.plan.runtime.available_parser_ids if item != "m10.csv"
    )
    draft = chain.parse_result.plan.runtime.model_copy(
        update={"available_parser_ids": available, "runtime_hash": "0" * 64}
    )
    m08_runtime = draft.model_copy(update={"runtime_hash": calculate_parse_runtime_hash(draft)})
    bundle = build_offline_table_parsing_bundle(
        chain.parse_result.plan.capability_registry,
        m08_runtime,
        clock=lambda: chain.table_request.runtime.checked_at,
    )
    assert bundle.runtime.available_parser_ids == ()
    assert bundle.runtime.parser_descriptors == ()


def test_policy_and_runtime_contracts_fail_closed(chain: _TableChain) -> None:
    with pytest.raises(ValidationError):
        TableParsingPolicy(max_rows_per_table=1, max_columns_per_table=1, max_cells_per_table=2)
    with pytest.raises(ValidationError):
        TableParsingPolicy(allow_external_network=True)

    runtime = chain.table_request.runtime
    payload = runtime.model_dump(mode="python")
    payload["available_parser_ids"] = ("m10.csv", "m10.csv")
    with pytest.raises(ValidationError):
        TableParsingRuntimeSnapshot.model_validate(payload)
    payload = runtime.model_dump(mode="python")
    payload["parser_descriptors"] = ()
    with pytest.raises(ValidationError):
        TableParsingRuntimeSnapshot.model_validate(payload)
    payload = runtime.model_dump(mode="python")
    payload["model_execution_enabled"] = True
    with pytest.raises(ValidationError):
        TableParsingRuntimeSnapshot.model_validate(payload)

    request_payload = chain.table_request.model_dump(mode="python")
    request_payload["requested_at"] = chain.table_request.requested_at + timedelta(seconds=1)
    with pytest.raises(ValidationError):
        TableParsingRequest.model_validate(request_payload)


def test_nested_table_contracts_reject_inconsistent_shapes(
    parsed_result: TableParsingResult,
) -> None:
    table = parsed_result.tables[0]
    with pytest.raises(ValidationError):
        TableByteSpan(
            object_id=table.object_id,
            byte_sha256=table.source_byte_sha256,
            start_byte=3,
            end_byte=2,
        )
    node = table.header_hierarchy.nodes[0]
    payload = node.model_dump(mode="python")
    payload["end_column"] = -1
    with pytest.raises(ValidationError):
        HeaderNode.model_validate(payload)
    payload = node.model_dump(mode="python")
    payload["source_cell_ids"] = (node.source_cell_ids[0], node.source_cell_ids[0])
    with pytest.raises(ValidationError):
        HeaderNode.model_validate(payload)
    hierarchy_payload = table.header_hierarchy.model_dump(mode="python")
    hierarchy_payload["header_row_count"] = 0
    with pytest.raises(ValidationError):
        HeaderHierarchy.model_validate(hierarchy_payload)
    hierarchy_payload = table.header_hierarchy.model_dump(mode="python")
    hierarchy_payload["nodes"] = (node, node)
    with pytest.raises(ValidationError):
        HeaderHierarchy.model_validate(hierarchy_payload)

    check = table.quality.checks[0]
    check_payload = check.model_dump(mode="python")
    check_payload["passed"] = False
    with pytest.raises(ValidationError):
        TableQualityCheck.model_validate(check_payload)
    report_payload = table.quality.model_dump(mode="python")
    report_payload["checks"] = table.quality.checks[:-1]
    with pytest.raises(ValidationError):
        TableQualityReport.model_validate(report_payload)
    report_payload = table.quality.model_dump(mode="python")
    report_payload["passed"] = False
    with pytest.raises(ValidationError):
        TableQualityReport.model_validate(report_payload)


def test_table_and_attempt_contracts_reject_cross_link_drift(
    parsed_result: TableParsingResult,
) -> None:
    table = parsed_result.tables[0]
    payload = table.model_dump(mode="python")
    payload["cells"] = table.cells[:-1]
    with pytest.raises(ValidationError):
        TableIR.model_validate(payload)
    payload = table.model_dump(mode="python")
    payload["cells"] = tuple(reversed(table.cells))
    with pytest.raises(ValidationError):
        TableIR.model_validate(payload)
    payload = table.model_dump(mode="python")
    cells = list(table.cells)
    cells[0] = cells[0].model_copy(update={"role": cells[-1].role})
    payload["cells"] = tuple(cells)
    with pytest.raises(ValidationError):
        TableIR.model_validate(payload)

    attempt = parsed_result.attempts[0]
    payload = attempt.model_dump(mode="python")
    payload["table_ref"] = None
    with pytest.raises(ValidationError):
        TableParseAttempt.model_validate(payload)
    payload = attempt.model_dump(mode="python")
    payload.update(
        {
            "status": TableAttemptStatus.BLOCKED,
            "engine_name": None,
            "engine_version": None,
            "table_ref": None,
            "quality_report_hash": None,
            "error_code": TableGapCode.PARSER_UNAVAILABLE,
            "error_detail": "blocked",
            "network_performed": True,
        }
    )
    with pytest.raises(ValidationError):
        TableParseAttempt.model_validate(payload)


def test_aggregate_contract_rejects_metrics_warnings_and_event_drift(
    parsed_result: TableParsingResult,
) -> None:
    payload = parsed_result.model_dump(mode="python")
    payload["metrics"] = parsed_result.metrics.model_copy(update={"cell_count": 999})
    with pytest.raises(ValidationError):
        TableParsingResult.model_validate(payload)
    payload = parsed_result.model_dump(mode="python")
    payload["warnings"] = ("invented warning",)
    with pytest.raises(ValidationError):
        TableParsingResult.model_validate(payload)
    payload = parsed_result.model_dump(mode="python")
    payload["event"] = parsed_result.event.model_copy(
        update={"payload": parsed_result.event.payload.model_copy(update={"table_count": 99})}
    )
    with pytest.raises(ValidationError):
        TableParsingResult.model_validate(payload)
    payload = parsed_result.model_dump(mode="python")
    route = parsed_result.route_results[0]
    payload["route_results"] = (route.model_copy(update={"attempt_hash": "f" * 64}),)
    with pytest.raises(ValidationError):
        TableParsingResult.model_validate(payload)


def test_table_store_is_content_addressed_and_bounded(
    parsed_result: TableParsingResult,
) -> None:
    table = parsed_result.tables[0]
    store = MemoryTableIRStore()
    first = store.put(table)
    second = store.put(table)
    assert first.newly_stored is True
    assert second.newly_stored is False
    assert store.contains(first.ir_ref.artifact_sha256) is True
    assert store.contains("f" * 64) is False
    with pytest.raises(AppError) as captured:
        store.read("invalid")
    assert captured.value.code is ErrorCode.INVALID_REQUEST
    with pytest.raises(AppError):
        MemoryTableIRStore(max_object_bytes=2, max_total_bytes=1)
    with pytest.raises(AppError) as captured:
        MemoryTableIRStore(max_object_bytes=1, max_total_bytes=1).put(table)
    assert captured.value.code is ErrorCode.VALIDATION_FAILED
    store._objects[first.ir_ref.artifact_sha256] = b"tampered"
    with pytest.raises(AppError) as captured:
        store.read(first.ir_ref.artifact_sha256)
    assert captured.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_checkpoint_replay_is_canonical_and_conflict_preserving(
    parsed_result: TableParsingResult,
) -> None:
    checkpoints = MemoryTableCheckpointStore()
    assert checkpoints.load(parsed_result.idempotency_key) is None
    assert checkpoints.save(parsed_result) == parsed_result
    assert checkpoints.save(parsed_result) == parsed_result
    with pytest.raises(AppError):
        MemoryTableCheckpointStore(max_checkpoint_bytes=0)
    with pytest.raises(AppError):
        checkpoints.load("invalid")
    tiny = MemoryTableCheckpointStore(max_checkpoint_bytes=1)
    with pytest.raises(AppError) as captured:
        tiny.save(parsed_result)
    assert captured.value.code is ErrorCode.VALIDATION_FAILED
    checkpoints._values[parsed_result.idempotency_key] = b"{}"
    with pytest.raises(AppError) as captured:
        checkpoints.load(parsed_result.idempotency_key)
    assert captured.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_self_contained_hash_verifier_rejects_nested_identity_drift(
    parsed_result: TableParsingResult,
) -> None:
    table = parsed_result.tables[0]
    cells = list(table.cells)
    cells[0] = cells[0].model_copy(update={"cell_hash": "f" * 64})
    tampered_table = table.model_copy(update={"cells": tuple(cells)})
    tampered = parsed_result.model_copy(update={"tables": (tampered_table,)})
    with pytest.raises(AppError) as captured:
        verify_table_result_hashes(tampered)
    assert captured.value.code is ErrorCode.ARTIFACT_INTEGRITY_ERROR


def test_descriptor_contract_rejects_extra_fields(chain: _TableChain) -> None:
    descriptor = chain.table_request.runtime.parser_descriptors[0]
    payload = descriptor.model_dump(mode="python")
    payload["secret"] = "not allowed"
    with pytest.raises(ValidationError):
        TableParserRuntimeDescriptor.model_validate(payload)


def test_route_contract_is_strict(parsed_result: TableParsingResult) -> None:
    route = parsed_result.route_results[0]
    payload = route.model_dump(mode="python")
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        TableRouteResult.model_validate(payload)


def test_type_probe_labels_values_without_converting_them() -> None:
    assert _infer_kind("") is TableValueKind.EMPTY
    assert _infer_kind(" true ") is TableValueKind.BOOLEAN_CANDIDATE
    assert _infer_kind("-42") is TableValueKind.INTEGER_CANDIDATE
    assert _infer_kind("NaN") is TableValueKind.TEXT


def test_offline_bundle_rejects_registry_runtime_and_clock_drift(chain: _TableChain) -> None:
    registry = chain.parse_result.plan.capability_registry
    runtime = chain.parse_result.plan.runtime
    with pytest.raises(ValueError, match="parser registry"):
        build_offline_table_parsing_bundle(
            registry.model_copy(update={"registry_hash": "f" * 64}),
            runtime,
        )

    capabilities = list(registry.parsers)
    capabilities[0] = capabilities[0].model_copy(update={"capability_hash": "f" * 64})
    tampered_registry = registry.model_copy(
        update={"parsers": tuple(capabilities), "registry_hash": "0" * 64}
    )
    tampered_registry = tampered_registry.model_copy(
        update={"registry_hash": calculate_parser_registry_hash(tampered_registry)}
    )
    with pytest.raises(ValueError, match="tampered parser capability"):
        build_offline_table_parsing_bundle(
            tampered_registry,
            runtime,
        )

    mismatched = runtime.model_copy(update={"capability_registry_hash": "f" * 64})
    mismatched = mismatched.model_copy(
        update={"runtime_hash": calculate_parse_runtime_hash(mismatched)}
    )
    with pytest.raises(ValueError, match="exact M08 registry"):
        build_offline_table_parsing_bundle(registry, mismatched)

    mock_runtime = runtime.model_copy(
        update={"execution_mode": ParsePlanningExecutionMode.MOCK, "runtime_hash": "0" * 64}
    )
    mock_runtime = mock_runtime.model_copy(
        update={"runtime_hash": calculate_parse_runtime_hash(mock_runtime)}
    )
    with pytest.raises(ValueError, match="rejects M08 model or network"):
        build_offline_table_parsing_bundle(registry, mock_runtime)

    with pytest.raises(ValueError, match="cannot predate"):
        build_offline_table_parsing_bundle(
            registry,
            runtime,
            clock=lambda: runtime.checked_at - timedelta(seconds=1),
        )

    without_csv = registry.model_copy(
        update={
            "parsers": tuple(item for item in registry.parsers if item.parser_id != "m10.csv"),
            "registry_hash": "0" * 64,
        }
    )
    without_csv = without_csv.model_copy(
        update={"registry_hash": calculate_parser_registry_hash(without_csv)}
    )
    matching_runtime = runtime.model_copy(
        update={"capability_registry_hash": without_csv.registry_hash, "runtime_hash": "0" * 64}
    )
    matching_runtime = matching_runtime.model_copy(
        update={"runtime_hash": calculate_parse_runtime_hash(matching_runtime)}
    )
    with pytest.raises(ValueError, match="does not match"):
        build_offline_table_parsing_bundle(
            without_csv,
            matching_runtime,
            clock=lambda: chain.table_request.runtime.checked_at,
        )

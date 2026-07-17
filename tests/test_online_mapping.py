"""M28 reviewable field mapping and evidence-table export tests."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json

import pytest
from pydantic import ValidationError

import scidatafusion.online.structured as structured_module
from scidatafusion.config import Settings
from scidatafusion.contracts.model import (
    ModelInvocationRecord,
    ModelUsage,
    StructuredModelCompletion,
    StructuredModelRequest,
)
from scidatafusion.contracts.online import OnlineAcquiredArtifact
from scidatafusion.online.service import OnlineResearchService
from scidatafusion.online.structured import OnlineStructuredDataService


class _MappingModel:
    def __init__(self, content: str) -> None:
        self.content = content
        self.request: StructuredModelRequest | None = None

    async def complete(self, request: StructuredModelRequest) -> StructuredModelCompletion:
        self.request = request
        return StructuredModelCompletion(
            content=self.content,
            invocation=ModelInvocationRecord(
                region="cn-beijing",
                endpoint_host="dashscope.aliyuncs.com",
                requested_model=request.model_id,
                actual_model=request.model_id,
                role=request.role,
                prompt_version=request.prompt_version,
                schema_name=request.schema_name,
                request_hash="a" * 64,
                response_hash="b" * 64,
                usage=ModelUsage(input_tokens=20, output_tokens=10),
                latency_ms=1.0,
                attempt_count=1,
            ),
        )


def _artifact(payload: bytes) -> OnlineAcquiredArtifact:
    digest = hashlib.sha256(payload).hexdigest()
    return OnlineAcquiredArtifact.model_validate(
        {
            "source_url": "https://data.example.org/current.csv",
            "source_title": "Current topic table",
            "locator_hash": "1" * 64,
            "byte_sha256": digest,
            "size_bytes": len(payload),
            "media_type": "text/csv",
            "artifact_kind": "structured_table",
            "storage_uri": f"bronze://sha256/{digest}",
        }
    )


def _service(model: _MappingModel) -> OnlineResearchService:
    settings = Settings(
        _env_file=None,
        offline_mode=False,
        dashscope_api_key="test-dashscope-key",
        serpapi_api_key="test-serpapi-key",
    )
    return OnlineResearchService(settings, model_client=model)


def test_mapping_uses_only_headers_and_validated_targets() -> None:
    payload = b"city,lst,note\nA,32.1,raw scientific value\n"
    artifact = _artifact(payload)
    structured = OnlineStructuredDataService().parse((artifact,), lambda _: payload)
    model = _MappingModel(
        json.dumps(
            {
                "mappings": [
                    {
                        "artifact_sha256": artifact.byte_sha256,
                        "column_index": 2,
                        "source_column": "lst",
                        "target_field": "surface_temperature",
                        "confidence": 0.92,
                        "rationale": "LST is a standard name abbreviation.",
                    },
                    {
                        "artifact_sha256": artifact.byte_sha256,
                        "column_index": 3,
                        "source_column": "note",
                        "target_field": None,
                        "confidence": 0.2,
                        "rationale": "The name is not semantically specific.",
                    },
                ]
            }
        )
    )

    result = asyncio.run(
        _service(model).map_structured_fields(
            research_goal="Study urban heat and vegetation using reproducible observations.",
            target_fields=("city", "surface_temperature", "vegetation_index"),
            structured_data=structured,
        )
    )

    assert [(item.source_column, item.target_field, item.method) for item in result.decisions] == [
        ("city", "city", "exact"),
        ("lst", "surface_temperature", "qwen"),
        ("note", None, "unmapped"),
    ]
    assert result.mapped_count == 2
    assert model.request is not None
    assert model.request.schema_name == "FieldMappingProposalBatch"
    assert "32.1" not in model.request.user_prompt
    assert "raw scientific value" not in model.request.user_prompt


def test_invalid_model_mapping_fails_closed() -> None:
    payload = b"temperature\n32.1\n"
    artifact = _artifact(payload)
    structured = OnlineStructuredDataService().parse((artifact,), lambda _: payload)
    model = _MappingModel(
        json.dumps(
            {
                "mappings": [
                    {
                        "artifact_sha256": artifact.byte_sha256,
                        "column_index": 1,
                        "source_column": "temperature",
                        "target_field": "invented_target",
                        "confidence": 1.0,
                        "rationale": "Unsupported target.",
                    }
                ]
            }
        )
    )

    result = asyncio.run(
        _service(model).map_structured_fields(
            research_goal="Study urban heat using reproducible temperature observations.",
            target_fields=("city", "surface_temperature", "vegetation_index"),
            structured_data=structured,
        )
    )

    assert result.unmapped_count == 1
    assert result.decisions[0].target_field is None
    assert result.warnings


def test_evidence_csv_preserves_raw_json_and_provenance() -> None:
    payload = b"source,flux\n=cmd,1.20\n"
    artifact = _artifact(payload)
    structured_service = OnlineStructuredDataService()
    structured = structured_service.parse((artifact,), lambda _: payload)
    mapping = asyncio.run(
        _service(_MappingModel("{}")).map_structured_fields(
            research_goal="Study source flux values with complete provenance records.",
            target_fields=("source", "flux", "observation_time"),
            structured_data=structured,
        )
    )

    exported = structured_service.build_evidence_csv((artifact,), lambda _: payload, mapping)
    rows = list(csv.DictReader(io.StringIO(exported.decode("utf-8-sig"))))

    assert len(rows) == 2
    assert rows[0]["raw_value_json"] == '"=cmd"'
    assert rows[1]["raw_value_json"] == '"1.20"'
    assert rows[0]["artifact_sha256"] == artifact.byte_sha256
    assert rows[0]["source_url"] == "https://data.example.org/current.csv"
    assert rows[0]["source_location_json"] == '"第 2 行, 第 1 列"'
    assert rows[0]["evidence_id"].startswith("sev_")


def test_mapping_contracts_reject_conflicting_states() -> None:
    payload = b"source,flux\nA,1.20\n"
    artifact = _artifact(payload)
    structured = OnlineStructuredDataService().parse((artifact,), lambda _: payload)
    result = asyncio.run(
        _service(_MappingModel("{}")).map_structured_fields(
            research_goal="Study source flux values with complete provenance records.",
            target_fields=("source", "flux", "observation_time"),
            structured_data=structured,
        )
    )
    decision_payload = result.decisions[0].model_dump(mode="python")
    decision_payload.update({"status": "mapped", "method": "unmapped", "target_field": None})
    with pytest.raises(ValidationError, match="mapped decisions require"):
        type(result.decisions[0]).model_validate(decision_payload)
    decision_payload.update({"status": "unmapped", "method": "exact", "target_field": "source"})
    with pytest.raises(ValidationError, match="unmapped decisions cannot claim"):
        type(result.decisions[0]).model_validate(decision_payload)

    base = result.model_dump(mode="python")
    variants = []
    variants.append(
        (
            {**base, "decisions": (*base["decisions"], base["decisions"][0])},
            "field mapping decisions must be unique",
        )
    )
    variants.append(({**base, "mapped_count": 0}, "field mapping counts must match"))
    variants.append(
        (
            {**base, "target_fields": ("source", "SOURCE", "other")},
            "target fields must be unique",
        )
    )
    unknown = dict(base["decisions"][0])
    unknown["target_field"] = "unknown"
    variants.append(
        (
            {**base, "decisions": (unknown, base["decisions"][1])},
            "unknown target field",
        )
    )
    collision = dict(base["decisions"][1])
    collision["target_field"] = "source"
    variants.append(
        (
            {**base, "decisions": (base["decisions"][0], collision)},
            "cannot map multiple columns",
        )
    )
    for variant, message in variants:
        with pytest.raises(ValidationError, match=message):
            type(result).model_validate(variant)


def test_evidence_export_rejects_scope_header_and_size_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"source,flux\nA,1.20\n"
    artifact = _artifact(payload)
    structured_service = OnlineStructuredDataService()
    structured = structured_service.parse((artifact,), lambda _: payload)
    mapping = asyncio.run(
        _service(_MappingModel("{}")).map_structured_fields(
            research_goal="Study source flux values with complete provenance records.",
            target_fields=("source", "flux", "observation_time"),
            structured_data=structured,
        )
    )

    unknown = mapping.decisions[0].model_copy(update={"artifact_sha256": "f" * 64})
    with pytest.raises(Exception, match="当前任务之外"):
        structured_service.build_evidence_csv(
            (artifact,), lambda _: payload, mapping.model_copy(update={"decisions": (unknown,)})
        )

    wrong_header = mapping.decisions[0].model_copy(update={"source_column": "changed"})
    with pytest.raises(Exception, match="原始表头不一致"):
        structured_service.build_evidence_csv(
            (artifact,),
            lambda _: payload,
            mapping.model_copy(update={"decisions": (wrong_header, mapping.decisions[1])}),
        )

    extra = mapping.decisions[1].model_copy(update={"column_index": 3})
    with pytest.raises(Exception, match="未导出的列"):
        structured_service.build_evidence_csv(
            (artifact,),
            lambda _: payload,
            mapping.model_copy(update={"decisions": (*mapping.decisions, extra)}),
        )

    monkeypatch.setattr(structured_module, "_MAX_EXPORT_BYTES", 1)
    with pytest.raises(Exception, match="64 MiB"):
        structured_service.build_evidence_csv((artifact,), lambda _: payload, mapping)

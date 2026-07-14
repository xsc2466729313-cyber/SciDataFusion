"""Pure deterministic M20 report and tabular serializers."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass

import polars as pl

from scidatafusion.contracts.fusion import GoldRecordCandidate
from scidatafusion.contracts.quality import FormalGoldDataset, QualityAuditResult
from scidatafusion.contracts.scientific import ScientificDataContract
from scidatafusion.errors import AppError, ErrorCode


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


@dataclass(frozen=True, slots=True)
class TabularExport:
    csv_bytes: bytes
    parquet_bytes: bytes
    row_count: int
    field_names: tuple[str, ...]


class TabularExporter:
    """Serialize exact Formal Gold strings and verify CSV/Parquet equivalence."""

    def export(self, dataset: FormalGoldDataset, contract: ScientificDataContract) -> TabularExport:
        field_names = tuple(item.name for item in contract.fields)
        rows = [_record_row(record, field_names) for record in dataset.records]
        schema = {
            "gold_record_id": pl.String,
            "entity_cluster_id": pl.String,
            **{name: pl.String for name in field_names},
        }
        frame = pl.DataFrame(rows, schema=schema)
        if "observation_time" in field_names:
            frame = frame.sort(
                pl.col("observation_time").cast(pl.Decimal(scale=9), strict=False),
                maintain_order=True,
            )
        csv_bytes = frame.write_csv().encode("utf-8")
        parquet_buffer = io.BytesIO()
        frame.write_parquet(parquet_buffer)
        parquet_bytes = parquet_buffer.getvalue()
        csv_frame = pl.read_csv(io.BytesIO(csv_bytes), schema_overrides=schema)
        parquet_frame = pl.read_parquet(io.BytesIO(parquet_bytes))
        if frame.rows() != csv_frame.rows() or frame.rows() != parquet_frame.rows():
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "M20 CSV and Parquet exports are not value-equivalent",
            )
        return TabularExport(csv_bytes, parquet_bytes, frame.height, field_names)


def _record_row(record: GoldRecordCandidate, field_names: tuple[str, ...]) -> dict[str, str | None]:
    values = {field.field_name: field.value for field in record.fields}
    return {
        "gold_record_id": record.gold_record_id,
        "entity_cluster_id": record.entity_cluster_id,
        **{name: values.get(name) for name in field_names},
    }


class DataDictionaryBuilder:
    """Project the frozen scientific contract without interpreting values."""

    def build(self, contract: ScientificDataContract) -> bytes:
        fields = [
            {
                "name": item.name,
                "data_type": item.data_type.value,
                "requirement": item.requirement.value,
                "nullable": item.nullable,
                "description": item.description,
                "target_unit": item.target_unit,
                "allowed_units": item.allowed_units,
            }
            for item in contract.fields
        ]
        return canonical_json_bytes(
            {
                "contract_id": contract.contract_id,
                "contract_version": contract.version,
                "contract_hash": contract.contract_hash,
                "fields": fields,
            }
        )


class ProvenanceExporter:
    """Emit one immutable lineage row per delivered Formal Gold field."""

    def build(self, dataset: FormalGoldDataset | None) -> tuple[bytes, int]:
        if dataset is None:
            return b"", 0
        lines = []
        for record in dataset.records:
            for field in record.fields:
                lines.append(
                    json.dumps(
                        {
                            "gold_record_id": record.gold_record_id,
                            "field_name": field.field_name,
                            "value_sha256": field.value_sha256,
                            "evidence_ids": field.evidence_ids,
                            "selected_candidate_id": field.selected_candidate_id,
                            "decision_id": field.decision_id,
                        },
                        ensure_ascii=True,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
        return ("\n".join(lines) + ("\n" if lines else "")).encode(), len(lines)


class ReportGenerator:
    """Generate a factual quality report with no LLM-authored claims."""

    def build(self, quality: QualityAuditResult) -> bytes:
        return canonical_json_bytes(
            {
                "status": quality.status.value,
                "quality_gate_passed": quality.quality_report.quality_gate_passed,
                "formal_gold_eligible": quality.quality_report.formal_gold_eligible,
                "quality_score": quality.quality_report.quality_score,
                "gate_count": quality.metrics.gate_count,
                "passed_gate_count": quality.metrics.passed_gate_count,
                "issue_count": quality.metrics.issue_count,
                "issues": [
                    {
                        "issue_id": item.issue_id,
                        "code": item.code.value,
                        "severity": item.severity.value,
                        "affected_fields": item.affected_field_names,
                        "evidence_refs": item.evidence_refs,
                        "suggested_action": item.suggested_action.value,
                    }
                    for item in quality.issue_set.issues
                ],
                "warnings": quality.warnings,
            }
        )

"""Bounded, value-preserving previews for acquired CSV, TSV, and JSON artifacts."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

import polars as pl

from scidatafusion.contracts.online import OnlineAcquiredArtifact
from scidatafusion.contracts.online_mapping import OnlineFieldMappingResult
from scidatafusion.contracts.structured import (
    OnlineStructuredDataResult,
    StructuredCellEvidence,
    StructuredColumnProfile,
    StructuredDatasetPreview,
    StructuredParseFailure,
)
from scidatafusion.domain.registry import canonical_hash
from scidatafusion.errors import AppError, ErrorCode

_MAX_ROWS = 100_000
_MAX_COLUMNS = 128
_MAX_CELLS = 250_000
_MAX_EXPORT_BYTES = 64 * 1024 * 1024
_PREVIEW_ROWS = 20
_PREVIEW_COLUMNS = 20
_MAX_VALUE_JSON_LENGTH = 8_192


class _RawNumber(str):
    """Retain a JSON number's exact lexical representation."""


@dataclass(frozen=True)
class _ParsedTable:
    format: Literal["csv", "tsv", "json"]
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    locations: tuple[tuple[str, ...], ...]


class _StructuredParseError(ValueError):
    def __init__(
        self,
        code: Literal[
            "unsupported_media_type",
            "hash_mismatch",
            "invalid_encoding",
            "invalid_structure",
            "limit_exceeded",
        ],
        detail: str,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class OnlineStructuredDataService:
    """Create read-only previews without semantic mapping or scientific value repair."""

    def parse(
        self,
        artifacts: tuple[OnlineAcquiredArtifact, ...],
        reader: Callable[[str], bytes],
    ) -> OnlineStructuredDataResult:
        datasets: list[StructuredDatasetPreview] = []
        failures: list[StructuredParseFailure] = []
        for artifact in artifacts:
            try:
                payload = reader(artifact.byte_sha256)
                datasets.append(self._parse_artifact(artifact, payload))
            except _StructuredParseError as exc:
                failures.append(
                    StructuredParseFailure(
                        artifact_sha256=artifact.byte_sha256,
                        source_url=artifact.source_url,
                        media_type=artifact.media_type,
                        code=exc.code,
                        detail=exc.detail,
                    )
                )
            except (csv.Error, json.JSONDecodeError, UnicodeError, ValueError):
                failures.append(
                    StructuredParseFailure(
                        artifact_sha256=artifact.byte_sha256,
                        source_url=artifact.source_url,
                        media_type=artifact.media_type,
                        code="invalid_structure",
                        detail="文件不符合受支持的矩形记录结构。",
                    )
                )
        return OnlineStructuredDataResult(
            attempted_count=len(artifacts),
            datasets=tuple(datasets),
            failures=tuple(failures),
        )

    def _parse_artifact(
        self, artifact: OnlineAcquiredArtifact, payload: bytes
    ) -> StructuredDatasetPreview:
        return _build_preview(artifact, self._parse_table(artifact, payload))

    def build_evidence_csv(
        self,
        artifacts: tuple[OnlineAcquiredArtifact, ...],
        reader: Callable[[str], bytes],
        mappings: OnlineFieldMappingResult,
    ) -> bytes:
        """Export every accepted cell as a provenance-rich row without value mutation."""

        artifact_by_hash = {item.byte_sha256: item for item in artifacts}
        decisions = {(item.artifact_sha256, item.column_index): item for item in mappings.decisions}
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(
            (
                "dataset_id",
                "source_url",
                "artifact_sha256",
                "source_row",
                "source_column_index",
                "source_column_json",
                "target_field_json",
                "mapping_status",
                "mapping_method",
                "raw_value_json",
                "evidence_id",
                "source_location_json",
            )
        )
        exported_decisions: set[tuple[str, int]] = set()
        for artifact_hash in dict.fromkeys(item.artifact_sha256 for item in mappings.decisions):
            artifact = artifact_by_hash.get(artifact_hash)
            if artifact is None:
                raise AppError(
                    ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                    "字段映射引用了当前任务之外的原始文件。",
                )
            parsed = self._parse_table(artifact, reader(artifact_hash))
            dataset = _build_preview(artifact, parsed)
            for column_index, source_column in enumerate(parsed.columns, start=1):
                decision = decisions.get((artifact_hash, column_index))
                if decision is None or decision.source_column != source_column:
                    raise AppError(
                        ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                        "字段映射与重新校验后的原始表头不一致。",
                    )
                exported_decisions.add((artifact_hash, column_index))
                for row_index, row in enumerate(parsed.rows, start=1):
                    evidence_id = _cell_evidence_id(artifact_hash, row_index, column_index)
                    writer.writerow(
                        (
                            dataset.dataset_id,
                            str(artifact.source_url),
                            artifact_hash,
                            row_index,
                            column_index,
                            _json_string(source_column),
                            "null"
                            if decision.target_field is None
                            else _json_string(decision.target_field),
                            decision.status,
                            decision.method,
                            row[column_index - 1],
                            evidence_id,
                            _json_string(parsed.locations[row_index - 1][column_index - 1]),
                        )
                    )
                    if output.tell() > _MAX_EXPORT_BYTES:
                        raise AppError(
                            ErrorCode.BUDGET_EXCEEDED,
                            "多源证据表超过 64 MiB 导出上限, 请缩小研究范围。",
                        )
        if exported_decisions != set(decisions):
            raise AppError(
                ErrorCode.ARTIFACT_INTEGRITY_ERROR,
                "字段映射包含未导出的列。",
            )
        return output.getvalue().encode("utf-8-sig")

    def _parse_table(self, artifact: OnlineAcquiredArtifact, payload: bytes) -> _ParsedTable:
        if hashlib.sha256(payload).hexdigest() != artifact.byte_sha256:
            raise _StructuredParseError("hash_mismatch", "Bronze 文件内容哈希校验失败。")
        format_name = _detect_format(artifact)
        if format_name is None:
            raise _StructuredParseError(
                "unsupported_media_type", "当前仅解析 CSV、TSV 和标量记录 JSON。"
            )
        try:
            text = payload.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as exc:
            raise _StructuredParseError("invalid_encoding", "文件不是有效的 UTF-8 文本。") from exc
        if format_name == "csv":
            parsed = _parse_delimited(text, "csv")
        elif format_name == "tsv":
            parsed = _parse_delimited(text, "tsv")
        else:
            parsed = _parse_json_records(text)
        return parsed


def _detect_format(
    artifact: OnlineAcquiredArtifact,
) -> Literal["csv", "tsv", "json"] | None:
    media_type = artifact.media_type.casefold().split(";", 1)[0].strip()
    if media_type == "text/csv":
        return "csv"
    if media_type == "text/tab-separated-values":
        return "tsv"
    if media_type in {"application/json", "application/geo+json"}:
        return "json"
    suffix = PurePosixPath(urlsplit(str(artifact.source_url)).path).suffix.casefold()
    if suffix == ".csv":
        return "csv"
    if suffix == ".tsv":
        return "tsv"
    if suffix in {".json", ".geojson"}:
        return "json"
    return None


def _parse_delimited(text: str, format_name: Literal["csv", "tsv"]) -> _ParsedTable:
    delimiter = "," if format_name == "csv" else "\t"
    records = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True))
    if not records:
        raise _StructuredParseError("invalid_structure", "文件没有表头。")
    columns = tuple(records[0])
    _validate_columns(columns)
    data_rows = records[1:]
    if len(data_rows) > _MAX_ROWS:
        raise _StructuredParseError("limit_exceeded", "记录数超过 100000 行安全上限。")
    _validate_cell_count(len(data_rows), len(columns))
    if any(len(row) != len(columns) for row in data_rows):
        raise _StructuredParseError("invalid_structure", "数据行列数与表头不一致。")
    rows = tuple(tuple(_json_string(value) for value in row) for row in data_rows)
    locations = tuple(
        tuple(
            f"第 {row_index + 2} 行, 第 {column_index + 1} 列"
            for column_index in range(len(columns))
        )
        for row_index in range(len(rows))
    )
    return _ParsedTable(format=format_name, columns=columns, rows=rows, locations=locations)


def _parse_json_records(text: str) -> _ParsedTable:
    payload = json.loads(
        text,
        parse_int=_RawNumber,
        parse_float=_RawNumber,
        parse_constant=lambda value: _raise_invalid_json_number(value),
    )
    records, prefix = _select_json_records(payload)
    if len(records) > _MAX_ROWS:
        raise _StructuredParseError("limit_exceeded", "记录数超过 100000 行安全上限。")
    if any(not isinstance(item, dict) for item in records):
        raise _StructuredParseError("invalid_structure", "JSON 记录必须全部为对象。")
    columns = _ordered_json_columns(records)
    _validate_columns(columns)
    _validate_cell_count(len(records), len(columns))
    rows: list[tuple[str, ...]] = []
    locations: list[tuple[str, ...]] = []
    for row_index, record in enumerate(records):
        values: list[str] = []
        value_locations: list[str] = []
        for column in columns:
            value = record.get(column)
            values.append(_json_scalar(value))
            value_locations.append(f"{prefix}[{row_index}].{column}")
        rows.append(tuple(values))
        locations.append(tuple(value_locations))
    return _ParsedTable(
        format="json",
        columns=columns,
        rows=tuple(rows),
        locations=tuple(locations),
    )


def _select_json_records(payload: Any) -> tuple[list[Any], str]:
    if isinstance(payload, list):
        return payload, "$"
    if isinstance(payload, dict):
        if payload.get("type") == "FeatureCollection" and isinstance(payload.get("features"), list):
            features = payload["features"]
            if any(
                not isinstance(item, dict) or not isinstance(item.get("properties"), dict)
                for item in features
            ):
                raise _StructuredParseError(
                    "invalid_structure", "GeoJSON features 必须包含对象类型 properties。"
                )
            return [item["properties"] for item in features], "$.features[*].properties"
        for key in ("records", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value, f"$.{key}"
    raise _StructuredParseError(
        "invalid_structure", "JSON 必须是记录数组或 records/data/results 记录容器。"
    )


def _ordered_json_columns(records: Iterable[dict[Any, Any]]) -> tuple[str, ...]:
    columns: list[str] = []
    seen: set[str] = set()
    for record in records:
        for key in record:
            if not isinstance(key, str):
                raise _StructuredParseError("invalid_structure", "JSON 字段名必须是字符串。")
            if key not in seen:
                seen.add(key)
                columns.append(key)
    return tuple(columns)


def _validate_columns(columns: tuple[str, ...]) -> None:
    if not columns:
        raise _StructuredParseError("invalid_structure", "结构化文件没有字段。")
    if len(columns) > _MAX_COLUMNS:
        raise _StructuredParseError("limit_exceeded", "字段数超过 128 列安全上限。")
    if any(not name or name != name.strip() or len(name) > 512 for name in columns):
        raise _StructuredParseError("invalid_structure", "字段名为空、过长或包含首尾空白。")
    if len(columns) != len(set(columns)):
        raise _StructuredParseError("invalid_structure", "字段名不能重复。")


def _validate_cell_count(row_count: int, column_count: int) -> None:
    if row_count * column_count > _MAX_CELLS:
        raise _StructuredParseError("limit_exceeded", "单个文件超过 250000 个单元格安全上限。")


def _json_string(value: str) -> str:
    return _bounded_json(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _json_scalar(value: Any) -> str:
    if isinstance(value, _RawNumber):
        return _bounded_json(str(value))
    if value is None or isinstance(value, (str, bool)):
        return _bounded_json(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    raise _StructuredParseError(
        "invalid_structure", "记录单元格必须是字符串、数字、布尔值或 null。"
    )


def _bounded_json(value: str) -> str:
    if len(value) > _MAX_VALUE_JSON_LENGTH:
        raise _StructuredParseError("limit_exceeded", "单元格值超过 8192 字符预览上限。")
    return value


def _raise_invalid_json_number(value: str) -> None:
    raise _StructuredParseError("invalid_structure", f"JSON 数值 {value} 不是有限标准数值。")


def _build_preview(
    artifact: OnlineAcquiredArtifact, parsed: _ParsedTable
) -> StructuredDatasetPreview:
    # Polars owns the rectangular runtime representation while every value remains JSON encoded.
    frame = pl.DataFrame(
        {name: [row[index] for row in parsed.rows] for index, name in enumerate(parsed.columns)},
        schema={name: pl.String for name in parsed.columns},
        strict=True,
    )
    preview_row_count = min(frame.height, _PREVIEW_ROWS)
    preview_column_count = min(frame.width, _PREVIEW_COLUMNS)
    columns = tuple(
        StructuredColumnProfile(
            name=name,
            column_index=index + 1,
            non_empty_count=sum(row[index] not in {'""', "null"} for row in parsed.rows),
            empty_count=sum(row[index] == '""' for row in parsed.rows),
            null_count=sum(row[index] == "null" for row in parsed.rows),
        )
        for index, name in enumerate(parsed.columns)
    )
    cells = tuple(
        StructuredCellEvidence(
            evidence_id=_cell_evidence_id(artifact.byte_sha256, row_index + 1, column_index + 1),
            row_index=row_index + 1,
            column_index=column_index + 1,
            column_name=parsed.columns[column_index],
            raw_value_json=parsed.rows[row_index][column_index],
            source_location=parsed.locations[row_index][column_index],
            source_hash=artifact.byte_sha256,
        )
        for row_index in range(preview_row_count)
        for column_index in range(preview_column_count)
    )
    identity = {
        "artifact_sha256": artifact.byte_sha256,
        "format": parsed.format,
        "parser_id": "polars-structured-preview",
        "parser_version": pl.__version__,
        "row_count": frame.height,
        "columns": [item.model_dump(mode="json") for item in columns],
        "cells": [item.model_dump(mode="json") for item in cells],
    }
    dataset_hash = canonical_hash(identity)
    return StructuredDatasetPreview(
        dataset_id=f"sds_{dataset_hash[:32]}",
        artifact_sha256=artifact.byte_sha256,
        source_url=artifact.source_url,
        media_type=artifact.media_type,
        format=parsed.format,
        parser_version=pl.__version__,
        row_count=frame.height,
        column_count=frame.width,
        preview_row_count=preview_row_count,
        preview_column_count=preview_column_count,
        truncated=preview_row_count < frame.height or preview_column_count < frame.width,
        columns=columns,
        cells=cells,
        dataset_hash=dataset_hash,
    )


def _cell_evidence_id(artifact_sha256: str, row_index: int, column_index: int) -> str:
    digest = canonical_hash({"artifact": artifact_sha256, "row": row_index, "column": column_index})
    return f"sev_{digest[:32]}"

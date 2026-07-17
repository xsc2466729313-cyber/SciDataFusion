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
from scidatafusion.contracts.structured import (
    OnlineStructuredDataResult,
    StructuredCellEvidence,
    StructuredColumnProfile,
    StructuredDatasetPreview,
    StructuredParseFailure,
)
from scidatafusion.domain.registry import canonical_hash

_MAX_ROWS = 100_000
_MAX_COLUMNS = 128
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
        return _build_preview(artifact, parsed)


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
            evidence_id=f"sev_{canonical_hash({'artifact': artifact.byte_sha256, 'row': row_index + 1, 'column': column_index + 1})[:32]}",
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

"""Strict contracts for bounded previews of current-topic structured artifacts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, HttpUrl, StringConstraints, model_validator

from scidatafusion.contracts.base import ContentHash, SemanticVersion, StrictContract

StructuredText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)
]
RawValueJson = Annotated[str, StringConstraints(min_length=1, max_length=8_192)]


class StructuredColumnProfile(StrictContract):
    name: StructuredText
    column_index: int = Field(ge=1, le=128)
    non_empty_count: int = Field(ge=0)
    empty_count: int = Field(ge=0)
    null_count: int = Field(ge=0)


class StructuredCellEvidence(StrictContract):
    evidence_id: Annotated[str, StringConstraints(pattern=r"^sev_[0-9a-f]{32}$")]
    row_index: int = Field(ge=1)
    column_index: int = Field(ge=1, le=128)
    column_name: StructuredText
    raw_value_json: RawValueJson
    source_location: StructuredText
    source_hash: ContentHash


class StructuredDatasetPreview(StrictContract):
    dataset_id: Annotated[str, StringConstraints(pattern=r"^sds_[0-9a-f]{32}$")]
    artifact_sha256: ContentHash
    source_url: HttpUrl
    media_type: StructuredText
    format: Literal["csv", "tsv", "json"]
    parser_id: Literal["polars-structured-preview"] = "polars-structured-preview"
    parser_version: SemanticVersion
    row_count: int = Field(ge=0, le=100_000)
    column_count: int = Field(ge=1, le=128)
    preview_row_count: int = Field(ge=0, le=20)
    preview_column_count: int = Field(ge=1, le=20)
    truncated: bool
    columns: tuple[StructuredColumnProfile, ...] = Field(min_length=1, max_length=128)
    cells: tuple[StructuredCellEvidence, ...] = Field(max_length=400)
    dataset_hash: ContentHash

    @model_validator(mode="after")
    def preview_is_rectangular_and_bounded(self) -> StructuredDatasetPreview:
        names = [item.name for item in self.columns]
        indexes = [item.column_index for item in self.columns]
        if len(names) != len(set(names)) or indexes != list(range(1, len(indexes) + 1)):
            raise ValueError("structured columns must be unique and ordered")
        if self.column_count != len(self.columns):
            raise ValueError("column count must match profiles")
        if self.preview_row_count > self.row_count:
            raise ValueError("preview rows cannot exceed total rows")
        expected_cells = self.preview_row_count * self.preview_column_count
        if len(self.cells) != expected_cells:
            raise ValueError("structured preview must be rectangular")
        allowed_names = set(names[: self.preview_column_count])
        if any(
            item.row_index > self.preview_row_count
            or item.column_index > self.preview_column_count
            or item.column_name not in allowed_names
            or item.source_hash != self.artifact_sha256
            for item in self.cells
        ):
            raise ValueError("structured cell falls outside the preview boundary")
        expected_truncated = (
            self.preview_row_count < self.row_count or self.preview_column_count < self.column_count
        )
        if self.truncated != expected_truncated:
            raise ValueError("truncation flag must match preview dimensions")
        return self


class StructuredParseFailure(StrictContract):
    artifact_sha256: ContentHash
    source_url: HttpUrl
    media_type: StructuredText
    code: Literal[
        "unsupported_media_type",
        "hash_mismatch",
        "invalid_encoding",
        "invalid_structure",
        "limit_exceeded",
    ]
    detail: StructuredText


class OnlineStructuredDataResult(StrictContract):
    policy_version: Literal["1.0.0"] = "1.0.0"
    attempted_count: int = Field(ge=0, le=20)
    datasets: tuple[StructuredDatasetPreview, ...] = Field(max_length=20)
    failures: tuple[StructuredParseFailure, ...] = Field(max_length=20)

    @model_validator(mode="after")
    def attempts_are_fully_accounted_for(self) -> OnlineStructuredDataResult:
        if self.attempted_count != len(self.datasets) + len(self.failures):
            raise ValueError("structured parsing attempts must be fully accounted for")
        hashes = [item.artifact_sha256 for item in self.datasets]
        if len(hashes) != len(set(hashes)):
            raise ValueError("structured datasets must reference unique artifacts")
        return self

"""Strict contracts for reviewable current-topic field mapping."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from scidatafusion.contracts.base import ContentHash, StrictContract
from scidatafusion.contracts.model import ModelInvocationRecord

MappingText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]


class FieldMappingProposal(StrictContract):
    """One untrusted model proposal; validation never grants it evidence status."""

    artifact_sha256: ContentHash
    column_index: int = Field(ge=1, le=128)
    source_column: MappingText
    target_field: MappingText | None
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    rationale: MappingText


class FieldMappingProposalBatch(StrictContract):
    mappings: tuple[FieldMappingProposal, ...] = Field(max_length=2_560)


class FieldMappingDecision(StrictContract):
    mapping_id: Annotated[str, StringConstraints(pattern=r"^sfm_[0-9a-f]{32}$")]
    dataset_id: Annotated[str, StringConstraints(pattern=r"^sds_[0-9a-f]{32}$")]
    artifact_sha256: ContentHash
    column_index: int = Field(ge=1, le=128)
    source_column: MappingText
    target_field: MappingText | None
    status: Literal["mapped", "unmapped"]
    method: Literal["exact", "qwen", "unmapped"]
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    rationale: MappingText
    evidence_ids: tuple[Annotated[str, StringConstraints(pattern=r"^sev_[0-9a-f]{32}$")], ...] = (
        Field(max_length=20)
    )

    @model_validator(mode="after")
    def status_matches_target(self) -> FieldMappingDecision:
        if self.status == "mapped" and (self.target_field is None or self.method == "unmapped"):
            raise ValueError("mapped decisions require a target and mapping method")
        if self.status == "unmapped" and (
            self.target_field is not None or self.method != "unmapped"
        ):
            raise ValueError("unmapped decisions cannot claim a target")
        return self


class OnlineFieldMappingResult(StrictContract):
    """A complete, auditable decision for every parsed source column."""

    policy_version: Literal["1.0.0"] = "1.0.0"
    target_fields: tuple[MappingText, ...] = Field(min_length=3, max_length=12)
    decisions: tuple[FieldMappingDecision, ...] = Field(max_length=2_560)
    mapped_count: int = Field(ge=0, le=2_560)
    unmapped_count: int = Field(ge=0, le=2_560)
    model_invocation: ModelInvocationRecord | None
    warnings: tuple[MappingText, ...] = Field(max_length=8)

    @model_validator(mode="after")
    def decisions_are_unique_and_accounted_for(self) -> OnlineFieldMappingResult:
        keys = [(item.artifact_sha256, item.column_index) for item in self.decisions]
        if len(keys) != len(set(keys)):
            raise ValueError("field mapping decisions must be unique by artifact and column")
        mapped = sum(item.status == "mapped" for item in self.decisions)
        if mapped != self.mapped_count or len(self.decisions) - mapped != self.unmapped_count:
            raise ValueError("field mapping counts must match decisions")
        targets = {item.casefold() for item in self.target_fields}
        if len(targets) != len(self.target_fields):
            raise ValueError("target fields must be unique")
        if any(
            item.target_field is not None and item.target_field.casefold() not in targets
            for item in self.decisions
        ):
            raise ValueError("mapping decision references an unknown target field")
        mapped_targets = [
            (item.dataset_id, item.target_field.casefold())
            for item in self.decisions
            if item.target_field is not None
        ]
        if len(mapped_targets) != len(set(mapped_targets)):
            raise ValueError("one dataset cannot map multiple columns to the same target")
        return self

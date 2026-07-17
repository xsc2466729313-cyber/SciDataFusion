"""Strict product-facing projection of the complete scientific-data workflow."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from scidatafusion.contracts.base import ContentHash, StrictContract
from scidatafusion.contracts.online import (
    AgentReflectionTrace,
    AutomatedQualityReview,
    OnlineAcquisitionResult,
    OnlineResearchResult,
    ResearchExecutionMode,
    ResearchExplorationProfile,
)
from scidatafusion.contracts.online_mapping import OnlineFieldMappingResult
from scidatafusion.contracts.structured import OnlineStructuredDataResult

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]
DetailText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024)]


class WorkbenchStage(StrictContract):
    key: Literal["goal", "discover", "parse", "integrate", "quality", "deliver"]
    label: ShortText
    status: Literal["complete", "review", "blocked"]
    primary_count: int = Field(ge=0)
    count_label: ShortText
    detail: DetailText


class WorkbenchSource(StrictContract):
    candidate_id: str
    rank: int = Field(ge=1)
    source_names: tuple[ShortText, ...]
    categories: tuple[ShortText, ...]
    covered_fields: tuple[str, ...]
    license_status: str
    download_status: str
    primary: bool
    score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)


class WorkbenchArtifact(StrictContract):
    object_id: str
    format: str
    media_type: str
    size_bytes: int = Field(ge=0)
    disposition: str
    parser: str | None
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    sha256: ContentHash


class WorkbenchField(StrictContract):
    name: str
    label: ShortText
    requirement: str
    data_type: str
    target_unit: str | None
    raw_value: str | None
    normalized_value: str | None
    selected_value: str | None
    mapping_method: str | None
    mapping_score: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    decision: str
    evidence_ids: tuple[str, ...]
    issue_count: int = Field(ge=0)


class WorkbenchEvidence(StrictContract):
    evidence_id: str
    field_name: str
    raw_value: str
    source_location: ShortText
    byte_range: ShortText
    method: str
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    source_hash: ContentHash


class WorkbenchGate(StrictContract):
    gate_id: str
    label: ShortText
    fields: tuple[str, ...]
    score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    threshold: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    passed: bool
    blocking: bool


class WorkbenchIssue(StrictContract):
    issue_id: str
    code: str
    severity: str
    fields: tuple[str, ...]
    detail: DetailText
    action: str
    evidence_count: int = Field(ge=0)


class WorkbenchHit(StrictContract):
    source_id: str
    location: ShortText
    sparse_score: float = Field(ge=0.0, allow_inf_nan=False)
    graph_score: float = Field(ge=0.0, allow_inf_nan=False)
    final_score: float = Field(ge=0.0, allow_inf_nan=False)


class WorkbenchGraphNode(StrictContract):
    node_id: str
    kind: str
    source_id: str
    label: ShortText
    trusted: bool


class WorkbenchGraphEdge(StrictContract):
    source: str
    target: str
    kind: str
    evidence_refs: tuple[str, ...]


class WorkbenchChartPoint(StrictContract):
    x: str
    y: str
    error_x: str
    error_y: str


class WorkbenchReviewAutomation(StrictContract):
    """Cost-aware review routing with immutable proof references."""

    policy_version: Literal["1.0.0"] = "1.0.0"
    automatic_item_count: int = Field(ge=0)
    evidence_wait_count: int = Field(ge=0)
    human_review_count: int = Field(ge=0)
    ai_assessment_performed: bool
    proof_hashes: tuple[ContentHash, ...] = Field(max_length=32)


class WorkbenchScientificDataset(StrictContract):
    format: str
    parser_id: str
    engine_name: str
    hdu_index: int = Field(ge=0)
    variable_names: tuple[str, ...]
    selected_row_count: int = Field(ge=0)
    materialized_cell_count: int = Field(ge=0)
    missing_value_count: int = Field(ge=0)
    transformation_count: int = Field(ge=0)
    input_byte_count: int = Field(ge=0)
    dataset_hash: ContentHash


class WorkbenchSnapshot(StrictContract):
    execution_mode: ResearchExecutionMode
    research_goal: DetailText
    retrieval_query: DetailText
    research_blueprint: ResearchExplorationProfile
    topic_data_status: Literal["reference_demo", "live_discovery"]
    task_id: str
    run_id: str
    contract_id: str
    status: str
    quality_score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    quality_gate_passed: bool
    stages: tuple[WorkbenchStage, ...]
    sources: tuple[WorkbenchSource, ...]
    artifacts: tuple[WorkbenchArtifact, ...]
    fields: tuple[WorkbenchField, ...]
    evidence: tuple[WorkbenchEvidence, ...]
    gates: tuple[WorkbenchGate, ...]
    issues: tuple[WorkbenchIssue, ...]
    hits: tuple[WorkbenchHit, ...]
    graph_nodes: tuple[WorkbenchGraphNode, ...]
    graph_edges: tuple[WorkbenchGraphEdge, ...]
    chart_points: tuple[WorkbenchChartPoint, ...]
    scientific_dataset: WorkbenchScientificDataset | None
    online_research: OnlineResearchResult | None
    online_acquisition: OnlineAcquisitionResult | None = None
    online_structured_data: OnlineStructuredDataResult | None = None
    online_field_mapping: OnlineFieldMappingResult | None = None
    agent_reflection: AgentReflectionTrace | None = None
    automated_quality_review: AutomatedQualityReview | None = None
    review_automation: WorkbenchReviewAutomation
    delivery_artifact_count: int = Field(ge=0)
    package_filename: str
    formal_gold_available: bool

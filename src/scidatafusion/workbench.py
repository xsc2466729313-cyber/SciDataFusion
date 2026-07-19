"""Build the Chinese workbench's complete, evidence-backed product projection."""

from __future__ import annotations

import hashlib
import re
from itertools import pairwise
from typing import Literal

from scidatafusion.contracts.datasets import ScientificParsingRequest, ScientificParsingResult
from scidatafusion.contracts.delivery import DeliveryResult
from scidatafusion.contracts.extraction import (
    ExtractedFieldCandidate,
    ExtractionRequest,
    ExtractionResult,
)
from scidatafusion.contracts.figures import FigureDigitizationResult
from scidatafusion.contracts.fusion import FusedField
from scidatafusion.contracts.knowledge import KnowledgeRequest, KnowledgeResult
from scidatafusion.contracts.mapping import FieldMapping
from scidatafusion.contracts.normalization import NormalizedField
from scidatafusion.contracts.online import (
    AgentReflectionTrace,
    AutomatedQualityReview,
    OnlineAcquisitionResult,
    OnlineResearchResult,
    ResearchExecutionMode,
    ResearchExplorationProfile,
)
from scidatafusion.contracts.online_mapping import OnlineFieldMappingResult
from scidatafusion.contracts.quality import QualityAuditResult
from scidatafusion.contracts.scientific import FieldContract
from scidatafusion.contracts.structured import OnlineStructuredDataResult
from scidatafusion.contracts.workbench import (
    WorkbenchArtifact,
    WorkbenchChartPoint,
    WorkbenchEvidence,
    WorkbenchField,
    WorkbenchGate,
    WorkbenchGraphEdge,
    WorkbenchGraphNode,
    WorkbenchHit,
    WorkbenchIssue,
    WorkbenchReviewAutomation,
    WorkbenchScientificDataset,
    WorkbenchSnapshot,
    WorkbenchSource,
    WorkbenchStage,
)
from scidatafusion.exploration import build_fallback_exploration_profile

_FIELD_LABELS = {
    "object_id": "天体编号",
    "source_record_id": "来源记录编号",
    "observation_time": "观测时间",
    "band": "观测波段",
    "magnitude": "星等",
    "flux": "流量",
}
_GATE_LABELS = {
    "photometric_value_present": "光度值完整性",
    "required_fields_complete": "必填字段完整性",
    "required_field_provenance": "必填字段证据链",
}


def build_workbench_snapshot(
    *,
    research_goal: str,
    retrieval_query: str,
    request: KnowledgeRequest,
    knowledge: KnowledgeResult,
    figure: FigureDigitizationResult,
    scientific_request: ScientificParsingRequest,
    scientific: ScientificParsingResult,
    delivery: DeliveryResult,
    execution_mode: ResearchExecutionMode = ResearchExecutionMode.OFFLINE,
    online_research: OnlineResearchResult | None = None,
    online_acquisition: OnlineAcquisitionResult | None = None,
    online_structured_data: OnlineStructuredDataResult | None = None,
    online_field_mapping: OnlineFieldMappingResult | None = None,
    agent_reflection: AgentReflectionTrace | None = None,
    automated_quality_review: AutomatedQualityReview | None = None,
) -> WorkbenchSnapshot:
    """Project immutable workflow artifacts into a bounded UI read model."""

    quality_request = request.quality_request
    quality = request.quality_result
    fusion_request = quality_request.fusion_request
    fusion = quality_request.fusion_result
    entity_request = fusion_request.entity_request
    normalization = entity_request.normalization_result
    mapping = entity_request.normalization_request.mapping_result
    extraction_request = entity_request.normalization_request.mapping_request.extraction_request
    extraction = entity_request.normalization_request.mapping_request.extraction_result
    parse_request = extraction_request.table_parsing_request.parse_planning_request
    parse_result = extraction_request.table_parsing_request.parse_planning_result
    contract = extraction_request.contract
    selected = parse_request.download_request.selected_source_set
    download = parse_request.download_result
    gold_chart_points = _gold_chart_points(quality, extraction_request, extraction)

    evidence_field: dict[str, str] = {}
    candidate_by_field = {item.field_name: item for item in extraction.candidate_set.candidates}
    for candidate in extraction.candidate_set.candidates:
        for evidence_id in candidate.evidence_ids:
            evidence_field[evidence_id] = candidate.field_name
    mappings = {item.target_field_name: item for item in mapping.mapping_set.mappings}
    normalized_fields = {
        item.field_name: item
        for record in normalization.record_set.records
        for item in record.fields
    }
    fused_fields = {
        item.field_name: item
        for record in fusion.fused_record_set.records
        for item in record.fields
    }
    fields = tuple(
        _field_view(
            field,
            candidate_by_field.get(field.name),
            mappings.get(field.name),
            normalized_fields.get(field.name),
            fused_fields.get(field.name),
        )
        for field in contract.fields
    )

    routes = {item.object_id: item for item in parse_result.plan.routes}
    classifications = {item.object_id: item for item in parse_result.plan.classifications}
    routed_artifacts = tuple(
        WorkbenchArtifact(
            object_id=item.object_id,
            format=classifications[item.object_id].format_family.value,
            media_type=classifications[item.object_id].classified_media_type,
            size_bytes=item.size_bytes,
            disposition=routes[item.object_id].disposition.value,
            parser=routes[item.object_id].primary_parser_id,
            confidence=routes[item.object_id].confidence,
            sha256=item.byte_sha256,
        )
        for item in parse_result.plan.source_objects
    )
    artifacts = (
        *routed_artifacts,
        WorkbenchArtifact(
            object_id=scientific_request.artifact.object_id,
            format=scientific_request.artifact.format.value,
            media_type=scientific_request.artifact.media_type,
            size_bytes=scientific_request.artifact.size_bytes,
            disposition="parse",
            parser=scientific_request.artifact.parser_id,
            confidence=1.0,
            sha256=scientific_request.artifact.byte_sha256,
        ),
    )
    report = quality.quality_report
    blueprint = (
        online_research.search_plan.profile
        if online_research is not None
        else build_fallback_exploration_profile(research_goal)
    )
    live_discovery = execution_mode is ResearchExecutionMode.ONLINE
    stages = (
        _live_discovery_stages(
            blueprint,
            online_research,
            online_acquisition,
            online_structured_data,
            online_field_mapping,
        )
        if live_discovery
        else (
            WorkbenchStage(
                key="goal",
                label="研究需求",
                status="complete",
                primary_count=len(contract.fields),
                count_label="目标字段",
                detail="研究目标已转为可验证的数据合同, 字段、来源类型和质量门均已冻结。",
            ),
            WorkbenchStage(
                key="discover",
                label="多源发现",
                status="complete",
                primary_count=len(selected.sources),
                count_label="选定来源",
                detail=f"从候选结果中选定 {len(selected.sources)} 个互补来源并保存 {len(download.artifact_set.objects)} 个不可变原始产物。",
            ),
            WorkbenchStage(
                key="parse",
                label="解析提取",
                status="complete",
                primary_count=len(extraction.evidence_set.atoms),
                count_label="字段证据",
                detail="正文、表格和附件按格式路由; 当前可用表格已完成单元格级证据提取。",
            ),
            WorkbenchStage(
                key="integrate",
                label="清洗整合",
                status="review" if fusion.metrics.withheld_field_count else "complete",
                primary_count=fusion.metrics.selected_field_count,
                count_label="已选字段",
                detail=f"完成字段映射、确定性规范化和实体聚合; {fusion.metrics.withheld_field_count} 个字段因证据不足被保留待审。",
            ),
            WorkbenchStage(
                key="quality",
                label="质量校验",
                status="complete" if report.quality_gate_passed else "blocked",
                primary_count=report.passed_gate_count,
                count_label=f"通过 / {report.gate_count} 门",
                detail="所有质量结论均绑定证据; 未通过的阻断门不会被静默绕过。",
            ),
            WorkbenchStage(
                key="deliver",
                label="成果交付",
                status="complete" if report.formal_gold_eligible else "review",
                primary_count=delivery.metrics.artifact_count,
                count_label="交付文件",
                detail="已生成数据字典、证据图、质量报告和复现包; 正式数据表仅在质量门通过后开放。",
            ),
        )
    )
    graph_nodes, graph_edges = (
        _exploration_graph(blueprint, online_research, online_structured_data, online_field_mapping)
        if live_discovery
        else (
            tuple(
                WorkbenchGraphNode(
                    node_id=item.node_id,
                    kind=item.kind.value,
                    source_id=item.source_id,
                    label=_chinese_graph_label(item.kind.value, item.label),
                    trusted=item.trusted_fact,
                )
                for item in knowledge.graph.nodes
            ),
            tuple(
                WorkbenchGraphEdge(
                    source=item.source_node_id,
                    target=item.target_node_id,
                    kind=item.kind.value,
                    evidence_refs=item.evidence_refs,
                )
                for item in knowledge.graph.edges
            ),
        )
    )
    return WorkbenchSnapshot(
        execution_mode=execution_mode,
        research_goal=research_goal,
        retrieval_query=retrieval_query,
        research_blueprint=blueprint,
        topic_data_status="live_discovery" if live_discovery else "reference_demo",
        task_id=knowledge.task_id,
        run_id=knowledge.run_id,
        contract_id=knowledge.contract_id,
        status=(
            "evidence_table_ready"
            if live_discovery
            and online_field_mapping is not None
            and online_field_mapping.decisions
            else "discovery_completed"
            if live_discovery
            else delivery.status.value
        ),
        quality_score=0.0 if live_discovery else report.quality_score,
        quality_gate_passed=False if live_discovery else report.quality_gate_passed,
        stages=stages,
        sources=_live_sources(online_research, online_acquisition, online_structured_data)
        if live_discovery
        else tuple(
            WorkbenchSource(
                candidate_id=item.candidate_id,
                rank=item.selection_rank,
                source_names=item.source_ids,
                categories=tuple(value.value for value in item.categories),
                covered_fields=item.covered_fields,
                license_status=item.license_decision.value,
                download_status=item.download_readiness.value,
                primary=item.primary_source,
                score=item.assessment_score,
            )
            for item in selected.sources
        ),
        artifacts=(
            tuple(
                WorkbenchArtifact(
                    object_id=item.byte_sha256,
                    format=item.artifact_kind,
                    media_type=item.media_type,
                    size_bytes=item.size_bytes,
                    disposition="parse",
                    parser=_structured_parser_for(item.byte_sha256, online_structured_data),
                    confidence=1.0,
                    sha256=item.byte_sha256,
                )
                for item in online_acquisition.artifacts
            )
            if live_discovery and online_acquisition is not None
            else (() if live_discovery else artifacts)
        ),
        fields=() if live_discovery else fields,
        evidence=_live_structured_evidence(online_structured_data)
        if live_discovery
        else tuple(
            WorkbenchEvidence(
                evidence_id=item.evidence_id,
                field_name=evidence_field.get(item.evidence_id, "unknown"),
                raw_value=item.raw_value,
                source_location=f"表格第 {item.row_index + 1} 行, 第 {item.column_index + 1} 列",
                byte_range=f"字节 {item.start_byte}-{item.end_byte}",
                method=item.extraction_method,
                confidence=item.confidence,
                source_hash=item.artifact_hash,
            )
            for item in extraction.evidence_set.atoms
        ),
        gates=()
        if live_discovery
        else tuple(
            WorkbenchGate(
                gate_id=item.gate_id,
                label=_GATE_LABELS.get(item.gate_id, item.gate_id),
                fields=item.field_names,
                score=item.score,
                threshold=item.threshold,
                passed=item.passed,
                blocking=item.blocking,
            )
            for item in quality.gate_evaluation_set.evaluations
        ),
        issues=_live_structured_issues(online_structured_data)
        if live_discovery
        else tuple(
            WorkbenchIssue(
                issue_id=item.issue_id,
                code=item.code.value,
                severity=item.severity.value,
                fields=item.affected_field_names,
                detail=item.detail,
                action=item.suggested_action.value,
                evidence_count=len(item.evidence_refs),
            )
            for item in quality.issue_set.issues
        ),
        hits=()
        if live_discovery
        else tuple(
            WorkbenchHit(
                source_id=item.source_id,
                location=item.location,
                sparse_score=item.sparse_score,
                graph_score=item.graph_score,
                final_score=item.final_score,
            )
            for item in knowledge.retrieval.hits
        ),
        graph_nodes=graph_nodes,
        graph_edges=graph_edges,
        chart_points=()
        if live_discovery
        else (
            gold_chart_points
            if gold_chart_points
            else tuple(
                WorkbenchChartPoint(
                    x=item.data_x,
                    y=item.data_y,
                    error_x=item.error_x,
                    error_y=item.error_y,
                )
                for item in figure.figure_ir.point_set.points
            )
        ),
        scientific_dataset=None
        if live_discovery
        else WorkbenchScientificDataset(
            format=scientific_request.artifact.format.value,
            parser_id=scientific.runtime.parser.parser_id,
            engine_name=scientific.runtime.parser.engine_name,
            hdu_index=scientific_request.subset.hdu_index,
            variable_names=scientific_request.subset.variable_names,
            selected_row_count=scientific.metrics.selected_row_count,
            materialized_cell_count=scientific.metrics.materialized_cell_count,
            missing_value_count=scientific.metrics.missing_value_count,
            transformation_count=scientific.metrics.transformation_count,
            input_byte_count=scientific.metrics.input_byte_count,
            dataset_hash=scientific.dataset_ref.dataset_hash,
        ),
        online_research=online_research,
        online_acquisition=online_acquisition,
        online_structured_data=online_structured_data,
        online_field_mapping=online_field_mapping,
        agent_reflection=agent_reflection,
        automated_quality_review=automated_quality_review,
        review_automation=_review_automation(
            live_discovery=live_discovery,
            blueprint=blueprint,
            online_research=online_research,
            quality=quality,
            automated_quality_review=automated_quality_review,
        ),
        delivery_artifact_count=0 if live_discovery else delivery.metrics.artifact_count,
        package_filename=delivery.package.filename,
        formal_gold_available=False if live_discovery else quality.formal_gold_dataset is not None,
    )


def _review_automation(
    *,
    live_discovery: bool,
    blueprint: ResearchExplorationProfile,
    online_research: OnlineResearchResult | None,
    quality: QualityAuditResult,
    automated_quality_review: AutomatedQualityReview | None,
) -> WorkbenchReviewAutomation:
    """Route only genuine unresolved conflicts to people; retain machine-call proof."""

    proof_hashes: list[str] = []
    if online_research is not None:
        for execution in online_research.search_executions:
            if execution.invocation is not None:
                proof_hashes.extend(
                    (execution.invocation.query_hash, execution.invocation.response_hash)
                )
        for invocation in (
            online_research.planning_model_invocation,
            online_research.model_invocation,
        ):
            if invocation is not None:
                proof_hashes.extend((invocation.request_hash, invocation.response_hash))
    if (
        automated_quality_review is not None
        and automated_quality_review.model_invocation is not None
    ):
        proof_hashes.extend(
            (
                automated_quality_review.model_invocation.request_hash,
                automated_quality_review.model_invocation.response_hash,
            )
        )
    unique_hashes = tuple(dict.fromkeys(proof_hashes))
    if live_discovery:
        automatic = len(online_research.sources) if online_research is not None else 0
        return WorkbenchReviewAutomation(
            automatic_item_count=automatic,
            evidence_wait_count=len(blueprint.candidate_fields),
            human_review_count=0,
            ai_assessment_performed=bool(online_research and online_research.model_performed),
            proof_hashes=unique_hashes,
        )
    human_review_count = (
        sum(decision.action == "request_human" for decision in automated_quality_review.decisions)
        if automated_quality_review is not None
        else len(quality.review_queue.items)
    )
    return WorkbenchReviewAutomation(
        automatic_item_count=quality.metrics.gate_count,
        evidence_wait_count=max(0, len(quality.issue_set.issues) - human_review_count),
        human_review_count=human_review_count,
        ai_assessment_performed=bool(
            automated_quality_review and automated_quality_review.status == "completed"
        ),
        proof_hashes=unique_hashes,
    )


def _live_discovery_stages(
    blueprint: ResearchExplorationProfile,
    online_research: OnlineResearchResult | None,
    online_acquisition: OnlineAcquisitionResult | None,
    online_structured_data: OnlineStructuredDataResult | None,
    online_field_mapping: OnlineFieldMappingResult | None,
) -> tuple[WorkbenchStage, ...]:
    source_count = 0 if online_research is None else len(online_research.sources)
    query_count = 0 if online_research is None else len(online_research.search_plan.queries)
    discovery_status: Literal["complete", "review"] = "complete" if source_count else "review"
    artifact_count = 0 if online_acquisition is None else len(online_acquisition.artifacts)
    dataset_count = 0 if online_structured_data is None else len(online_structured_data.datasets)
    evidence_count = (
        0
        if online_structured_data is None
        else sum(len(item.cells) for item in online_structured_data.datasets)
    )
    field_count = (
        0
        if online_structured_data is None
        else sum(item.column_count for item in online_structured_data.datasets)
    )
    mapped_count = 0 if online_field_mapping is None else online_field_mapping.mapped_count
    unmapped_count = 0 if online_field_mapping is None else online_field_mapping.unmapped_count
    return (
        WorkbenchStage(
            key="goal",
            label="主题理解",
            status="complete",
            primary_count=len(blueprint.evidence_priorities),
            count_label="证据重点",
            detail="研究方向已转为自主探索蓝图, 包含候选字段、来源类型和质量检查。",
        ),
        WorkbenchStage(
            key="discover",
            label="多源检索",
            status=discovery_status,
            primary_count=source_count,
            count_label="真实来源",
            detail=f"执行 {query_count} 条主题检索式, 发现 {source_count} 个可继续核验的网页来源。",
        ),
        WorkbenchStage(
            key="parse",
            label="获取与解析",
            status="complete" if dataset_count else "review",
            primary_count=evidence_count,
            count_label="单元格证据",
            detail=f"已内容寻址保存 {artifact_count} 个真实材料, 其中 {dataset_count} 个机器可读文件完成严格结构化预览。",
        ),
        WorkbenchStage(
            key="integrate",
            label="字段整合",
            status="complete" if field_count and online_field_mapping is not None else "review",
            primary_count=mapped_count,
            count_label=f"已映射 / {field_count} 列",
            detail=f"已对真实表头生成可审核字段映射; {unmapped_count} 列证据不足并保留原名, 原始科学值未改写。",
        ),
        WorkbenchStage(
            key="quality",
            label="证据校验",
            status="review",
            primary_count=len(blueprint.quality_checks),
            count_label="计划检查",
            detail="当前预览已绑定文件哈希与行列位置, 但尚未通过语义、单位、冲突和不确定性质量门。",
        ),
        WorkbenchStage(
            key="deliver",
            label="证据表交付",
            status="complete" if online_field_mapping and field_count else "review",
            primary_count=field_count,
            count_label="可追溯列",
            detail="可下载多源证据长表, 每个单元格保留来源、哈希、行列和映射状态; 正式 Gold 仍须通过全部质量门。",
        ),
    )


def _exploration_graph(
    blueprint: ResearchExplorationProfile,
    online_research: OnlineResearchResult | None,
    online_structured_data: OnlineStructuredDataResult | None,
    online_field_mapping: OnlineFieldMappingResult | None,
) -> tuple[tuple[WorkbenchGraphNode, ...], tuple[WorkbenchGraphEdge, ...]]:
    task_id = "explore-topic"
    nodes = [
        WorkbenchGraphNode(
            node_id=task_id,
            kind="task",
            source_id="user-research-goal",
            label=blueprint.topic_title[:256],
            trusted=True,
        )
    ]
    edges: list[WorkbenchGraphEdge] = []

    def add_group(values: tuple[str, ...], kind: str, prefix: str, edge_kind: str) -> None:
        for value in values:
            digest = hashlib.sha256(f"{prefix}:{value}".encode()).hexdigest()[:16]
            node_id = f"{prefix}-{digest}"
            nodes.append(
                WorkbenchGraphNode(
                    node_id=node_id,
                    kind=kind,
                    source_id=f"plan:{prefix}",
                    label=value[:256],
                    trusted=False,
                )
            )
            edges.append(
                WorkbenchGraphEdge(
                    source=task_id,
                    target=node_id,
                    kind=edge_kind,
                    evidence_refs=(f"plan:{prefix}",),
                )
            )

    add_group(blueprint.evidence_priorities, "evidence", "priority", "prioritizes")
    add_group(blueprint.candidate_fields, "field", "field", "targets")
    add_group(blueprint.quality_checks, "quality_gate", "quality", "requires")
    add_group(blueprint.target_outputs, "memory", "output", "produces")
    if online_research is not None:
        for source in online_research.sources:
            value = source.search.title
            url = str(source.search.url)
            digest = hashlib.sha256(url.encode()).hexdigest()[:16]
            node_id = f"source-{digest}"
            nodes.append(
                WorkbenchGraphNode(
                    node_id=node_id,
                    kind="source",
                    source_id=url,
                    label=value[:256],
                    trusted=False,
                )
            )
            edges.append(
                WorkbenchGraphEdge(
                    source=task_id,
                    target=node_id,
                    kind="discovered",
                    evidence_refs=(url,),
                )
            )
    if online_structured_data is not None:
        for dataset in online_structured_data.datasets:
            nodes.append(
                WorkbenchGraphNode(
                    node_id=dataset.dataset_id,
                    kind="dataset",
                    source_id=dataset.artifact_sha256,
                    label=f"{dataset.format.upper()} · {dataset.row_count} 行 x {dataset.column_count} 列",
                    trusted=True,
                )
            )
            edges.append(
                WorkbenchGraphEdge(
                    source=task_id,
                    target=dataset.dataset_id,
                    kind="parsed_from",
                    evidence_refs=(dataset.artifact_sha256,),
                )
            )
            for column in dataset.columns[:20]:
                column_id = f"column-{dataset.dataset_id[4:20]}-{column.column_index}"
                nodes.append(
                    WorkbenchGraphNode(
                        node_id=column_id,
                        kind="field",
                        source_id=dataset.artifact_sha256,
                        label=column.name,
                        trusted=True,
                    )
                )
                column_evidence = tuple(
                    item.evidence_id
                    for item in dataset.cells
                    if item.column_index == column.column_index
                )
                edges.append(
                    WorkbenchGraphEdge(
                        source=dataset.dataset_id,
                        target=column_id,
                        kind="contains_field",
                        evidence_refs=column_evidence or (dataset.artifact_sha256,),
                    )
                )
            cells_by_row: dict[int, list[str]] = {}
            for cell in dataset.cells[:120]:
                nodes.append(
                    WorkbenchGraphNode(
                        node_id=cell.evidence_id,
                        kind="evidence",
                        source_id=cell.source_hash,
                        label=(
                            f"证据: {cell.column_name} (第 {cell.row_index} 行, "
                            f"第 {cell.column_index} 列)"
                        )[:256],
                        trusted=True,
                    )
                )
                edges.append(
                    WorkbenchGraphEdge(
                        source=dataset.dataset_id,
                        target=cell.evidence_id,
                        kind="contains_evidence",
                        evidence_refs=(cell.evidence_id,),
                    )
                )
                column_id = f"column-{dataset.dataset_id[4:20]}-{cell.column_index}"
                edges.append(
                    WorkbenchGraphEdge(
                        source=cell.evidence_id,
                        target=column_id,
                        kind="supports_field",
                        evidence_refs=(cell.evidence_id,),
                    )
                )
                cells_by_row.setdefault(cell.row_index, []).append(cell.evidence_id)
            for row_evidence in cells_by_row.values():
                for left, right in pairwise(row_evidence):
                    edges.append(
                        WorkbenchGraphEdge(
                            source=left,
                            target=right,
                            kind="co_observed",
                            evidence_refs=(left, right),
                        )
                    )
    if online_field_mapping is not None:
        for mapping in online_field_mapping.decisions:
            if mapping.target_field is None or mapping.column_index > 20:
                continue
            column_id = f"column-{mapping.dataset_id[4:20]}-{mapping.column_index}"
            digest = hashlib.sha256(f"field:{mapping.target_field}".encode()).hexdigest()[:16]
            edges.append(
                WorkbenchGraphEdge(
                    source=column_id,
                    target=f"field-{digest}",
                    kind="maps_to",
                    evidence_refs=mapping.evidence_ids or (mapping.artifact_sha256,),
                )
            )
    return tuple(nodes), tuple(edges)


def _chinese_graph_label(kind: str, label: str) -> str:
    """Localize deterministic M19 labels while preserving immutable graph identities."""

    match = re.fullmatch(r"evidence table_cell row (\d+) column (\d+)", label)
    if match:
        return f"表格单元格证据 (第 {match.group(1)} 行, 第 {match.group(2)} 列)"
    prefixes = {
        "field ": "字段: ",
        "quality gate ": "质量门: ",
        "quality issue ": "质量问题: ",
        "task memory ": "任务记忆: ",
    }
    for prefix, translated in prefixes.items():
        if label.startswith(prefix):
            return f"{translated}{label[len(prefix) :]}"[:256]
    if kind == "task" and label.lower().startswith("task"):
        return "当前研究任务"
    return label[:256]


def _structured_parser_for(
    artifact_sha256: str, online_structured_data: OnlineStructuredDataResult | None
) -> str | None:
    if online_structured_data is None:
        return None
    dataset = next(
        (
            item
            for item in online_structured_data.datasets
            if item.artifact_sha256 == artifact_sha256
        ),
        None,
    )
    return None if dataset is None else dataset.parser_id


def _live_structured_evidence(
    online_structured_data: OnlineStructuredDataResult | None,
) -> tuple[WorkbenchEvidence, ...]:
    if online_structured_data is None:
        return ()
    return tuple(
        WorkbenchEvidence(
            evidence_id=cell.evidence_id,
            field_name=cell.column_name,
            raw_value=cell.raw_value_json,
            source_location=cell.source_location,
            byte_range="内容哈希与结构位置联合定位",
            method=dataset.parser_id,
            confidence=1.0,
            source_hash=cell.source_hash,
        )
        for dataset in online_structured_data.datasets
        for cell in dataset.cells
    )


def _live_structured_issues(
    online_structured_data: OnlineStructuredDataResult | None,
) -> tuple[WorkbenchIssue, ...]:
    if online_structured_data is None:
        return ()
    return tuple(
        WorkbenchIssue(
            issue_id=f"issue_{hashlib.sha256(f'{item.artifact_sha256}:{item.code}'.encode()).hexdigest()[:32]}",
            code=item.code,
            severity="info" if item.code == "unsupported_media_type" else "warning",
            fields=(),
            detail=item.detail,
            action="下载原文件核验或配置对应格式解析器",
            evidence_count=0,
        )
        for item in online_structured_data.failures
    )


def _live_sources(
    online_research: OnlineResearchResult | None,
    online_acquisition: OnlineAcquisitionResult | None,
    online_structured_data: OnlineStructuredDataResult | None,
) -> tuple[WorkbenchSource, ...]:
    if online_research is None:
        return ()
    acquired_urls = (
        set()
        if online_acquisition is None
        else {str(item.source_url) for item in online_acquisition.artifacts}
    )
    fields_by_url: dict[str, tuple[str, ...]] = {}
    if online_structured_data is not None:
        fields_by_url = {
            str(item.source_url): tuple(column.name for column in item.columns)
            for item in online_structured_data.datasets
        }
    return tuple(
        WorkbenchSource(
            candidate_id=f"live_{hashlib.sha256(str(item.search.url).encode()).hexdigest()[:32]}",
            rank=index,
            source_names=(item.search.title,),
            categories=(
                *((value for value in item.assessment.evidence_types) if item.assessment else ()),
                item.search.channel.value,
            ),
            covered_fields=fields_by_url.get(str(item.search.url), ()),
            license_status="待核验",
            download_status=(
                "已内容寻址保存" if str(item.search.url) in acquired_urls else "已发现待获取"
            ),
            primary=index <= 3,
            score=(
                item.assessment.relevance_score
                if item.assessment is not None
                else max(0.0, 1.0 - (index - 1) * 0.05)
            ),
        )
        for index, item in enumerate(online_research.sources, start=1)
    )


def _gold_chart_points(
    quality: QualityAuditResult,
    extraction_request: ExtractionRequest,
    extraction: ExtractionResult,
) -> tuple[WorkbenchChartPoint, ...]:
    """Build the displayed light curve from quality-approved Gold records."""

    formal = quality.formal_gold_dataset
    if formal is None:
        return ()
    evidence_by_id = {item.evidence_id: item for item in extraction.evidence_set.atoms}
    tables = extraction_request.table_parsing_result.tables
    errors: dict[tuple[str, int], str] = {}
    for table in tables:
        headers = table.cells[: table.column_count]
        error_column = next(
            (
                index
                for index, header in enumerate(headers)
                if header.decoded_text == "magnitude_error"
            ),
            None,
        )
        if error_column is None:
            continue
        for row_index in range(1, table.row_count):
            cell = table.cells[row_index * table.column_count + error_column]
            if cell.decoded_text:
                errors[(table.table_id, row_index)] = cell.decoded_text
    points: list[WorkbenchChartPoint] = []
    for record in formal.records:
        fields = {item.field_name: item for item in record.fields}
        time_field = fields.get("observation_time")
        magnitude_field = fields.get("magnitude")
        if time_field is None or magnitude_field is None:
            continue
        magnitude_atom = evidence_by_id.get(magnitude_field.evidence_ids[0])
        error_y = (
            "0"
            if magnitude_atom is None
            else errors.get((magnitude_atom.table_id, magnitude_atom.row_index), "0")
        )
        points.append(
            WorkbenchChartPoint(
                x=time_field.value,
                y=magnitude_field.value,
                error_x="0",
                error_y=error_y,
            )
        )
    return tuple(sorted(points, key=lambda item: (float(item.x), float(item.y))))


def _field_view(
    field: FieldContract,
    candidate: ExtractedFieldCandidate | None,
    mapping: FieldMapping | None,
    normalized: NormalizedField | None,
    fused: FusedField | None,
) -> WorkbenchField:
    evidence_ids = () if candidate is None else candidate.evidence_ids
    issue_ids = () if normalized is None else normalized.issue_ids
    decision = "missing"
    selected_value = None
    if fused is not None:
        selected_value = fused.selected_value
        decision = "selected" if selected_value is not None else "withheld"
    return WorkbenchField(
        name=field.name,
        label=_FIELD_LABELS.get(field.name, field.name),
        requirement=field.requirement.value,
        data_type=field.data_type.value,
        target_unit=field.target_unit,
        raw_value=None if candidate is None else candidate.raw_value,
        normalized_value=None if normalized is None else normalized.normalized_value,
        selected_value=selected_value,
        mapping_method=None if mapping is None else mapping.method.value,
        mapping_score=None if mapping is None else mapping.score,
        decision=decision,
        evidence_ids=evidence_ids,
        issue_count=len(issue_ids),
    )

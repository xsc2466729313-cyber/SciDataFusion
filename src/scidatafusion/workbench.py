"""Build the Chinese workbench's complete, evidence-backed product projection."""

from __future__ import annotations

import hashlib
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
from scidatafusion.contracts.quality import QualityAuditResult
from scidatafusion.contracts.scientific import FieldContract
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
        _live_discovery_stages(blueprint, online_research, online_acquisition)
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
        _exploration_graph(blueprint, online_research)
        if live_discovery
        else (
            tuple(
                WorkbenchGraphNode(
                    node_id=item.node_id,
                    kind=item.kind.value,
                    source_id=item.source_id,
                    label=item.label,
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
        status="discovery_completed" if live_discovery else delivery.status.value,
        quality_score=0.0 if live_discovery else report.quality_score,
        quality_gate_passed=False if live_discovery else report.quality_gate_passed,
        stages=stages,
        sources=()
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
                    parser=None,
                    confidence=1.0,
                    sha256=item.byte_sha256,
                )
                for item in online_acquisition.artifacts
            )
            if live_discovery and online_acquisition is not None
            else (() if live_discovery else artifacts)
        ),
        fields=() if live_discovery else fields,
        evidence=()
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
        issues=()
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
) -> tuple[WorkbenchStage, ...]:
    source_count = 0 if online_research is None else len(online_research.sources)
    query_count = 0 if online_research is None else len(online_research.search_plan.queries)
    discovery_status: Literal["complete", "review"] = "complete" if source_count else "review"
    artifact_count = 0 if online_acquisition is None else len(online_acquisition.artifacts)
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
            label="材料获取",
            status="complete" if artifact_count else "review",
            primary_count=artifact_count,
            count_label="已获取材料",
            detail=f"已自动获取并内容寻址保存 {artifact_count} 个真实材料; 失败来源保留结构化原因, 不阻塞其他来源。",
        ),
        WorkbenchStage(
            key="integrate",
            label="字段整合",
            status="review",
            primary_count=len(blueprint.candidate_fields),
            count_label="候选字段",
            detail="字段仅为主题驱动的提取计划; 取得真实数据前不生成、不填充科学值。",
        ),
        WorkbenchStage(
            key="quality",
            label="证据校验",
            status="review",
            primary_count=len(blueprint.quality_checks),
            count_label="计划检查",
            detail="后续数据必须通过来源、完整性、单位、冲突与不确定性检查。",
        ),
        WorkbenchStage(
            key="deliver",
            label="结构化交付",
            status="review",
            primary_count=len(blueprint.target_outputs),
            count_label="目标成果",
            detail="当前完成主题探索; 只有真实数据完成解析并通过质量门后才开放 CSV 与复现包。",
        ),
    )


def _exploration_graph(
    blueprint: ResearchExplorationProfile,
    online_research: OnlineResearchResult | None,
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
                    kind="evidence",
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
    return tuple(nodes), tuple(edges)


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

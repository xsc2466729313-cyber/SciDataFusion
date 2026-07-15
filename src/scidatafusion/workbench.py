"""Build the Chinese workbench's complete, evidence-backed product projection."""

from __future__ import annotations

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
    AutomatedQualityReview,
    OnlineResearchResult,
    ResearchExecutionMode,
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
    WorkbenchScientificDataset,
    WorkbenchSnapshot,
    WorkbenchSource,
    WorkbenchStage,
)

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
    stages = (
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
    return WorkbenchSnapshot(
        execution_mode=execution_mode,
        research_goal=research_goal,
        retrieval_query=retrieval_query,
        task_id=knowledge.task_id,
        run_id=knowledge.run_id,
        contract_id=knowledge.contract_id,
        status=delivery.status.value,
        quality_score=report.quality_score,
        quality_gate_passed=report.quality_gate_passed,
        stages=stages,
        sources=tuple(
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
        artifacts=artifacts,
        fields=fields,
        evidence=tuple(
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
        gates=tuple(
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
        issues=tuple(
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
        hits=tuple(
            WorkbenchHit(
                source_id=item.source_id,
                location=item.location,
                sparse_score=item.sparse_score,
                graph_score=item.graph_score,
                final_score=item.final_score,
            )
            for item in knowledge.retrieval.hits
        ),
        graph_nodes=tuple(
            WorkbenchGraphNode(
                node_id=item.node_id,
                kind=item.kind.value,
                source_id=item.source_id,
                label=item.label,
                trusted=item.trusted_fact,
            )
            for item in knowledge.graph.nodes
        ),
        graph_edges=tuple(
            WorkbenchGraphEdge(
                source=item.source_node_id,
                target=item.target_node_id,
                kind=item.kind.value,
                evidence_refs=item.evidence_refs,
            )
            for item in knowledge.graph.edges
        ),
        chart_points=(
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
        scientific_dataset=WorkbenchScientificDataset(
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
        automated_quality_review=automated_quality_review,
        delivery_artifact_count=delivery.metrics.artifact_count,
        package_filename=delivery.package.filename,
        formal_gold_available=quality.formal_gold_dataset is not None,
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

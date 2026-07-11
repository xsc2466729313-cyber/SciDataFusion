# 06 检索、Hybrid RAG、GraphRAG与知识管理

## 1. 三种检索目标必须分开

1. **发现检索**：找论文、数据集、仓库和附件；
2. **证据检索**：在已下载资料中找字段和关系证据；
3. **经验检索**：复用领域规则、数据源说明和历史成功策略。

## 2. Hybrid RAG

```text
候选 = BM25 ∪ DenseEmbedding ∪ MetadataFilter ∪ GraphNeighborhood
最终 = Rerank(query, 候选Top20~100) → Top5~10
```

官方百炼文档也建议在初始检索返回20–100+混合相关候选时使用rerank，再选Top结果进入模型。

## 3. 结构化Chunk

- 正文按章节、段落和引用关系；
- 表格按caption+header+行组，不破坏表头；
- 图表按caption+OCR+视觉描述+系列；
- 数据库文档按endpoint/字段/示例；
- chunk保留文档、页码、bbox和层级路径。

## 4. Evidence Graph

节点：ResearchQuestion、Entity、Paper、Dataset、Artifact、Table、Figure、Field、Unit、Observation、Transformation、QualityIssue、Run。

关系：MENTIONS、PROVIDES、EXTRACTED_FROM、DERIVED_FROM、HAS_UNIT、SAME_AS、CONFLICTS_WITH、VALIDATED_BY、GENERATED_IN。

## 5. GraphRAG使用边界

适合：跨文档实体关系、来源链、冲突定位、全局主题和证据推理。
不适合：简单精确字段查找、单文档小规模任务、预算极小任务。

## 6. 知识准入

- official：官方数据库文档/标准；
- verified：人工确认且评测通过；
- provisional：当前任务临时推断；
- quarantined：冲突或低质量，不参与自动决策。

任何从任务自动提炼的规则默认provisional，不能直接污染所有后续任务。

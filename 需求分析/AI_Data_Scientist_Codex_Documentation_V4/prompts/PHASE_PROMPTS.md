# Codex阶段提示词索引

## Phase 0 工程基线
读取：00、03、04、08、11。建立配置、合同基类、错误、事件、测试和CI。

## Phase 1 需求与合同
读取模块：M00-M03。出口是用户输入到已确认ScientificDataContract。

## Phase 2 检索
读取模块：M04-M06。出口是SelectedSourceSet、CoverageReport和可重放搜索日志。

## Phase 3 下载与解析
读取模块：M07-M10。出口是Bronze Manifest、DocumentIR和TableIR。

## Phase 4 Evidence和规范化
读取模块：M13-M15。出口是EvidenceAtom、字段映射和TransformationRecord。

## Phase 5 实体、融合和质量
读取模块：M16-M18。出口是GoldCandidate、ConflictSet、QualityReport和审核闭环。

## Phase 6 知识系统
读取模块：M19及06/07。出口是Hybrid Retriever、Evidence Graph和知识准入流程。

## Phase 7 特殊模态
读取模块：M11-M12。出口是图表数字化和至少一种领域科学文件解析。

## Phase 8 交付
读取模块：M20及02/08/12。出口是前端和复现包。

## Phase 9 评测与演示
读取09、10、12。建立黄金集、故障注入、消融、三领域和留出领域报告。

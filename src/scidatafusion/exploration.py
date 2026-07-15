"""Deterministic topic-first exploration fallbacks used without a valid model plan."""

from __future__ import annotations

from scidatafusion.contracts.online import ResearchExplorationProfile


def build_fallback_exploration_profile(research_goal: str) -> ResearchExplorationProfile:
    """Create a bounded generic plan without inventing topic facts or scientific values."""

    normalized = " ".join(research_goal.split())
    title = normalized[:120].rstrip(",.; ")
    if len(normalized) > 120:
        title = f"{title}…"
    return ResearchExplorationProfile(
        topic_title=title,
        research_summary=(
            f"围绕“{title}”发现论文、数据仓库、机器可读表格、补充材料与图像, "
            "先建立来源和候选字段清单, 再对可验证数据执行解析、对齐和质量校验。"
        ),
        evidence_priorities=(
            "与研究主题直接相关的可验证证据",
            "数据定义、采集方法与适用范围",
            "记录级来源、版本、许可与引用信息",
            "可下载的机器可读数据及其字段说明",
        ),
        source_types=("paper", "repository", "table", "supplement", "image", "catalog"),
        candidate_fields=(
            "entity_id",
            "observation_or_sample_time",
            "measured_variable",
            "measured_value",
            "unit",
            "uncertainty",
            "source_record_id",
            "source_url",
        ),
        quality_checks=(
            "必填字段完整性",
            "单位、数据类型与取值范围一致性",
            "重复记录和冲突值检测",
            "字段级来源与证据链完整性",
            "缺失值和不确定性保留",
        ),
        target_outputs=("来源清单", "候选字段字典", "证据关联数据表", "质量报告"),
        visualization_hint="以研究主题、来源、候选字段和质量检查构建可交互知识图谱",
    )


def build_fallback_search_query(research_goal: str) -> str:
    """Derive one bounded search expression from untrusted user text."""

    normalized = " ".join(research_goal.split())
    return f"{normalized[:360]} 论文 数据集 开放数据 机器可读表格"

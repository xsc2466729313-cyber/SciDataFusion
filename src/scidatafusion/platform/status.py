"""Secret-free platform capability reporting."""

from __future__ import annotations

import importlib.util

from scidatafusion.config import PlatformMode, Settings
from scidatafusion.contracts.platform import PlatformComponent, PlatformStatus


def build_platform_status(settings: Settings) -> PlatformStatus:
    infrastructure_ready = settings.platform_mode is PlatformMode.CELERY
    components = [
        PlatformComponent(name="fastapi", status="ready", detail="异步研究任务 API"),
        PlatformComponent(
            name="postgresql",
            status="ready" if infrastructure_ready else "disabled",
            detail="任务元数据与状态",
        ),
        PlatformComponent(
            name="redis_celery",
            status="ready" if infrastructure_ready else "disabled",
            detail="分布式任务队列",
        ),
        PlatformComponent(
            name="chroma",
            status="ready" if infrastructure_ready else "disabled",
            detail="证据向量索引",
        ),
    ]
    optional = (
        ("langgraph", "langgraph", "受约束 Agent 状态机"),
        ("langchain", "langchain_core", "文档互操作"),
        ("llamaindex", "llama_index.core", "证据节点互操作"),
        ("sklearn", "sklearn", "确定性文本向量"),
        ("pytorch", "torch", "向量有限值校验"),
    )
    for name, module, detail in optional:
        available = _module_available(module)
        components.append(
            PlatformComponent(
                name=name,  # type: ignore[arg-type]
                status="ready" if available else "optional",
                detail=detail,
            )
        )
    return PlatformStatus(mode=settings.platform_mode.value, components=tuple(components))


def _module_available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False

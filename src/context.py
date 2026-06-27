# -*- coding: utf-8 -*-
"""全局上下文和共享状态。

替代原来的 _ctx 单例模式。
GlobalContext 在主入口实例化，显式传递给 Agent 和 Service。
"""

from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from novel2comic.src.models import Novel, ChapterData
    from agentflow.runtime.llm_client import OpenAIClient


@dataclass
class GlobalContext:
    """显式管理的共享状态，替代全局 _ctx 单例。

    生命周期由 main.py / cli.py 管理。
    每个 Agent 在其构造函数中接收此对象。
    """

    novel: Optional["Novel"] = None
    chapter_data: Optional["ChapterData"] = None
    llm_model: str = ""

    # LLM 客户端（由 build_application 设置）
    agent_llm: Optional["OpenAIClient"] = None  # 用于 AgentFlow 异步循环
    sync_openai: Optional[object] = None  # 用于工具内部的同步 LLM 调用

    services: Optional["ServiceRegistry"] = None

    # 角色蒸馏 Profile（由 CharacterDistiller 蒸馏后挂载）
    # {character_name: CharacterProfile}
    character_profiles: dict = field(default_factory=dict)


@dataclass
class ServiceRegistry:
    """所有管线服务的访问入口。

    每个服务无状态（除配置和客户端外）。
    Agent 通过此注册表调用服务，而非通过 Agent 工具。
    """

    kg: Optional["KnowledgeGraphService"] = None
    image: Optional["ImageGenerationService"] = None
    comic: Optional["ComicCompilationService"] = None
    project: Optional["ProjectService"] = None
    search: Optional["SearchService"] = None


# 向后兼容别名（Phase 1 过渡用）
# agent.py 中的 _ctx 仍可用，但底层指向 GlobalContext
_ctx = GlobalContext()

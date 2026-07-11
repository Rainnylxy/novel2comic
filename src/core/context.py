# -*- coding: utf-8 -*-
"""全局上下文和共享服务。

重构要点:
  - AppContext: 只持有 novel + services，其余全部通过 services 访问
  - ServiceRegistry: 统一的依赖注入入口（kg + project + llm）
  - LLM 不再散落在 ctx 上，统一到 services.llm
"""

from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Novel


@dataclass
class AppContext:
    """应用级上下文 —— 只持有小说和服务的引用。

    生命周期由 main.py / cli.py 管理。
    """

    novel: Optional["Novel"] = None
    services: Optional["ServiceRegistry"] = None


@dataclass
class ServiceRegistry:
    """服务注册表 —— 统一的依赖注入入口。

    所有基础设施服务通过此入口访问，避免 ctx 膨胀。
    """

    kg: Optional["KnowledgeGraphService"] = None
    project: Optional["ProjectService"] = None
    llm: Optional["UnifiedLLM"] = None       # 统一 LLM 入口（含 chat_json + chat）
    agent_llm: Optional[object] = None       # AgentFlow OpenAIClient（AgentBuilder 需要）


# ── 向后兼容别名 ──
GlobalContext = AppContext  # 旧名称仍可用

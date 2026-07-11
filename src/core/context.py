# -*- coding: utf-8 -*-
"""全局上下文和共享状态。"""

from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Novel
    from agentflow.runtime.llm_client import OpenAIClient


@dataclass
class GlobalContext:
    """显式管理的共享状态。

    生命周期由 main.py / cli.py 管理。
    """

    novel: Optional["Novel"] = None
    llm_model: str = ""

    agent_llm: Optional["OpenAIClient"] = None
    sync_openai: Optional[object] = None

    services: Optional["ServiceRegistry"] = None

    # 角色蒸馏 Profile
    character_profiles: dict = field(default_factory=dict)


@dataclass
class ServiceRegistry:
    """服务访问入口。"""

    kg: Optional["KnowledgeGraphService"] = None
    project: Optional["ProjectService"] = None

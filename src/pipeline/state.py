# -*- coding: utf-8 -*-
"""PipelineState —— Agent 共享状态的只读视图。

取代 ctx/services 的全量注入模式。
Agent 通过 state 按需访问共享上下文，调用方通过 set_context() 只传每章 delta。
"""

from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.models import Novel
    from ..distillers.style_profile import AuthorStyleProfile
    from .story_memory import StoryMemory


@dataclass
class PipelineState:
    """Pipeline 与 Agent 之间的共享状态。

    Agent 通过此对象访问风格、角色档案、故事记忆等稳定上下文。
    每章变化的数据（ending、roadmap、instruction）仍然通过 set_context() 传入。

    Attributes:
        novel: 小说对象（含 story_graph、chapters 等）
        style_profile: 蒸馏后的文风档案
        character_profiles: {name: CharacterProfile} 核心角色蒸馏档案
        story_memory: 故事应用层记忆
        status_verified: 已验证过角色状态的名称集合（可变，Pipeline 和 Agent 共享）
        status_fixes: {name: status} 验证后的状态修正（可变引用）
        novel_text: 小说全文缓存
        kg: KnowledgeGraphService 引用（Agent 工具按需查询）
    """

    novel: Optional["Novel"] = None
    style_profile: Optional["AuthorStyleProfile"] = None
    character_profiles: dict = field(default_factory=dict)
    story_memory: Optional["StoryMemory"] = None
    status_verified: set = field(default_factory=set)
    status_fixes: dict = field(default_factory=dict)
    novel_text: str = ""
    kg: Optional[object] = None
    agent_llm: Optional[object] = None
    sync_llm: Optional[object] = None

    character_statuses: dict = field(default_factory=dict)

    # ── 派生属性 ──

    @property
    def graph(self):
        """StoryGraph 快捷访问。"""
        return self.novel.story_graph if self.novel else None

    def refresh_statuses(self):
        """从 story_memory 刷新 character_statuses。Pipeline 每章调用。"""
        if self.story_memory:
            self.character_statuses = self.story_memory.get_dead_or_missing()

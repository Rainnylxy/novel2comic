# -*- coding: utf-8 -*-
"""BaseAgent —— 所有 Agent 的抽象基类。

封装 AgentFlow AgentBuilder 的通用模式，集成 PromptContext + AgentMemory。
"""

import os
import sys
from typing import Optional, TYPE_CHECKING

from agentflow.runtime.builder import AgentBuilder
from agentflow.runtime.llm_client import OpenAIClient
from agentflow.runtime.memory.manager import MemoryProfile
from agentflow.runtime.thinking import ThinkingMode
from agentflow.runtime.toolkit import tool

from novel2comic.src.prompt_context import PromptContext, PromptNeed, PromptResult
from novel2comic.src.agent_memory import AgentMemory

if TYPE_CHECKING:
    from novel2comic.src.context import GlobalContext, ServiceRegistry
    from novel2comic.src.llm import UnifiedLLM

SKILLS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "skills",
)


class BaseAgent:
    """所有 Agent 的抽象基类。

    内置三层：
    - AgentMemory   → AgentLLM 记忆（跨 Agent、跨会话）
    - PromptContext → LLM Prompt 装配（统一模板 + KG + token 预算）
    - UnifiedLLM    → 纯 LLM 调用

    子类覆写：
    - SKILL_NAME
    - _build_tools() → 使用 self._build_prompt(need) 而非内联拼 prompt
    """

    SKILL_NAME: str = ""

    def __init__(
        self,
        ctx: "GlobalContext",
        services: "ServiceRegistry",
        llm: "UnifiedLLM",
        memory: Optional[AgentMemory] = None,
    ):
        self._ctx = ctx
        self._services = services
        self._llm = llm
        self._memory = memory or AgentMemory()
        self._prompt_ctx = PromptContext(kg_service=services.kg)

    # ── Prompt 装配（工具使用） ──

    def _build_prompt(self, need: PromptNeed) -> PromptResult:
        """工具调用：声明需求 → 返回组装好的 prompt。

        自动设置 KG graph（从 ctx.novel.story_graph）。
        """
        if self._ctx.novel and self._ctx.novel.story_graph:
            self._prompt_ctx.set_graph(self._ctx.novel.story_graph)
        return self._prompt_ctx.build(need)

    def _fetch_kg(self, specs: list[str]) -> str:
        """快捷方法：精确取 KG 片段（不经过模板系统）。"""
        if self._ctx.novel and self._ctx.novel.story_graph:
            self._prompt_ctx.set_graph(self._ctx.novel.story_graph)
        return self._prompt_ctx.fetch_kg_for(specs)

    # ── 记忆操作 ──

    def _remember(self, key: str, value: str, scope: str = "session",
                  importance: int = 1):
        """记录一条 Agent 记忆。"""
        self._memory.remember(key, value, scope=scope,
                              agent_type=self.SKILL_NAME, importance=importance)

    def _recall(self, key: str, scope: str = "all") -> Optional[str]:
        """检索一条记忆。"""
        return self._memory.recall(key, scope=scope)

    # ── 子类接口 ──

    def _build_tools(self) -> list:
        raise NotImplementedError("子类必须实现 _build_tools()")

    def _get_skill_path(self) -> str:
        return os.path.join(SKILLS_DIR, f"{self.SKILL_NAME}.md")

    async def build(self):
        if not self.SKILL_NAME:
            raise ValueError("SKILL_NAME 未设置")

        skill_path = self._get_skill_path()
        if not os.path.exists(skill_path):
            print(f"[WARNING] 技能文件不存在: {skill_path}")

        tools = self._build_tools()

        # 注入 AgentMemory 上下文到 system prompt
        memory_context = self._memory.build_agent_context(self.SKILL_NAME)

        builder = (
            AgentBuilder(self.SKILL_NAME)
            .with_llm(self._ctx.agent_llm)
            .with_skills_dir(SKILLS_DIR)
            .with_skill(self.SKILL_NAME)
            .with_tools(*tools)
            .with_memory(MemoryProfile.standard())
            .with_thinking(ThinkingMode.REACT)
            .with_max_iterations(15)
        )

        # 有记忆上下文时注入
        if memory_context:
            builder = builder.with_prompt(
                f"{{skill:{self.SKILL_NAME}}}\n\n{memory_context}"
            )

        return await builder.build()

    async def run(self, task: str):
        agent = await self.build()
        return await agent.run(task)

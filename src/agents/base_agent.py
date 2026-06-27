# -*- coding: utf-8 -*-
"""BaseAgent —— 所有 Agent 的抽象基类。

支持固定身份 system prompt（角色扮演等场景）。
集成 PromptContext + AgentMemory Facade。
"""

import os
from typing import Optional, TYPE_CHECKING

from agentflow.runtime.builder import AgentBuilder
from agentflow.runtime.memory.manager import MemoryProfile, WorkingConfig
from agentflow.runtime.thinking import ThinkingMode

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

    关键字段:
    - _identity_prompt: 固定身份 prompt，非空时替换 skill 内容作为 system prompt
      (RolePlayAgent 用: start_conversation 设为 "你是江停。XXXXX")
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
        self._identity_prompt: str = ""       # 固定身份（非空时替换 skill）
        self._built_agent = None              # 当前 AgentFlow agent 实例
        self._needs_rebuild = False           # 标记：身份变了，需要重建

    # ── 身份 Prompt ──

    @property
    def identity(self) -> str:
        """当前固定身份的显示名。"""
        return self._identity_prompt.split("\n")[0].replace("你是 ", "").rstrip("。") if self._identity_prompt else ""

    def set_identity(self, prompt: str):
        """设置固定身份 system prompt。标记需要重建，保留对话上下文。"""
        self._identity_prompt = prompt
        self._needs_rebuild = True

    def clear_identity(self):
        """清除固定身份，恢复默认 skill prompt。"""
        self._identity_prompt = ""
        self._needs_rebuild = True

    # ── Prompt 装配 ──

    def _build_prompt(self, need: PromptNeed) -> PromptResult:
        if self._ctx.novel and self._ctx.novel.story_graph:
            self._prompt_ctx.set_graph(self._ctx.novel.story_graph)
        return self._prompt_ctx.build(need)

    def _fetch_kg(self, specs: list[str]) -> str:
        if self._ctx.novel and self._ctx.novel.story_graph:
            self._prompt_ctx.set_graph(self._ctx.novel.story_graph)
        return self._prompt_ctx.fetch_kg_for(specs)

    # ── 对话上下文 ──

    def _get_conversation_context(self, max_turns: int = 20) -> str:
        """从 AgentFlow WorkingMemory 提取最近的对话。

        替代原 RolePlayState.conversation_history 的手动维护。
        """
        if not self._built_agent:
            return ""
        try:
            messages = list(self._built_agent.memory.working._messages)
        except Exception:
            return ""

        # 取最近 N*2 条消息（user + assistant 成对），排除 system
        recent = [m for m in messages if m.role != "system"][-max_turns * 2:]
        lines = []
        for msg in recent:
            role_label = {"user": "对方", "assistant": self.identity or "角色"}.get(msg.role, msg.role)
            content = getattr(msg, 'content', str(msg))[:200]
            lines.append(f"{role_label}: {content}")
        return "\n".join(lines)

    # ── 子类接口 ──

    def _build_tools(self) -> list:
        raise NotImplementedError

    def _get_skill_path(self) -> str:
        return os.path.join(SKILLS_DIR, f"{self.SKILL_NAME}.md")

    def _get_memory_profile(self) -> MemoryProfile:
        """子类可覆盖以定制记忆配置。默认 roleplay 需要更长上下文。"""
        return MemoryProfile(
            working=WorkingConfig(max_turns=30),
            episodic_max=500,
            semantic_enabled=False,  # 暂不启用，避免额外依赖
        )

    # ── Agent 构建 ──

    def _build_system_prompt(self) -> str:
        """组装 system prompt：身份 > skill。"""
        if self._identity_prompt:
            return self._identity_prompt

        skill_path = self._get_skill_path()
        if os.path.exists(skill_path):
            with open(skill_path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    async def build(self):
        """构建 AgentFlow agent。"""
        if not self.SKILL_NAME:
            raise ValueError("SKILL_NAME 未设置")

        tools = self._build_tools()
        system_prompt = self._build_system_prompt()

        builder = (
            AgentBuilder(self.SKILL_NAME)
            .with_llm(self._ctx.agent_llm)
            .with_prompt(system_prompt)
            .with_tools(*tools)
            .with_memory(self._get_memory_profile())
            .with_thinking(ThinkingMode.REACT)
            .with_max_iterations(15)
        )

        return await builder.build()

    async def rebuild(self):
        """重建 agent，保留上一个 agent 的 WorkingMemory 上下文。

        用于 start_conversation / switch_character 后
        切换身份但保持对话连续性。
        """
        old_messages = []
        if self._built_agent:
            try:
                old_messages = list(self._built_agent.memory.working._messages)
            except Exception:
                pass

        self._built_agent = await self.build()

        # 恢复上下文（排除旧的 system prompt，新身份已替换）
        for msg in old_messages:
            if msg.role != "system":
                self._built_agent.memory.working.add(msg)

    async def run(self, task: str):
        """运行 agent。

        如果身份变了（set_identity 被调用），自动重建并保留对话上下文。
        """
        if self._built_agent is None:
            self._built_agent = await self.build()
        elif self._needs_rebuild:
            await self.rebuild()
            self._needs_rebuild = False
        return await self._built_agent.run(task)

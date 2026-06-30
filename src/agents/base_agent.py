# -*- coding: utf-8 -*-
"""BaseAgent —— Agent 抽象基类。

支持固定身份 system prompt + AgentFlow 集成。
"""

import os
from typing import Optional, TYPE_CHECKING

from agentflow.runtime.builder import AgentBuilder
from agentflow.runtime.memory.manager import MemoryProfile, WorkingConfig
from agentflow.runtime.thinking import ThinkingMode

from ..agent_memory import AgentMemory

if TYPE_CHECKING:
    from ..context import GlobalContext, ServiceRegistry
    from ..llm import UnifiedLLM

SKILLS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "skills",
)


class BaseAgent:
    """Agent 抽象基类。

    关键字段:
    - _identity_prompt: 固定身份 prompt，非空时替换 skill 内容作为 system prompt
    - _built_agent: 当前 AgentFlow agent 实例
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
        self._identity_prompt: str = ""
        self._built_agent = None
        self._needs_rebuild = False

    # ── 身份 Prompt ──

    @property
    def identity(self) -> str:
        return self._identity_prompt.split("\n")[0].replace("你是 ", "").rstrip("。") if self._identity_prompt else ""

    def set_identity(self, prompt: str):
        self._identity_prompt = prompt
        self._needs_rebuild = True

    def clear_identity(self):
        self._identity_prompt = ""
        self._needs_rebuild = True

    # ── 对话上下文 ──

    def _get_conversation_context(self, max_turns: int = 20) -> str:
        """从 AgentFlow WorkingMemory 提取最近的对话。"""
        if not self._built_agent:
            return ""
        try:
            messages = list(self._built_agent.memory.working._messages)
        except Exception:
            return ""

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
        return MemoryProfile(
            working=WorkingConfig(max_turns=30),
            episodic_max=500,
            semantic_enabled=False,
        )

    # ── Agent 构建 ──

    def _build_system_prompt(self) -> str:
        if self._identity_prompt:
            return self._identity_prompt

        skill_path = self._get_skill_path()
        if os.path.exists(skill_path):
            with open(skill_path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    async def build(self):
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
        old_messages = []
        if self._built_agent:
            try:
                old_messages = list(self._built_agent.memory.working._messages)
            except Exception:
                pass

        self._built_agent = await self.build()

        for msg in old_messages:
            if msg.role != "system":
                self._built_agent.memory.working.add(msg)

    async def run(self, task: str):
        if self._built_agent is None:
            self._built_agent = await self.build()
        elif self._needs_rebuild:
            await self.rebuild()
            self._needs_rebuild = False
        return await self._built_agent.run(task)

# -*- coding: utf-8 -*-
"""BaseAgent —— Agent 抽象基类。

支持 AgentFlow 集成 + skill 延迟加载 + 追踪日志。
"""

import logging
import os
from typing import Optional, TYPE_CHECKING

from agentflow.runtime.builder import AgentBuilder
from agentflow.runtime.memory.manager import MemoryProfile, WorkingConfig
from agentflow.runtime.thinking import ThinkingMode
from agentflow.runtime.hooks import StreamEvent

from ..agent_memory import AgentMemory

if TYPE_CHECKING:
    from ..context import GlobalContext, ServiceRegistry
    from ..llm import UnifiedLLM

SKILLS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "skills",
)

# Agent 追踪日志
_trace_logger = logging.getLogger("agentflow.trace")
if not _trace_logger.handlers:
    _fh = logging.FileHandler("agent_trace.log", encoding="utf-8")
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    _trace_logger.addHandler(_fh)
    _trace_logger.setLevel(logging.DEBUG)


class BaseAgent:
    """Agent 抽象基类。

    关键字段:
    - _identity_prompt: 固定身份 prompt，非空时叠加到 with_prompt
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

    async def build(self):
        if not self.SKILL_NAME:
            raise ValueError("SKILL_NAME 未设置")

        tools = self._build_tools()
        tool_names = [getattr(t, '__name__', str(t)) for t in tools]

        builder = (
            AgentBuilder(self.SKILL_NAME)
            .with_llm(self._ctx.agent_llm)
            .with_skills_dir(SKILLS_DIR)
            .with_skill(self.SKILL_NAME)
            .with_tools(*tools)
            .with_memory(self._get_memory_profile())
            .with_thinking(ThinkingMode.REACT)
            .with_max_iterations(15)
        )

        if self._identity_prompt:
            builder = builder.with_prompt(self._identity_prompt)

        agent = await builder.build()

        # 追踪：构建信息
        _trace_logger.info("[%s] build: skill=%s tools=%s max_iter=15",
                           self.SKILL_NAME, self.SKILL_NAME, tool_names)

        return agent

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

    # ── 追踪日志 ──

    def _trace_stream(self, event: StreamEvent):
        """Stream 回调：记录 AgentFlow ReAct 循环中的每个事件。"""
        etype = event.type
        if etype == "thinking":
            content = (event.content or "")[:200]
            _trace_logger.info("[%s] turn=%s thinking: %.200s",
                               self.SKILL_NAME, getattr(event, 'turn', '?'), content)
        elif etype == "tool_call":
            name = getattr(event, 'tool_name', '') or event.data.get('name', '?')
            args = event.data.get('args', {}) if event.data else {}
            args_str = str(args)[:300]
            _trace_logger.info("[%s] turn=%s tool_call: %s(%s)",
                               self.SKILL_NAME, getattr(event, 'turn', '?'), name, args_str)
        elif etype == "tool_result":
            name = getattr(event, 'tool_name', '') or event.data.get('name', '?')
            result = (event.content or "")[:300]
            _trace_logger.info("[%s] turn=%s tool_result: %s → %.300s",
                               self.SKILL_NAME, getattr(event, 'turn', '?'), name, result)
        elif etype == "final":
            _trace_logger.info("[%s] turn=%s final: %.300s",
                               self.SKILL_NAME, getattr(event, 'turn', '?'),
                               (event.content or "")[:300])
        elif etype == "error":
            _trace_logger.error("[%s] turn=%s error: %s",
                                self.SKILL_NAME, getattr(event, 'turn', '?'),
                                event.content or "")
        elif etype == "progress":
            _trace_logger.debug("[%s] progress: %s", self.SKILL_NAME, event.content or "")
        else:
            _trace_logger.debug("[%s] event=%s content=%.200s",
                                self.SKILL_NAME, etype, (event.content or "")[:200])

    # ── 运行 ──

    async def run(self, task: str):
        if self._built_agent is None:
            self._built_agent = await self.build()
        elif self._needs_rebuild:
            await self.rebuild()
            self._needs_rebuild = False

        _trace_logger.info("[%s] >>> task: %.300s", self.SKILL_NAME, task)

        result = await self._built_agent.run(task, stream=self._trace_stream)

        _trace_logger.info("[%s] <<< done", self.SKILL_NAME)

        # Post-turn 钩子：子类可覆盖以写入 episodic memory 等
        self._on_post_turn(task, result)
        return result

    def _on_post_turn(self, user_msg: str, assistant_msg: str):
        """Post-turn 钩子。子类覆盖以实现记忆写入等逻辑。"""
        pass

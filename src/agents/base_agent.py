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

from ..agent_memory import AgentMemory

if TYPE_CHECKING:
    from ..core.context import AppContext, ServiceRegistry
    from ..core.llm import UnifiedLLM

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
    _sh = logging.StreamHandler()
    _sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    _trace_logger.addHandler(_sh)
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
        ctx: "AppContext",
        services: "ServiceRegistry",
        llm: "UnifiedLLM" = None,  # 可选，优先从 services.llm 获取
        memory: Optional[AgentMemory] = None,
    ):
        self._ctx = ctx
        self._services = services
        self._llm = llm or (services.llm if services else None)
        self._memory = memory or AgentMemory()
        self._identity_prompt: str = ""
        self._built_agent = None
        self._needs_rebuild = False
        self._pending_references: dict[str, str] = {}  # Agent 未构建时暂存

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
        """子类可覆盖以调整记忆容量。"""
        return MemoryProfile(
            working=WorkingConfig(max_turns=30, max_tokens=8000),
            episodic_max=500,
            semantic_enabled=False,
        )

    # ── Reference 卡 ──

    def _get_references(self) -> dict[str, str]:
        """子类覆盖此方法以提供 Reference 卡内容。

        返回 {key: content} 映射。BaseAgent 负责在合适的时机推入 _built_agent。

        Reference 卡放什么:
          - 文风、角色 Voice、死活约束等不常变的基础信息
          - 跨 run() 持久、永不裁剪、不占 Working Memory 配额

        不放什么:
          - 每章/每节变化的内容（走 Dynamic Prefix 字符串）

        默认返回空（不推任何 reference）。
        """
        return {}

    def set_reference(self, key: str, content: str):
        """设置一条 Reference 卡。跨 run() 持久，永不裁剪。

        Agent 未构建时缓存到 _pending_references，build() 时自动应用。
        Agent 已构建时直接写入 _built_agent.reference。
        """
        if self._built_agent is not None:
            self._built_agent.set_reference(key, content)
        else:
            self._pending_references[key] = content

    def _flush_pending_references(self):
        """将缓存的 reference 写入已构建的 Agent。"""
        if self._built_agent is None or not self._pending_references:
            return
        for key, content in self._pending_references.items():
            self._built_agent.set_reference(key, content)
        self._pending_references.clear()

    def _apply_references(self):
        """从 _get_references() 拉取并推入 Reference 卡。"""
        for key, content in self._get_references().items():
            self.set_reference(key, content)

    # ── Agent 构建 ──

    def _load_skill_body(self) -> str:
        """从 skill 文件读取正文（跳过 YAML frontmatter）。

        供子类的 _get_system_prompt() 调用。
        """
        path = self._get_skill_path()
        if not os.path.exists(path):
            _trace_logger.warning("[%s] skill 文件不存在: %s", self.SKILL_NAME, path)
            return ""

        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            _trace_logger.warning("[%s] skill 读取失败: %s", self.SKILL_NAME, e)
            return ""

        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                return parts[2].strip()
        return content.strip()

    def _get_system_prompt(self) -> str:
        """子类覆盖此方法以提供自定义 system prompt。

        返回 ""  →  走 AgentFlow 懒加载（with_skill）
        返回非空  →  直接注入 system prompt（热加载，不走 use_skill_xxx）

        默认返回 ""（懒加载）。子类覆盖 + 调用 self._load_skill_body() 即可热加载。
        """
        return ""

    async def build(self):
        if not self.SKILL_NAME:
            raise ValueError("SKILL_NAME 未设置")

        if self._services is None or self._services.agent_llm is None:
            raise RuntimeError("services.agent_llm 未初始化，无法构建 Agent")

        tools = self._build_tools()
        tool_names = [getattr(t, '__name__', str(t)) for t in tools]

        # 系统 prompt：子类覆盖 _get_system_prompt() 决定懒加载还是热加载
        system_prompt = self._get_system_prompt()

        builder = (
            AgentBuilder(self.SKILL_NAME)
            .with_llm(self._services.agent_llm)
            .with_tools(*tools)
            .with_memory(self._get_memory_profile())
            .with_thinking(ThinkingMode.REACT)
            .with_max_iterations(15)
        )

        if system_prompt:
            # 热加载模式：skill body 直接注入 system prompt
            # identity_prompt 如果设置了就叠加在前面
            if self._identity_prompt:
                system_prompt = self._identity_prompt + "\n\n" + system_prompt
            builder = builder.with_prompt(system_prompt)
            _trace_logger.info("[%s] build: skill=%s (eager, %d chars) tools=%s max_iter=15",
                               self.SKILL_NAME, self.SKILL_NAME,
                               len(system_prompt), tool_names)
        else:
            # 懒加载模式：使用 AgentFlow with_skill（LLM 首轮调用 use_skill_xxx）
            builder = builder.with_skills_dir(SKILLS_DIR).with_skill(self.SKILL_NAME)
            if self._identity_prompt:
                builder = builder.with_prompt(self._identity_prompt)
            _trace_logger.info("[%s] build: skill=%s (lazy) tools=%s max_iter=15",
                               self.SKILL_NAME, self.SKILL_NAME, tool_names)

        agent = await builder.build()

        # 应用 build 前缓存的 reference 卡
        self._built_agent = agent
        self._flush_pending_references()

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

    # ── 运行 ──

    async def _live_stream(self, event):
        """实时进度回调：工具调用和思考过程即时输出到终端和日志。"""
        etype = event.type
        data = event.data or {}
        if etype == "tool_call":
            name = data.get("tool", "?")
            _trace_logger.info("[%s] → %s ...", self.SKILL_NAME, name)
        elif etype == "thinking":
            _trace_logger.debug("[%s] thinking: %.100s", self.SKILL_NAME,
                                (event.content or "")[:100])

    def _pre_run(self):
        """每次 run() 前自动拉取 _get_references() 推入 Reference 卡。

        子类只需覆盖 _get_references() 返回 {key: content} 映射即可。
        """
        self._apply_references()

    async def run(self, task: str):
        if self._built_agent is None:
            self._built_agent = await self.build()
        elif self._needs_rebuild:
            await self.rebuild()
            self._needs_rebuild = False

        # 子类钩子：更新 Reference 卡等
        self._pre_run()

        _trace_logger.info("[%s] >>> task: %.300s", self.SKILL_NAME, task)

        try:
            result = await self._built_agent.run(task, stream=self._live_stream)
        except Exception as e:
            _trace_logger.error("[%s] AgentFlow error: %s", self.SKILL_NAME, e)
            import traceback
            _trace_logger.error("[%s] Traceback:\n%s", self.SKILL_NAME,
                                traceback.format_exc())
            raise

        # AgentFlow 内置 Trace：记录每轮思维、工具调用、耗时、token
        self._log_agent_trace(result)

        _trace_logger.info("[%s] <<< done", self.SKILL_NAME)

        # Post-turn 钩子：子类可覆盖以写入 episodic memory 等
        self._on_post_turn(task, result)
        return result

    def _log_agent_trace(self, result):
        """将 AgentFlow 内置的 AgentTrace 写入日志。"""
        trace = getattr(result, 'agent_trace', None)
        if trace is None:
            _trace_logger.warning("[%s] agent_trace not available", self.SKILL_NAME)
            return

        turns = getattr(trace, 'turns', []) or []
        if not turns:
            return

        _trace_logger.info("[%s] === AgentTrace: %d turns ===",
                           self.SKILL_NAME, len(turns))

        for turn in turns:
            tn = getattr(turn, 'turn', '?')
            thinking = (getattr(turn, 'thinking', '') or '')[:200]
            if thinking:
                _trace_logger.info("[%s]   turn %s | thinking: %.200s",
                                   self.SKILL_NAME, tn, thinking)

            for tc in (getattr(turn, 'tool_calls', []) or []):
                tool = getattr(tc, 'tool', '?')
                inp = str(getattr(tc, 'input', {}))[:300]
                out = str(getattr(tc, 'output', ''))[:300]
                dur = getattr(tc, 'duration_ms', 0)
                success = getattr(tc, 'success', True)
                status = "✓" if success else "✗"
                _trace_logger.info("[%s]   turn %s | %s %s(%s) → %.300s (%dms)",
                                   self.SKILL_NAME, tn, status, tool, inp, out, dur)

            final = (getattr(turn, 'final_answer', '') or '')[:500]
            if final:
                _trace_logger.info("[%s]   turn %s | final: %.500s",
                                   self.SKILL_NAME, tn, final)

        total_turns = getattr(trace, 'total_turns', len(turns))
        total_calls = getattr(trace, 'total_tool_calls', 0)
        total_tokens = getattr(trace, 'total_tokens', {})
        total_ms = getattr(trace, 'total_duration_ms', 0)
        _trace_logger.info("[%s] === Trace summary: %d turns, %d tool calls, "
                           "tokens=%s, %dms ===",
                           self.SKILL_NAME, total_turns, total_calls,
                           total_tokens, total_ms)

    def _on_post_turn(self, user_msg: str, assistant_msg: str):
        """Post-turn 钩子。子类覆盖以实现记忆写入等逻辑。"""
        pass

# -*- coding: utf-8 -*-
"""BaseAgent —— Agent 抽象基类。

支持 AgentFlow 集成 + skill 热加载 + Reference 卡。
Trace 由 AgentFlow 内置 AgentTrace 负责，不自行实现。
"""

import json
import logging
import os
from typing import Optional, TYPE_CHECKING

from agentflow.runtime.builder import AgentBuilder
from agentflow.runtime.memory.manager import MemoryProfile, WorkingConfig
from agentflow.runtime.thinking import ThinkingMode

if TYPE_CHECKING:
    from ..pipeline.state import PipelineState

SKILLS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "skills",
)

logger = logging.getLogger("base_agent")
if not logger.handlers:
    _sh = logging.StreamHandler()
    _sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(_sh)
    logger.setLevel(logging.DEBUG)

# Trace 落盘：AgentFlow 的 AgentTrace 在此持久化
_trace_log = logging.getLogger("agentflow.trace")
if not _trace_log.handlers:
    _fh = logging.FileHandler("agent_trace.log", encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    _trace_log.addHandler(_fh)
    _trace_log.setLevel(logging.DEBUG)


class BaseAgent:
    """Agent 抽象基类。

    继承方法（子类覆盖）:
      - _get_system_prompt() → ""      热加载 skill body
      - _get_references()    → {}      Reference 卡内容
      - _get_memory_profile() → Profile  记忆配置
      - _build_tools()       → raise   工具列表
      - _on_post_turn()      → pass    后处理钩子

    Trace 由 AgentFlow 内置 AgentTrace 采集:
      - messages_snapshot: 每轮 LLM 调用前的完整 messages 快照
      - thinking / tool_calls / final_answer / tokens / duration_ms
    """

    SKILL_NAME: str = ""

    def __init__(
        self,
        agent_llm: object,
        kg: object,
        state: "PipelineState",
    ):
        self._agent_llm = agent_llm
        self._kg = kg
        self._state = state
        self._identity_prompt: str = ""
        self._built_agent = None
        self._needs_rebuild = False
        self._pending_references: dict[str, str] = {}

        # 子类属性默认值
        self._character_profiles = getattr(state, 'character_profiles', {})
        self._character_statuses: dict = {}
        self._style_profile = getattr(state, 'style_profile', None)

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

    def _get_thinking_mode(self):
        """子类覆盖以使用不同的思考模式。默认 ReAct。"""
        return ThinkingMode.REACT

    def _get_memory_profile(self) -> MemoryProfile:
        return MemoryProfile(
            working=WorkingConfig(max_turns=30, max_tokens=8000),
            episodic_max=500,
            semantic_enabled=False,
        )

    # ── System Prompt ──

    def _load_skill_body(self) -> str:
        path = self._get_skill_path()
        if not os.path.exists(path):
            logger.warning("[%s] skill 文件不存在: %s", self.SKILL_NAME, path)
            return ""

        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.warning("[%s] skill 读取失败: %s", self.SKILL_NAME, e)
            return ""

        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                return parts[2].strip()
        return content.strip()

    def _get_system_prompt(self) -> str:
        """子类覆盖。返回 "" 走懒加载，返回非空走热加载。"""
        return ""

    # ── Reference 卡 ──

    def _get_references(self) -> dict[str, str]:
        """默认实现：角色状态硬约束 + 文风概要 + 核心角色 Voice。

        子类可覆盖以追加 Agent 专属 Reference 卡。
        """
        refs = {}

        # ── 角色状态硬约束 ──
        if self._character_statuses:
            dead = [n for n, s in self._character_statuses.items()
                    if s in ("dead", "deceased", "killed")]
            missing = [n for n, s in self._character_statuses.items()
                       if s == "missing"]
            lines = []
            if dead:
                lines.append(f"已死亡: {', '.join(dead)}（只能以回忆/闪回出现）")
            if missing:
                lines.append(f"下落不明: {', '.join(missing)}（不能直接出场）")
            if lines:
                refs["character_statuses"] = "\n".join(lines)

        # ── 文风概要 ──
        if self._style_profile:
            atmos = self._style_profile.atmosphere
            narrative = self._style_profile.narrative
            lines = []
            if atmos.overall_tone:
                lines.append(f"基调: {atmos.overall_tone}")
            if atmos.emotional_tendency:
                lines.append(f"情感倾向: {atmos.emotional_tendency}")
            if narrative.cliffhanger_style:
                lines.append(f"章尾钩子: {narrative.cliffhanger_style}")
            if narrative.scene_transition_style:
                lines.append(f"场景过渡: {narrative.scene_transition_style}")
            if lines:
                refs["style_profile"] = "\n".join(lines)

        # ── 核心角色 Voice 概要 ──
        if self._character_profiles:
            voice_lines = []
            for name, profile in list(self._character_profiles.items())[:8]:
                if hasattr(profile, 'voice') and profile.voice:
                    v = profile.voice
                    parts = [f"{name}:"]
                    if v.summary:
                        parts.append(f"  {v.summary[:100]}")
                    if v.taboo_words:
                        parts.append(f"  禁用词: {', '.join(v.taboo_words[:5])}")
                    voice_lines.append("\n".join(parts))
            if voice_lines:
                refs["character_voices"] = "\n\n".join(voice_lines)

        return refs

    def set_reference(self, key: str, content: str):
        if self._built_agent is not None:
            self._built_agent.set_reference(key, content)
        else:
            self._pending_references[key] = content

    def _flush_pending_references(self):
        if self._built_agent is None or not self._pending_references:
            return
        for key, content in self._pending_references.items():
            self._built_agent.set_reference(key, content)
        self._pending_references.clear()

    def _apply_references(self):
        for key, content in self._get_references().items():
            self.set_reference(key, content)

    # ── Agent 构建 ──

    async def build(self):
        if not self.SKILL_NAME:
            raise ValueError("SKILL_NAME 未设置")

        if self._agent_llm is None:
            raise RuntimeError("agent_llm 未初始化，无法构建 Agent")

        tools = self._build_tools()

        system_prompt = self._get_system_prompt()

        builder = (
            AgentBuilder(self.SKILL_NAME)
            .with_llm(self._agent_llm)
            .with_tools(*tools)
            .with_memory(self._get_memory_profile())
            .with_thinking(self._get_thinking_mode())
            .with_max_iterations(15)
        )

        if system_prompt:
            if self._identity_prompt:
                system_prompt = self._identity_prompt + "\n\n" + system_prompt
            builder = builder.with_prompt(system_prompt)
        else:
            builder = builder.with_skills_dir(SKILLS_DIR).with_skill(self.SKILL_NAME)
            if self._identity_prompt:
                builder = builder.with_prompt(self._identity_prompt)

        agent = await builder.build()

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
        """实时进度回调：工具调用即时输出到终端。"""
        etype = event.type
        data = event.data or {}
        if etype == "tool_call":
            name = data.get("tool", "?")
            logger.info("[%s] → %s ...", self.SKILL_NAME, name)

    def _pre_run(self):
        self._apply_references()

    async def run(self, task: str):
        if self._built_agent is None:
            self._built_agent = await self.build()
        elif self._needs_rebuild:
            await self.rebuild()
            self._needs_rebuild = False

        self._pre_run()

        print(f"[{self.SKILL_NAME}] running task ({len(task)} chars)...", flush=True)
        try:
            result = await self._built_agent.run(task, stream=self._live_stream)
        except Exception as e:
            logger.error("[%s] AgentFlow error: %s", self.SKILL_NAME, e)
            print(f"[{self.SKILL_NAME}] ERROR: {e}", flush=True)
            raise

        output = getattr(result, 'output', str(result))
        print(f"[{self.SKILL_NAME}] done, output ({len(output)} chars): {output[:200]}...", flush=True)

        # AgentFlow 内置 Trace 落盘
        trace = getattr(result, 'agent_trace', None)
        print(f"[{self.SKILL_NAME}] agent_trace present: {trace is not None}", flush=True)
        if trace is not None:
            try:
                data = trace.to_dict()
                _trace_log.info(json.dumps(data, ensure_ascii=False, indent=2))
                print(f"[{self.SKILL_NAME}] trace dumped ({trace.total_turns} turns)", flush=True)
            except Exception as e:
                print(f"[{self.SKILL_NAME}] trace dump failed: {e}", flush=True)

        self._on_post_turn(task, result)
        return result

    def _on_post_turn(self, user_msg: str, assistant_msg: str):
        """Post-turn 钩子。子类覆盖以实现记忆写入等逻辑。"""
        pass

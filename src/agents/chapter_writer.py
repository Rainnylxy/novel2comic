# -*- coding: utf-8 -*-
"""ChapterWriter —— 章节写手 Agent（AgentFlow ReAct 模式）。

逐节写作，AgentFlow 自动管理上下文窗口:
  - System Prompt: skills/chapter_writer.md（缓存）
  - Dynamic Prefix: 角色状态硬约束 + 文风概要（Push）
  - Tools: lookup_character / recall_foreshadowing（Pull）

流程:
  Thought → 分析本节目标 → lookup_character("江停") → 确认 Voice/边界 →
  Thought → recall_foreshadowing() → 提取伏笔 →
  Thought → 写本节 3-6 个 StoryFragment → 自然终止
"""

import json
import asyncio
import logging
from typing import TYPE_CHECKING

from agentflow.runtime.toolkit import tool

from ..agents.base_agent import BaseAgent

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..context import GlobalContext, ServiceRegistry
    from ..llm import UnifiedLLM


class ChapterWriter(BaseAgent):
    """章节写手 —— AgentFlow ReAct 模式。

    继承 BaseAgent，通过 ReAct 循环逐节写作。
    AgentFlow 自动管理 WorkingMemory 上下文窗口，
    KG 查询通过 lookup_character / recall_foreshadowing 按需触发。
    """

    SKILL_NAME = "chapter_writer"

    def __init__(self, ctx, services, llm, memory=None):
        super().__init__(ctx, services, llm, memory)
        self._kg = services.kg

        # 运行时上下文（set_context 一次性注入整个 chapter 结构体）
        self._chapter: dict = {}
        self._style_profile = None
        self._previous_chapter_ending: str = ""
        self._character_profiles: dict = {}
        self._character_statuses: dict = {}
        self._graph = None

        # 角色查询缓存（同章内避免重复查 KG，set_context 时清空）
        self._lookup_cache: dict = {}

        # 流式输出队列
        self._fragment_queue: asyncio.Queue = asyncio.Queue()
        # 注入信号
        self._inject_instruction: str = ""
        self._inject_event: asyncio.Event = asyncio.Event()

    # ================================================================
    # 上下文设置
    # ================================================================

    def set_context(
        self,
        chapter: dict,
        style_profile,
        character_profiles: dict,
        character_statuses: dict = None,
        graph=None,
        previous_chapter_ending: str = "",
    ):
        """设置 Writer 运行时上下文 —— 整章一次性注入。

        chapter 结构体包含:
          - chapter_number, title, synopsis, tone
          - character_beats: {name: {arc, key_action, emotional_beat}}
          - sections: [{name, goal, characters, key_beats, target_fragments}]

        Args:
            chapter: 完整章节规划 dict（来自 PlotArchitect 输出）
            style_profile: AuthorStyleProfile
            character_profiles: 角色蒸馏 Profile（lookup_character 工具按需查询）
            character_statuses: 角色状态映射（dead/missing/active）
            graph: StoryGraph（Writer 用它按需查询角色信息）
            previous_chapter_ending: 上一章结尾衔接文本
        """
        self._chapter = chapter
        self._style_profile = style_profile
        self._previous_chapter_ending = previous_chapter_ending
        self._character_profiles = character_profiles or {}
        self._character_statuses = character_statuses or {}
        self._graph = graph
        self._lookup_cache.clear()  # 新章重置角色查询缓存
        # System prompt 来自 skills/chapter_writer.md（AgentFlow 缓存）
        # 动态上下文通过 _build_dynamic_prefix() 注入 task

    def _build_dynamic_prefix(self) -> str:
        """构建 Push 上下文（拼接到 task 前，不缓存）。

        只包含变化的数据：角色状态硬约束 + 风格概要。
        角色 Voice/Boundary 细节通过 lookup_character 工具 Pull。
        """
        parts = []

        # 角色生死状态（硬约束 —— Push）
        if self._character_statuses:
            dead_chars = [n for n, s in self._character_statuses.items()
                          if s in ("dead", "deceased", "killed")]
            missing_chars = [n for n, s in self._character_statuses.items()
                             if s == "missing"]
            if dead_chars:
                parts.append(f"## ⚠️ 已死亡角色: {', '.join(dead_chars)}")
                parts.append("只能以回忆/闪回/他人提及出现，绝不能写他们的对话或动作。")
            if missing_chars:
                parts.append(f"## ⚠️ 下落不明角色: {', '.join(missing_chars)}")
                parts.append("不能直接出场，只能通过线索/回忆间接涉及。")

        # 文风概要（轻量 Push）
        if self._style_profile:
            parts.append(self._style_profile.summary())
            exemplars_text = self._style_profile.exemplars_text()
            if exemplars_text:
                parts.append(exemplars_text)

        return "\n".join(parts) if parts else ""

    # ================================================================
    # 流式接口（供 Pipeline 调用）
    # ================================================================

    async def inject(self, instruction: str):
        """注入用户指令。触发 AgentFlow 中断。"""
        self._inject_instruction = instruction
        self._inject_event.set()

    async def stream(self, section: dict, section_index: int = 0):
        """流式生成一个小节的内容。

        Pipeline 逐节调用此方法。chapter 结构体已在 set_context 中注入。

        Yields:
            StoryFragment
        """
        from .fragment import StoryFragment

        # 构建完整 task: 动态前缀（Push 上下文）+ 用户 prompt（本章+本节信息）
        user_prompt = self._build_user_prompt(section, section_index)
        prefix = self._build_dynamic_prefix()
        task = (prefix + "\n\n" + user_prompt) if prefix else user_prompt
        self._fragment_queue = asyncio.Queue()

        # 启动 AgentFlow ReAct 循环（异步，结果通过 queue 流出）
        run_task = asyncio.create_task(self._run_and_collect(task))

        # 从 queue 中读取片段流
        while True:
            try:
                item = await asyncio.wait_for(
                    self._fragment_queue.get(), timeout=0.5,
                )
                if item is None:  # 结束标记
                    break
                yield item

                # 检查注入信号
                if self._inject_event.is_set():
                    self._inject_event.clear()
                    instruction = self._inject_instruction
                    self._inject_instruction = ""
                    # 注入指令作为新消息进入 AgentFlow 对话
                    await self._built_agent.run(
                        f"[用户指令] {instruction}\n请根据这个指令调整后续写作。"
                    )
                    # 后续片段会继续通过 queue 流出

            except asyncio.TimeoutError:
                if run_task.done():
                    break

        await run_task

    async def _run_and_collect(self, task: str):
        """运行 AgentFlow ReAct 循环，采集 write_section 产出的片段。"""
        try:
            result = await super().run(task)
            # 标记结束
            await self._fragment_queue.put(None)
        except Exception as e:
            logger.warning("ChapterWriter run error: %s", e)
            await self._fragment_queue.put(None)

    def _build_user_prompt(self, section: dict, section_index: int) -> str:
        """构建 user prompt —— 本章上下文 + 本节信息。"""
        ch = self._chapter
        sections = ch.get("sections", [])
        total = len(sections)

        parts = [
            f"## 本章: 第{ch.get('chapter_number', '?')}章「{ch.get('title', '')}」",
            f"本章梗概: {ch.get('synopsis', '')}",
            f"本章基调: {ch.get('tone', '')}",
        ]

        # 角色节拍（本章各角色的情绪轨迹）
        beats = ch.get("character_beats", {})
        if beats:
            parts.append("\n## 本章角色节拍")
            for name, beat in beats.items():
                parts.append(f"- {name}: {beat.get('arc', '')} | "
                             f"关键行动: {beat.get('key_action', '')} | "
                             f"情感时刻: {beat.get('emotional_beat', '')}")

        # 当前小节
        parts.append(f"\n## 本节任务: {section.get('name', '')} "
                      f"({section_index + 1}/{total})")
        parts.append(f"叙事目标: {section.get('goal', '')}")

        # 基调
        tone = section.get("tone", "") or ch.get("tone", "")
        if tone:
            parts.append(f"基调: {tone}")

        # 本节出场角色
        characters = section.get("characters", [])
        if characters:
            parts.append(f"\n本节主要角色: {', '.join(characters)}")
            parts.append("如需了解这些角色的最新状态，请调用 lookup_character。")

        # 情节点
        key_beats = section.get("key_beats", [])
        if key_beats:
            parts.append(f"\n本节需要覆盖的情节点:")
            for beat in key_beats:
                parts.append(f"  - {beat}")

        # 目标片段数
        target = section.get("target_fragments", 5)
        parts.append(f"\n目标: 写 {target} 个 StoryFragment JSON 片段后自然终止。")

        # 衔接文本
        if self._previous_chapter_ending:
            ending = self._previous_chapter_ending
            parts.append(f"\n## 衔接上下文")
            parts.append(ending[-800:] if len(ending) > 800 else ending)

        return "\n".join(parts)

    # ================================================================
    # ReAct 工具
    # ================================================================

    def _build_tools(self) -> list:
        self_ref = self  # 闭包引用，动态读取 _lookup_cache 等字段
        ctx = self._ctx
        kg = self._kg
        graph = self._graph
        char_profiles = self._character_profiles
        char_statuses = self._character_statuses
        style_profile = self._style_profile
        previous_ending = self._previous_chapter_ending
        queue = self._fragment_queue

        @tool
        def lookup_character(name: str) -> str:
            """查询角色的当前状态、关系和关键事件。

            当需要写某个角色但不清楚其最新状态时调用。
            不会一次性加载所有角色，而是按需查询。

            Args:
                name: 角色名

            Returns:
                角色的 KG 信息摘要
            """
            # 同章缓存：已查过的角色直接返回，避免重复查询 KG
            cached = self_ref._lookup_cache.get(name)
            if cached is not None:
                return cached

            if not graph:
                return f"KG 不可用，无法查询 {name}"

            person = graph.get_person_node(name) if hasattr(graph, 'get_person_node') else None
            if not person:
                return f"KG 中未找到角色「{name}」"

            lines = [
                f"角色: {name}",
                f"状态: {person.status}",
                f"身份: {person.role_type}",
                f"派系: {person.faction}",
                f"简介: {person.description or '无'}",
            ]

            # 最后参与的事件
            if hasattr(graph, 'character_events'):
                events = graph.character_events(name)
                if events:
                    last = events[-1]
                    lines.append(f"\n最后事件（第{last.get('chapter_start','?')}章）: "
                                 f"{last.get('name','?')} — {last.get('summary','')[:120]}")

            # 关系
            if hasattr(graph, 'relationship_edges'):
                rels = []
                for edge in graph.relationship_edges:
                    if edge.from_char == name:
                        rels.append(
                            f"对{edge.to_char}: {edge.relation_type}"
                            + (f"(亲密度:{edge.intimacy:+d})" if edge.intimacy else "")
                        )
                    elif edge.to_char == name:
                        rels.append(
                            f"被{edge.from_char}: {edge.relation_type}"
                            + (f"(亲密度:{edge.intimacy:+d})" if edge.intimacy else "")
                        )
                if rels:
                    lines.append(f"\n关系: {'; '.join(rels[:5])}")

            # 状态硬约束提示
            status = char_statuses.get(name, person.status)
            if status in ("dead", "deceased", "killed"):
                lines.append("\n⚠️ 此角色已死亡！只能以回忆/闪回/他人提及出现。")
            elif status == "missing":
                lines.append("\n⚠️ 此角色下落不明！不能直接出场。")

            # 角色蒸馏档案（按需 Pull，不从 system prompt 推送）
            profile = char_profiles.get(name)
            if profile:
                # Voice: 语气 + 句式 + 禁忌
                if hasattr(profile, 'voice') and profile.voice:
                    v = profile.voice
                    if v.summary:
                        lines.append(f"\nVoice: {v.summary}")
                    # 语气光谱
                    tone_parts = []
                    if v.tone_cold_warm:
                        tone_parts.append(f"冷暖={v.tone_cold_warm:.1f}")
                    if v.tone_hard_soft:
                        tone_parts.append(f"硬软={v.tone_hard_soft:.1f}")
                    if v.tone_distant_close:
                        tone_parts.append(f"疏近={v.tone_distant_close:.1f}")
                    if tone_parts:
                        lines.append(f"语气光谱: {', '.join(tone_parts)} (0=冷/硬/疏, 1=暖/软/近)")
                    if v.rhythm:
                        lines.append(f"说话节奏: {v.rhythm}")
                    if v.response_pattern:
                        lines.append(f"回应模式: {v.response_pattern}")
                    if v.taboo_words:
                        lines.append(f"禁用词: {', '.join(v.taboo_words)}")
                    if v.taboo_patterns:
                        lines.append(f"禁用句式: {', '.join(v.taboo_patterns)}")
                    # 对不同对象的表达差异
                    if v.voice_shift:
                        shifts = [f"对{k}: {vv}" for k, vv in v.voice_shift.items()]
                        if shifts:
                            lines.append(f"表达差异: {'; '.join(shifts[:5])}")

                # Boundary: 硬底线 + 行为倾向 + 关系行为
                if hasattr(profile, 'boundary') and profile.boundary:
                    b = profile.boundary
                    if b.hard_rules:
                        lines.append(f"硬底线: {', '.join(b.hard_rules[:5])}")
                    if b.tendencies:
                        lines.append(f"行为倾向: {', '.join(b.tendencies[:5])}")
                    if b.relationship_behaviors:
                        rb = [f"对{k}: {v}" for k, v in b.relationship_behaviors.items()]
                        if rb:
                            lines.append(f"关系行为: {'; '.join(rb[:5])}")

                # Sensitivity: 敏感触发点
                if hasattr(profile, 'sensitivity') and profile.sensitivity:
                    entries = profile.sensitivity.entries
                    if entries:
                        lines.append(f"敏感触发 ({len(entries)}项):")
                        for e in entries[:3]:
                            triggers = ', '.join(e.triggers[:3]) if hasattr(e, 'triggers') else ''
                            effects = str(e.effects)[:80] if hasattr(e, 'effects') else ''
                            if triggers:
                                lines.append(f"  · {triggers} → {effects}")

                # Policy Anchors: 典型行为模式
                if hasattr(profile, 'policy_anchors') and profile.policy_anchors:
                    anchors = profile.policy_anchors
                    if anchors:
                        lines.append(f"行为锚点 ({len(anchors)}个):")
                        for a in anchors[:3]:
                            situation = getattr(a, 'situation', '') or ''
                            action = getattr(a, 'action', '') or ''
                            if situation and action:
                                lines.append(f"  · {situation} → {action}")

                # State: 心理基线
                if hasattr(profile, 'state') and profile.state:
                    baseline = profile.state.baseline
                    if baseline:
                        base_str = ', '.join(f"{k}={v}" for k, v in list(baseline.items())[:5])
                        lines.append(f"心理基线: {base_str}")

            result = "\n".join(lines)
            self_ref._lookup_cache[name] = result  # 缓存，同章内复用
            return result

        @tool
        def recall_foreshadowing() -> str:
            """查询 KG 中所有未解决的伏笔和因果链。

            当需要推进情节但不确定有哪些未完成线索时调用。

            Returns:
                未解决伏笔列表
            """
            if not graph:
                return "KG 不可用"

            # 因果链
            hanging = []
            for edge in graph.event_relation_edges:
                if edge.relation_type == "causes":
                    ev = graph.get_event_node(
                        edge.from_event.split(":", 1)[-1]
                    ) if hasattr(graph, 'get_event_node') else None
                    if ev and ev.effect:
                        hanging.append(
                            f"「{ev.name}」(第{ev.chapter_start}章): {ev.effect[:100]}"
                        )

            if not hanging:
                return "KG 中暂无未解决伏笔记录"

            return "未解决伏笔:\n" + "\n".join(
                f"- {h}" for h in hanging[:10]
            )

        return [
            lookup_character,
            recall_foreshadowing,
        ]

    # ================================================================
    # Post-turn: 从 LLM 自然终止输出中提取 StoryFragment
    # ================================================================

    def _on_post_turn(self, user_msg: str, assistant_msg):
        """从 AgentFlow 自然终止输出中解析 StoryFragment。

        assistant_msg 可能是 str 或 AgentFlow 的 AgentResult 对象。
        """
        from .fragment import StoryFragment

        if not assistant_msg:
            return

        # AgentFlow 的 AgentResult 用 .output，工具返回值可能是 str
        from agentflow.runtime.builder import AgentResult
        if isinstance(assistant_msg, AgentResult):
            text = assistant_msg.output
        elif isinstance(assistant_msg, str):
            text = assistant_msg
        else:
            text = str(assistant_msg)

        if not text or not text.strip():
            return

        # 尝试按行解析 JSON fragment
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            fragment = StoryFragment.parse_stream_line(line)
            if fragment:
                try:
                    self._fragment_queue.put_nowait(fragment)
                except asyncio.QueueFull:
                    pass

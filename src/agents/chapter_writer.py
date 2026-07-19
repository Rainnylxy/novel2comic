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

from .base_agent import BaseAgent

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..pipeline.state import PipelineState


class ChapterWriter(BaseAgent):
    """章节写手 —— AgentFlow ReAct 模式。

    继承 BaseAgent，通过 ReAct 循环逐节写作。
    AgentFlow 自动管理 WorkingMemory 上下文窗口，
    KG 查询通过 lookup_character / recall_foreshadowing 按需触发。
    """

    SKILL_NAME = "chapter_writer"

    def _get_system_prompt(self) -> str:
        """热加载：skill body 直接注入 system prompt，不走 use_skill_xxx。"""
        return self._load_skill_body()

    def __init__(self, agent_llm, kg, state: "PipelineState"):
        super().__init__(agent_llm, kg, state)
        self._kg = kg

        # 运行时上下文（set_context 一次性注入整个 chapter 结构体）
        self._chapter: dict = {}
        self._previous_chapter_ending: str = ""
        self._plot_threads: list = []
        self._graph = self._state.graph

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
        previous_chapter_ending: str = "",
        plot_threads: list = None,
    ):
        """设置 Writer 运行时上下文 —— 整章一次性注入。

        Args:
            chapter: 完整章节规划 dict
            previous_chapter_ending: 上一章结尾衔接文本
            plot_threads: 路线图 + 前序章节引入的新伏笔列表
        """
        self._chapter = chapter
        self._previous_chapter_ending = previous_chapter_ending
        self._plot_threads = plot_threads or []
        self._graph = self._state.graph
        # 从 PipelineState 同步每章会变的状态
        self._character_statuses = self._state.character_statuses
        self._style_profile = self._state.style_profile
        self._character_profiles = self._state.character_profiles
        self._lookup_cache.clear()

        # 预加载本章涉及的所有角色
        self._preloaded_chars_text = self._preload_characters()

    def _preload_characters(self) -> str:
        """预加载本章涉及的所有角色，返回格式化摘要供注入 user prompt。"""
        ch = self._chapter
        # 收集本章所有角色名
        names = set()
        for name in (ch.get("character_beats") or {}).keys():
            names.add(name)
        for section in (ch.get("sections") or []):
            for name in (section.get("characters") or []):
                names.add(name)
        if not names:
            return ""

        lines = ["## 本章角色档案（已预加载，无需再 lookup_character）"]
        for name in sorted(names):
            # 状态
            status = self._character_statuses.get(name, "active")
            dead_warn = ""
            if status in ("dead", "deceased", "killed"):
                dead_warn = " ⚠️已死亡！只能以回忆/闪回/他人提及出现。"
            elif status == "missing":
                dead_warn = " ⚠️下落不明！不能直接出场。"

            lines.append(f"\n### {name} ({status}){dead_warn}")

            # KG 基础信息
            graph = self._graph
            if graph:
                person = (graph.get_person_node(name)
                          if hasattr(graph, 'get_person_node') else None)
                if person:
                    lines.append(f"叙事角色: {person.role_type} | 所属: {person.faction}")
                    # description 含关键职业/身份信息，优先展示
                    if person.description:
                        lines.append(f"身份: {person.description}")
                    # Dossier
                    if person.ending:
                        lines.append(f"结局: {person.ending}")
                    if person.foreshadowing:
                        lines.append(f"伏笔: {person.foreshadowing}")
                    # 关系
                    if hasattr(graph, 'relationship_edges'):
                        rels = []
                        for edge in graph.relationship_edges:
                            if edge.from_char == name:
                                rels.append(f"对{edge.to_char}: {edge.relation_type}"
                                            + (f"(亲密度:{edge.intimacy:+d})" if edge.intimacy else ""))
                            elif edge.to_char == name:
                                rels.append(f"被{edge.from_char}: {edge.relation_type}"
                                            + (f"(亲密度:{edge.intimacy:+d})" if edge.intimacy else ""))
                        if rels:
                            lines.append(f"关系: {'; '.join(rels[:5])}")

            # 蒸馏 Profile（Voice + Boundary + Sensitivity + Anchors）
            profile = self._character_profiles.get(name)
            if profile:
                v = getattr(profile, 'voice', None)
                if v and v.summary:
                    lines.append(f"Voice: {v.summary}")
                    if v.taboo_words:
                        lines.append(f"禁用词: {', '.join(v.taboo_words)}")
                b = getattr(profile, 'boundary', None)
                if b and b.hard_rules:
                    lines.append(f"硬底线: {', '.join(b.hard_rules[:5])}")
                sens = getattr(profile, 'sensitivity', None)
                if sens and sens.entries:
                    for e in sens.entries[:2]:
                        t = ', '.join(getattr(e, 'triggers', [])[:3])
                        if t:
                            lines.append(f"敏感触发: {t}")
                anchors = getattr(profile, 'policy_anchors', None)
                if anchors:
                    for a in anchors[:2]:
                        s = getattr(a, 'situation', '') or ''
                        act = getattr(a, 'action', '') or ''
                        if s and act:
                            lines.append(f"行为锚点: {s} → {act}")

        return "\n".join(lines) if len(lines) > 1 else ""

    # ================================================================
    # 流式接口（供 Pipeline 调用）
    # ================================================================

    async def inject(self, instruction: str):
        """注入用户指令。触发 AgentFlow 中断。"""
        self._inject_instruction = instruction
        self._inject_event.set()

    async def stream(self, section: dict, section_index: int = 0):
        """流式生成一个小节的内容。"""
        from ..pipeline.fragment import StoryFragment

        print(f"  [Writer] stream({section.get('name','?')}, idx={section_index}) 开始构建 task...",
              flush=True)
        user_prompt = self._build_user_prompt(section, section_index)
        task = user_prompt
        print(f"  [Writer] task 构建完成 ({len(task)} 字), 启动 ReAct...", flush=True)
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
        print("  [Writer] _run_and_collect: 进入 BaseAgent.run()...", flush=True)
        try:
            result = await super().run(task)
            print(f"  [Writer] _run_and_collect: ReAct 完成, output 长度={len(str(result))}",
                  flush=True)
            await self._fragment_queue.put(None)
        except Exception as e:
            logger.warning("ChapterWriter run error: %s", e)
            print(f"  [Writer] _run_and_collect 异常: {e}", flush=True)
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

        # 预加载角色档案（第一节注入，后续节共享上下文无需重复）
        if section_index == 0 and self._preloaded_chars_text:
            parts.append(self._preloaded_chars_text)

        # 角色节拍（本章各角色的情绪轨迹）
        beats = ch.get("character_beats", {})
        if beats:
            parts.append("\n## 本章角色节拍")
            for name, beat in beats.items():
                parts.append(f"- {name}: {beat.get('arc', '')} | "
                             f"关键行动: {beat.get('key_action', '')} | "
                             f"情感时刻: {beat.get('emotional_beat', '')}")

        # 叙事约束（新增）
        emotion = ch.get("emotion_arc", "")
        if emotion:
            parts.append(f"本章情绪目标: {emotion}")

        rhythm = ch.get("rhythm_position", "")
        if rhythm:
            rhythm_hints = {
                "高压": "对话紧、动作密、爽点集中释放，少用大段旁白",
                "推进": "主线叙事节奏，对话与动作交替推进",
                "关系": "侧重人物互动和情感流动，对话可以更细腻",
                "低压": "允许更多旁白和日常描写，但每节仍需有往下看的理由",
            }
            hint = rhythm_hints.get(rhythm, "")
            parts.append(f"本章节奏定位: {rhythm}" + (f"（{hint}）" if hint else ""))

        hook_type = ch.get("closing_hook_type", "")
        if hook_type:
            parts.append(f"章尾钩子类型: {hook_type} — 确保章尾给读者这个类型的下读理由")

        forbidden = ch.get("forbidden_releases", [])
        if forbidden:
            parts.append(f"⚠ 本章禁止出现: {', '.join(forbidden)}（封禁列表，不得在正文中提及）")

        # 伏笔上下文
        sm = self._state.story_memory
        if sm:
            foreshadowing = sm.foreshadowing_ledger.get_for_chapter(ch.get("chapter_number", 0))
            to_resolve = ch.get("foreshadowing_resolved", [])
            to_advance = ch.get("foreshadowing_advanced", [])
            if to_resolve or to_advance:
                parts.append("\n## 本章伏笔任务")
                if to_resolve:
                    parts.append(f"必须回收: {', '.join(to_resolve)}")
                if to_advance:
                    parts.append(f"必须推进: {', '.join(to_advance)}")
            if foreshadowing:
                parts.append(f"待处理的伏笔: {', '.join(e.id for e in foreshadowing)}")

        # 叙事参考
        if sm:
            narrative_ctx = sm.get_narrative_context(n=5)
            if narrative_ctx and "暂无" not in narrative_ctx:
                parts.append("\n## 最近章节叙事特征参考")
                parts.append(narrative_ctx)

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
        target = section.get("target_paragraphs", 5)
        parts.append(f"\n篇幅: 写 {target} 个自然段落后自然终止。")

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
        # 共享工具（来自 SharedToolKit，启用同章缓存）
        shared = self._make_shared_toolkit()
        lookup_character = shared.make_lookup_character()
        recall_foreshadowing = shared.make_recall_foreshadowing()

        return [
            lookup_character,
            recall_foreshadowing,
        ]

    def _make_shared_toolkit(self):
        """创建 SharedToolKit 实例。每次 set_context 后缓存刷新。"""
        from ..tools import SharedToolKit
        return SharedToolKit(
            graph=self._state.graph,
            kg=self._kg,
            character_profiles=self._state.character_profiles,
            character_statuses=self._character_statuses,
            plot_threads=self._plot_threads,
            story_memory=self._state.story_memory,
            enable_cache=True,
        )

    # ================================================================
    # Post-turn: prose → StoryFragment（Fragmentizer 规则层）
    # ================================================================

    def _on_post_turn(self, user_msg: str, assistant_msg):
        """Writer 输出自然段落，Fragmentizer 转为前端 fragment。"""
        from agentflow.runtime.builder import AgentResult
        from ..pipeline.fragmentizer import Fragmentizer

        if not assistant_msg:
            return

        text = assistant_msg.output if isinstance(assistant_msg, AgentResult) else str(assistant_msg)
        if not text or not text.strip():
            return

        for frag in Fragmentizer().process(text):
            try:
                self._fragment_queue.put_nowait(frag)
            except asyncio.QueueFull:
                pass

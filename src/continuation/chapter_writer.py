# -*- coding: utf-8 -*-
"""ChapterWriter —— 章节写手 Agent（AgentFlow ReAct 模式）。

逐节写作，AgentFlow 自动管理上下文窗口:
  - System Prompt: 文风约束 + 输出格式（缓存）
  - WorkingMemory: 大纲 + 已写章节（滑动窗口）
  - Tools: lookup_character / recall_foreshadowing / write_section

流程:
  Thought → 分析大纲 → write_section("opening") → 流式输出 →
  Thought → lookup_character("金杰") → 确认状态 →
  Thought → write_section("rising") → 流式输出 → ...
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

        # 构建 system prompt（会被 AgentFlow 缓存）
        self.set_identity(self._build_system_prompt())

    def _build_system_prompt(self) -> str:
        """构建 Writer 的 system prompt（缓存，不随小节变化）。

        Push（锚点）: 角色状态硬约束 + 风格核心标签
        Pull（工具）: 角色 Voice/Boundary → lookup_character(name)
        """
        parts = [
            "## 角色",
            "你是专业小说续写者。原文已完结，你写的是全新的后续故事，不是补全。"
            "每次调用你只写一个小节（3-6个 StoryFragment），向前推进剧情。",
            "",
            "## 工作流程",
            "1. 阅读本节目标和情节点",
            "2. **先用 lookup_character(name) 查询本节涉及角色的 Voice 和行为边界**",
            "3. 如有伏笔需要 → recall_foreshadowing",
            "4. 写本节内容（3-6 个 StoryFragment JSON）",
            "5. 自然终止 —— 不要嵌套调用工具",
            "",
            "## 输出格式",
            "以 StoryFragment JSON 格式逐行输出:",
            '  {"type": "narration", "text": "旁白/叙述文本..."}',
            '  {"type": "dialogue", "character": "角色名", "text": "对话内容..."}',
            '  {"type": "action", "character": "角色名", "text": "动作描写..."}',
            '  {"type": "inner_thought", "character": "角色名", "text": "内心独白..."}',
            '  {"type": "divider", "text": "", "divider_label": "时间/地点标签"}',
            "",
            "## 规则",
            "1. dialogue/action/inner_thought 的 character 必须用原文中的准确角色名",
            "2. 对话和动作交替推进，不要连续输出太长的 narration",
            "3. 保持原作叙事风格和角色性格一致性",
            "4. 写角色之前必须 lookup_character 确认其 Voice 和边界",
            "5. 每节写 3-6 段片段即可，不要一节写太多",
        ]

        # 注入角色生死状态（硬约束 —— Push）
        if self._character_statuses:
            dead_chars = [n for n, s in self._character_statuses.items()
                          if s in ("dead", "deceased", "killed")]
            missing_chars = [n for n, s in self._character_statuses.items()
                             if s == "missing"]
            if dead_chars:
                parts.append(f"\n## ⚠️ 已死亡角色: {', '.join(dead_chars)}")
                parts.append("只能以回忆/闪回/他人提及出现，绝不能写他们的对话或动作。")
            if missing_chars:
                parts.append(f"\n## ⚠️ 下落不明角色: {', '.join(missing_chars)}")
                parts.append("不能直接出场，只能通过线索/回忆间接涉及。")

        # 注入文风概要（轻量 Push —— 只给核心标签）
        if self._style_profile:
            parts.append("\n" + self._style_profile.summary())
            exemplars_text = self._style_profile.exemplars_text()
            if exemplars_text:
                parts.append("\n" + exemplars_text)

        return "\n".join(parts)

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

        # 构建 user prompt（本章上下文 + 本节信息）
        task = self._build_user_prompt(section, section_index)
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

            # Voice 约束 + 行为边界（按需 Pull，不从 system prompt 推送）
            profile = char_profiles.get(name)
            if profile and hasattr(profile, 'voice') and profile.voice:
                v = profile.voice
                if v.summary:
                    lines.append(f"Voice: {v.summary}")
                if v.taboo_words:
                    lines.append(f"禁用词: {', '.join(v.taboo_words)}")
            if profile and hasattr(profile, 'boundary') and profile.boundary:
                b = profile.boundary
                if b.hard_rules:
                    lines.append(f"硬底线: {', '.join(b.hard_rules[:5])}")

            return "\n".join(lines)

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

        @tool
        def write_section(section_name: str, goal: str) -> str:
            """写一个章节段落。

            根据大纲中本节的 goal 来写。每节 3-6 个 StoryFragment。
            写完后自然结束（不要再嵌套调用工具）。

            Args:
                section_name: 段落名（opening/rising/climax/hook）
                goal: 本节要完成的叙事目标

            Returns:
                已写的片段数量
            """
            # 这个方法只是一个标记——AgentFlow LLM 会在自然终止时输出内容
            # 实际的片段解析由 _on_post_turn 完成
            return f"开始写 {section_name}: {goal[:100]}"

        return [
            lookup_character,
            recall_foreshadowing,
            write_section,
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

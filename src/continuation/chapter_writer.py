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

        # 运行时上下文
        self._outline: dict = {}
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
        outline: dict,
        style_profile,
        previous_chapter_ending: str,
        character_profiles: dict,
        character_statuses: dict = None,
        graph=None,
    ):
        """设置 Writer 运行时上下文，构建 System Prompt。"""
        self._outline = outline
        self._style_profile = style_profile
        self._previous_chapter_ending = previous_chapter_ending
        self._character_profiles = character_profiles or {}
        self._character_statuses = character_statuses or {}
        self._graph = graph

        # 构建 system prompt（会被 AgentFlow 缓存，不随每轮对话变化）
        self.set_identity(self._build_system_prompt())

    def _build_system_prompt(self) -> str:
        """构建 Writer 的 system prompt。"""
        parts = [
            "## 角色",
            "你是专业小说续写者。你需要根据大纲逐节续写小说内容。",
            "每节写完后，检查是否需要 lookup_character 或 recall_foreshadowing，",
            "然后再写下一节。不要一次性写完所有内容。",
            "",
            "## 工作流程",
            "1. 阅读大纲 → 理解本节目标",
            "2. 如果有不熟悉的角色 → lookup_character",
            "3. 如果需要伏笔参考 → recall_foreshadowing",
            "4. write_section 写本节内容",
            "5. 重复 1-4 直到全部完成",
            "6. 自然终止（不要调用工具，直接输出完成摘要）",
            "",
            "## 输出格式",
            "write_section 工具输出的 text 字段直接以 StoryFragment JSON 格式逐行输出:",
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
            "4. 每节写 3-6 段片段即可，不要一节写太多",
        ]

        # 注入文风约束
        if self._style_profile:
            parts.append("\n" + self._style_profile.summary())
            exemplars_text = self._style_profile.exemplars_text()
            if exemplars_text:
                parts.append("\n" + exemplars_text)

        # 注入角色生死状态（硬约束）
        if self._character_statuses:
            dead_chars = [n for n, s in self._character_statuses.items()
                          if s in ("dead", "deceased", "killed")]
            missing_chars = [n for n, s in self._character_statuses.items()
                             if s == "missing"]
            if dead_chars:
                parts.append(f"\n## ⚠️ 已死亡角色: {', '.join(dead_chars)}")
                parts.append("只能以回忆/闪回/他人提及出现。")
            if missing_chars:
                parts.append(f"\n## ⚠️ 下落不明角色: {', '.join(missing_chars)}")

        # 注入角色行为约束
        if self._character_profiles:
            parts.append("\n## 角色行为约束")
            for name, profile in self._character_profiles.items():
                parts.append(f"\n### {name}")
                if hasattr(profile, 'voice') and profile.voice:
                    v = profile.voice
                    if v.summary:
                        parts.append(f"- Voice: {v.summary}")
                    if v.taboo_words:
                        parts.append(f"- 禁用词: {', '.join(v.taboo_words)}")
                if hasattr(profile, 'boundary') and profile.boundary:
                    b = profile.boundary
                    if b.hard_rules:
                        parts.append(f"- 硬底线: {', '.join(b.hard_rules[:3])}")

        return "\n".join(parts)

    # ================================================================
    # 流式接口（供 Pipeline 调用）
    # ================================================================

    async def inject(self, instruction: str):
        """注入用户指令。触发 AgentFlow 中断。"""
        self._inject_instruction = instruction
        self._inject_event.set()

    async def stream(self, outline: dict):
        """流式生成章节内容。

        AgentFlow ReAct 循环逐节写作，每节通过工具产出片段。
        Pipeline 通过此 async generator 获取片段流。

        Yields:
            StoryFragment
        """
        from .fragment import StoryFragment

        # 构建 user prompt
        task = self._build_user_prompt(outline)
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

    def _build_user_prompt(self, outline: dict) -> str:
        """构建 user prompt（大纲 + 前一章结尾）。"""
        parts = [
            "## 续写任务",
            f"章节: 第 {outline.get('chapter_number', '?')} 章「{outline.get('title', '')}」",
            f"梗概: {outline.get('synopsis', '')}",
        ]

        structure = outline.get("structure", {})
        if structure:
            parts.append("\n## 章节结构（逐节写作）")
            parts.append(f"第一节 - opening: {structure.get('opening', '')}")
            parts.append(f"第二节 - rising: {structure.get('rising', '')}")
            parts.append(f"第三节 - climax: {structure.get('climax', '')}")
            parts.append(f"第四节 - hook: {structure.get('hook', '')}")

        parts.append("\n按节点顺序逐节写作。每写完一节后检查是否需要查询角色信息。")

        if self._previous_chapter_ending:
            ending = self._previous_chapter_ending
            parts.append(f"\n## 前一章结尾（叙事衔接）")
            parts.append(ending[-1500:] if len(ending) > 1500 else ending)

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

            # Voice 约束
            profile = char_profiles.get(name)
            if profile and hasattr(profile, 'voice') and profile.voice:
                v = profile.voice
                if v.summary:
                    lines.append(f"Voice: {v.summary}")

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

    def _on_post_turn(self, user_msg: str, assistant_msg: str):
        """从 AgentFlow 自然终止输出中解析 StoryFragment。"""
        from .fragment import StoryFragment

        if not assistant_msg:
            return

        # AgentFlow 自然终止时，assistant_msg 是 LLM 直接输出的文本
        # 尝试按行解析 JSON fragment
        for line in assistant_msg.strip().split("\n"):
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            fragment = StoryFragment.parse_stream_line(line)
            if fragment:
                # 同步放入队列（_on_post_turn 在 run 内同步调用）
                try:
                    self._fragment_queue.put_nowait(fragment)
                except asyncio.QueueFull:
                    pass

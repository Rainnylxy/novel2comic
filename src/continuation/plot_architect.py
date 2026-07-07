# -*- coding: utf-8 -*-
"""PlotArchitect —— 剧情架构师 Agent。

继承 BaseAgent，通过 ReAct 循环管理:
  - 伏笔分析 (analyze_hanging_threads)
  - 角色节拍规划 (sketch_character_beats)
  - 章节结构设计 (plan_structure)

输入: KG 上下文 + 上一章结尾 + 用户指令 + 文风 Profile
输出: 章节大纲 JSON
"""

import json
import logging
from typing import TYPE_CHECKING, Optional

from agentflow.runtime.toolkit import tool

logger = logging.getLogger(__name__)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[%(name)s] %(levelname)s: %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

from ..agents.base_agent import BaseAgent

if TYPE_CHECKING:
    from ..context import GlobalContext, ServiceRegistry
    from ..llm import UnifiedLLM
    from .author_style_profile import AuthorStyleProfile


class PlotArchitect(BaseAgent):
    """剧情架构师 Agent。

    继承 BaseAgent，通过 skill 文件 + 动态前缀注入上下文。
    3 个 ReAct 工具：analyze_hanging_threads, sketch_character_beats, plan_structure。
    """

    SKILL_NAME = "plot_architect"

    def __init__(self, ctx, services, llm, memory=None):
        super().__init__(ctx, services, llm, memory)
        self._kg = services.kg
        # 运行时注入的上下文（由 pipeline 在 run 前设置）
        self._outline_context: dict = {}

    def set_context(
        self,
        previous_chapter_ending: str,
        character_profiles: dict,
        last_chapter: int,
        style_profile: Optional["AuthorStyleProfile"] = None,
        user_instruction: str = "",
        character_statuses: dict = None,
    ):
        """设置 Plot Architect 的运行时上下文。

        在 Agent 构建前由 Pipeline 调用。

        Args:
            previous_chapter_ending: 前一章结尾原文（~3000字）
            character_profiles: {name: CharacterProfile} 角色蒸馏 Profile
            last_chapter: 当前最后一章的章节号
            style_profile: AuthorStyleProfile
            user_instruction: 用户的初始指令（可选）
            character_statuses: {name: status} 角色生死状态
        """
        self._outline_context = {
            "previous_chapter_ending": previous_chapter_ending,
            "style_summary": style_profile.summary() if style_profile else "",
            "character_profiles": character_profiles,
            "last_chapter": last_chapter,
            "user_instruction": user_instruction,
            "character_statuses": character_statuses or {},
        }
        self._needs_rebuild = True

    def _build_dynamic_prefix(self) -> str:
        """构建注入 user message 的动态前缀。

        放在 system prompt 缓存之外，包含变化的数据（前一章结尾等）。
        """
        ctx = self._outline_context
        if not ctx:
            return ""

        lines = [
            f"## 续写上下文",
            f"原文共 {ctx['last_chapter']} 章，故事已完整。你要从第 {ctx['last_chapter'] + 1} 章开始创作全新的故事。",
            f"基于原文结尾和 KG 中的角色状态/伏笔，设计一个 3-5 章的新故事弧线。",
        ]

        if ctx.get("user_instruction"):
            lines.append(f"\n用户指令: {ctx['user_instruction']}")

        # 角色生死状态（硬约束）
        char_statuses = ctx.get("character_statuses", {})
        dead_chars = [n for n, s in char_statuses.items()
                      if s in ("dead", "deceased", "killed")]
        if dead_chars:
            lines.append(f"\n⚠️ 以下角色已死亡，绝不能在新章节中以存活状态出场: {', '.join(dead_chars)}")
            lines.append("只能以回忆、闪回、他人提及的方式出现。")

        if ctx.get("style_summary"):
            lines.append(f"\n{ctx['style_summary']}")

        # 角色 Profile 摘要
        char_profiles = ctx.get("character_profiles", {})
        if char_profiles:
            lines.append("\n## 主要角色约束")
            for name, profile in char_profiles.items():
                lines.append(f"\n### {name}")
                if hasattr(profile, 'voice') and profile.voice:
                    v = profile.voice
                    lines.append(f"- Voice: {v.summary or '无'}")
                if hasattr(profile, 'boundary') and profile.boundary:
                    b = profile.boundary
                    if b.hard_rules:
                        lines.append(f"- 硬底线: {', '.join(b.hard_rules[:3])}")

        lines.append(f"\n## 前一章结尾（叙事衔接）")
        ending = ctx.get("previous_chapter_ending", "")
        lines.append(ending[-3000:] if len(ending) > 3000 else ending)

        return "\n".join(lines)

    async def run(self, task: str = ""):
        """运行 Plot Architect 的 ReAct 循环。"""
        prefix = self._build_dynamic_prefix()
        if prefix:
            if task:
                task = prefix + "\n\n" + task
            else:
                task = prefix + "\n\n任务: 规划一个 3-5 章的故事弧线。依次调用 analyze_hanging_threads → sketch_character_beats → plan_arc。"
        result = await super().run(task)
        # Critical 1: AgentFlow 返回原始 str，契约要求 dict
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            try:
                return json.loads(result)
            except (json.JSONDecodeError, TypeError):
                pass
        # 兜底返回
        last_ch = self._outline_context.get("last_chapter", 0)
        return {
            "chapter_number": last_ch + 1,
            "title": "续",
            "synopsis": "继续推进故事",
            "structure": {
                "opening": "衔接上一章结尾",
                "rising": "推进现有冲突",
                "climax": "关键转折",
                "hook": "悬念钩子",
            },
            "plot_threads_advanced": [],
            "plot_threads_introduced": [],
            "tone": "保持原作风格",
            "target_word_count": 3000,
            "status": "ok",
        }

    def _build_tools(self) -> list:
        ctx = self._ctx
        kg = self._kg
        llm = self._llm
        outline_ctx = self._outline_context

        @tool
        def analyze_hanging_threads() -> str:
            """从知识图谱中提取所有未解决的伏笔和活跃冲突。

            查询 KG 的因果关系链，找出 effect 尚未在已覆盖章节中实现的事件。
            同时提取敌对角色的未解决冲突。

            Returns:
                JSON 格式的伏笔和冲突列表
            """
            if ctx.novel is None:
                return json.dumps({"hanging_threads": [], "active_conflicts": [],
                                   "message": "小说上下文未加载"}, ensure_ascii=False)
            graph = ctx.novel.story_graph
            if not graph:
                return json.dumps({"hanging_threads": [], "active_conflicts": [],
                                   "message": "KG 不可用"}, ensure_ascii=False)

            last_ch = outline_ctx.get("last_chapter", 0)

            # 因果链中的未解决事件
            hanging = []
            for edge in graph.event_relation_edges:
                if edge.relation_type == "causes":
                    ev = graph.get_event_node(edge.from_event.split(":", 1)[-1])
                    if ev:
                        ev_end = ev.chapter_end or ev.chapter_start
                        # 如果事件的 effect 还未在新章节中体现
                        if ev_end <= last_ch and ev.effect:
                            hanging.append({
                                "event": ev.name,
                                "chapter": ev_end,
                                "effect": ev.effect,
                                "status": "pending",
                            })

            # 敌对关系冲突
            conflicts = []
            for pair in kg.enemy_pairs(graph):
                rel = graph.get_relationship_edge(pair[0], pair[1])
                if rel:
                    conflicts.append({
                        "characters": list(pair),
                        "tension": rel.current_tension or "?",
                        "shared_history": rel.shared_history or "",
                    })

            result = json.dumps({
                "hanging_threads": hanging[:10],
                "active_conflicts": conflicts[:5],
                "total_hanging": len(hanging),
            }, ensure_ascii=False)
            logger.info(
                "analyze_hanging_threads: %d 未解决伏笔, %d 活跃冲突",
                len(hanging), len(conflicts),
            )
            return result

        @tool
        def sketch_character_beats(character_names: str) -> str:
            """为主要角色规划本章的情绪弧线和关键行动。

            每个角色需要定义:
            - arc: 本章情绪变化轨迹（如 "从犹豫到决断"）
            - key_action: 本章该角色的关键行动
            - emotional_beat: 关键情感时刻

            Args:
                character_names: 逗号分隔的角色名列表（如 "江停,严峫"）

            Returns:
                JSON 格式的角色节拍
            """
            names = [n.strip() for n in character_names.split(",") if n.strip()]
            # 过滤已死亡角色
            char_statuses = outline_ctx.get("character_statuses", {})
            dead = {n for n, s in char_statuses.items() if s in ("dead", "deceased", "killed")}
            alive_names = [n for n in names if n not in dead]
            if len(alive_names) < len(names):
                skipped = set(names) - set(alive_names)
                logger.warning("sketch_character_beats: 跳过已死亡角色 %s", skipped)
            names = alive_names
            graph = ctx.novel.story_graph if ctx.novel else None

            char_info = {}
            if graph:
                for name in names[:8]:  # 最多 8 个角色
                    person = kg.get_person(graph, name)
                    if person:
                        relations = kg.get_relations(graph, name)
                        char_info[name] = {
                            "role": person.role_type,
                            "importance": person.importance,
                            "status": person.status,
                            "faction": person.faction,
                            "relations": [
                                {
                                    "with": r.to_char if r.from_char == name else r.from_char,
                                    "type": r.relation_type,
                                    "intimacy": r.intimacy,
                                }
                                for r in relations[:5]
                            ],
                        }

            # 使用 LLM 规划节拍
            try:
                result = llm.chat_json(
                    system_prompt="你是专业剧情规划师。为每个角色设计本章的情绪弧线和关键行动。只返回 JSON。",
                    user_prompt=(
                        f"角色信息:\n{json.dumps(char_info, ensure_ascii=False, indent=2)}\n\n"
                        f"规划 {len(names)} 个角色在本章的情绪变化轨迹和关键行动。\n"
                        f"返回 JSON: {{characters: {{角色名: {{arc, key_action, emotional_beat}}}} }}"
                    ),
                    temperature=0.5,
                    max_tokens=2048,
                )
                if isinstance(result, dict):
                    chars = result.get("characters", result)
                    arcs = {n: c.get("arc", "?") for n, c in chars.items()} if isinstance(chars, dict) else {}
                    logger.info("sketch_character_beats: %s", arcs)
                    return json.dumps(result, ensure_ascii=False)
            except Exception:
                pass

            # Fallback
            logger.warning("sketch_character_beats: LLM 失败，使用兜底")
            fallback = {
                name: {"arc": "持续推进", "key_action": "参与关键事件",
                       "emotional_beat": "对事件做出反应"}
                for name in names
            }
            return json.dumps({"characters": fallback}, ensure_ascii=False)

        @tool
        def plan_arc(arc_spec: str) -> str:
            """规划 3-5 章的故事弧线，每章包含 3-6 个小节。

            基于伏笔分析和角色节拍，设计连贯的多章叙事弧线。
            每章有独立的主题，共同推进一个完整的故事段。

            Args:
                arc_spec: 角色节拍和伏笔分析的 JSON 摘要

            Returns:
                故事弧线 JSON（多章，每章多节）
            """
            style = outline_ctx.get("style_summary", "")
            prev_ending = outline_ctx.get("previous_chapter_ending", "")
            instruction = outline_ctx.get("user_instruction", "")
            last_ch = outline_ctx.get("last_chapter", 0)

            try:
                result = llm.chat_json(
                    system_prompt=(
                        "你是专业的续写故事架构师。原文已经完结，你需要创作全新的后续故事。"
                        "基于原文中的伏笔和角色现状，向前推进时间线，设计一个 3-5 章的新故事弧线。"
                        "每章有独立主题，每章拆分为 3-6 个小节供 Writer 逐节写作。"
                        "只返回 JSON。"
                    ),
                    user_prompt=(
                        f"## 文风约束\n{style}\n\n"
                        f"## 角色节拍 & 伏笔\n{arc_spec}\n\n"
                        f"## 原文结尾\n{prev_ending[-1500:]}\n\n"
                        + (f"## 用户指令\n{instruction}\n\n" if instruction else "")
                        + f"从第 {last_ch + 1} 章开始，规划 3-5 章。返回 JSON:\n"
                          f'{{\n'
                          f'  "arc_title": "故事弧线名",\n'
                          f'  "arc_synopsis": "整个弧线的梗概（100字内）",\n'
                          f'  "chapters": [\n'
                          f'    {{\n'
                          f'      "chapter_number": {last_ch + 1},\n'
                          f'      "title": "章节标题",\n'
                          f'      "synopsis": "本章梗概（50字内）",\n'
                          f'      "tone": "冷峻/紧张/悬疑...",\n'
                          f'      "sections": [\n'
                          f'        {{"name": "opening", "goal": "本节目标（30字内）", '
                          f'"characters": ["角色名"], "key_beats": ["情节点"], '
                          f'"target_fragments": 5}},\n'
                          f'        ...\n'
                          f'      ]\n'
                          f'    }},\n'
                          f'    ...\n'
                          f'  ],\n'
                          f'  "plot_threads_advanced": ["推进的伏笔"],\n'
                          f'  "plot_threads_introduced": ["新引入的悬念"]\n'
                          f'}}'
                    ),
                    temperature=0.6,
                    max_tokens=2048,
                )
                if isinstance(result, dict):
                    chapters = result.get("chapters", [])
                    ch_titles = [f"第{c.get('chapter_number','?')}章「{c.get('title','?')}」"
                                 for c in chapters]
                    logger.info("plan_arc: %s — %s",
                                result.get("arc_title", "?"), ", ".join(ch_titles))
                    result.setdefault("chapter_number", last_ch + 1)
                    result.setdefault("status", "ok")
                    return json.dumps(result, ensure_ascii=False)
            except Exception as e:
                logger.warning("plan_arc LLM 调用失败，使用兜底方案: %s", e)

            return json.dumps({
                "arc_title": "续",
                "arc_synopsis": "继续推进故事",
                "chapters": [{
                    "chapter_number": last_ch + 1,
                    "title": "续",
                    "synopsis": "继续推进故事",
                    "tone": "保持原作风格",
                    "sections": [
                        {"name": "opening", "goal": "衔接上一章结尾",
                         "characters": [], "key_beats": ["开场"], "target_fragments": 5},
                        {"name": "rising", "goal": "推进冲突",
                         "characters": [], "key_beats": ["推进"], "target_fragments": 6},
                        {"name": "climax", "goal": "关键转折",
                         "characters": [], "key_beats": ["高潮"], "target_fragments": 5},
                        {"name": "hook", "goal": "章尾悬念",
                         "characters": [], "key_beats": ["悬念"], "target_fragments": 3},
                    ],
                }],
                "plot_threads_advanced": [],
                "plot_threads_introduced": [],
                "status": "ok",
            }, ensure_ascii=False)

        return [
            analyze_hanging_threads,
            sketch_character_beats,
            plan_arc,
        ]

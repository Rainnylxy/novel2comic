# -*- coding: utf-8 -*-
"""ConsistencyReviewer —— 一致性审校 Agent。

继承 BaseAgent，通过 ReAct 循环检查草稿的一致性:
  - 角色 OOC (check_character_consistency)
  - 时间线 (check_timeline)
  - 设定矛盾 (check_setting_consistency)
"""

import json
import logging
from typing import TYPE_CHECKING

from agentflow.runtime.toolkit import tool

from ..agents.base_agent import BaseAgent

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..context import GlobalContext, ServiceRegistry
    from ..llm import UnifiedLLM


class ConsistencyReviewer(BaseAgent):
    """一致性审校 Agent。

    对照 KG + 角色 Profile 检查草稿的一致性。
    """

    SKILL_NAME = "consistency_reviewer"

    def __init__(self, ctx, services, llm, memory=None):
        super().__init__(ctx, services, llm, memory)
        self._kg = services.kg
        self._draft_fragments: list = []
        self._character_profiles: dict = {}
        self._style_profile = None

    def set_context(self, draft_fragments: list, character_profiles: dict,
                    style_profile=None):
        """设置审校上下文。"""
        self._draft_fragments = draft_fragments
        self._character_profiles = character_profiles
        self._style_profile = style_profile

    def _build_dynamic_prefix(self) -> str:
        """构建动态前缀。"""
        draft_text = "\n".join(
            f"[{i}] [{f.type}] " + (f"{f.character}: " if f.character else "") + f.text
            for i, f in enumerate(self._draft_fragments)
        )
        return (f"## 待审校草稿\n{draft_text[:6000]}\n\n"
                f"请依次调用 check_character_consistency -> check_timeline -> check_setting_consistency")

    async def run(self, task: str = ""):
        prefix = self._build_dynamic_prefix()
        if prefix:
            if task:
                task = prefix + "\n\n" + task
            else:
                task = prefix
        return await super().run(task)

    def _build_tools(self) -> list:
        ctx = self._ctx
        kg = self._kg
        llm = self._llm
        fragments = self._draft_fragments
        char_profiles = self._character_profiles

        @tool
        def check_character_consistency(draft_text: str = "") -> str:
            """检查草稿中角色是否 OOC。

            对照每个角色的 Voice 和 Boundary，检查对话风格和行为是否一致。

            Returns:
                JSON 格式的 OOC 问题列表
            """
            if not char_profiles:
                return json.dumps({"issues": [], "message": "无角色 Profile 数据"}, ensure_ascii=False)

            # 构建角色约束摘要
            char_specs = {}
            for name, profile in char_profiles.items():
                spec = {}
                if hasattr(profile, 'voice') and profile.voice:
                    spec["voice_summary"] = profile.voice.summary or ""
                    spec["taboo_words"] = profile.voice.taboo_words or []
                if hasattr(profile, 'boundary') and profile.boundary:
                    spec["hard_rules"] = profile.boundary.hard_rules or []
                char_specs[name] = spec

            # 提取草稿中角色对话
            char_dialogues = {}
            for i, f in enumerate(fragments):
                if f.type in ("dialogue", "inner_thought", "action") and f.character:
                    char_dialogues.setdefault(f.character, []).append(
                        f"[{i}] [{f.type}] {f.text}"
                    )

            if not char_dialogues:
                return json.dumps({"issues": []}, ensure_ascii=False)

            try:
                result = llm.chat_json(
                    system_prompt="你是角色一致性检查器。检查角色的对话/行为是否与其设定一致。只返回 JSON。",
                    user_prompt=(
                        f"## 角色设定\n{json.dumps(char_specs, ensure_ascii=False, indent=2)}\n\n"
                        f"## 草稿中的角色表现\n{json.dumps(char_dialogues, ensure_ascii=False, indent=2)}\n\n"
                        f"检查每个角色是否 OOC，返回 JSON:\n"
                        f'{{"issues": [{{"type": "character_ooc", "severity": "medium", '
                        f'"location": "片段序号", "character": "角色名", '
                        f'"description": "问题描述", "suggestion": "修改建议"}}]}}'
                    ),
                    temperature=0.3,
                    max_tokens=2048,
                )
                if isinstance(result, dict):
                    return json.dumps(result, ensure_ascii=False)
            except Exception as e:
                logger.warning("check_character_consistency LLM 调用失败: %s", e)

            return json.dumps({"issues": []}, ensure_ascii=False)

        @tool
        def check_timeline(draft_text: str = "") -> str:
            """检查草稿中的时间线是否与 KG 中的事件顺序一致。

            Returns:
                JSON 格式的时间线问题列表
            """
            graph = ctx.novel.story_graph if ctx.novel else None
            if not graph:
                return json.dumps({"issues": [], "message": "KG 不可用"}, ensure_ascii=False)

            # 提取 KG 中最近的事件时间线
            events = graph.event_nodes
            timeline = [
                {"name": ev.name, "chapter_start": ev.chapter_start,
                 "chapter_end": ev.chapter_end or ev.chapter_start}
                for ev in events[-20:]
            ]

            draft_snippet = "\n".join(
                f"[{i}] [{f.type}] {f.text[:100]}"
                for i, f in enumerate(fragments[:30])
            )

            try:
                result = llm.chat_json(
                    system_prompt="你是时间线一致性检查器。检查续写内容是否与已有事件时间线矛盾。只返回 JSON。",
                    user_prompt=(
                        f"## 已有事件时间线\n{json.dumps(timeline, ensure_ascii=False, indent=2)}\n\n"
                        f"## 草稿内容\n{draft_snippet}\n\n"
                        f"检查草稿中是否出现时间线矛盾（如: 已死角色出现、事件顺序颠倒）。"
                        f"返回 JSON: {{\"issues\": [...]}}"
                    ),
                    temperature=0.2,
                    max_tokens=1024,
                )
                if isinstance(result, dict):
                    return json.dumps(result, ensure_ascii=False)
            except Exception as e:
                logger.warning("check_timeline LLM 调用失败: %s", e)

            return json.dumps({"issues": []}, ensure_ascii=False)

        @tool
        def check_setting_consistency(draft_text: str = "") -> str:
            """检查草稿是否与已有设定矛盾。

            Returns:
                JSON 格式的设定问题列表
            """
            graph = ctx.novel.story_graph if ctx.novel else None
            if not graph:
                return json.dumps({"issues": []}, ensure_ascii=False)

            # 提取关键设定: 角色状态、组织从属、地点关系
            persons = kg.get_all_persons(graph)[:15]
            settings = [
                {"name": p.name, "status": p.status, "faction": p.faction,
                 "importance": p.importance}
                for p in persons
            ]

            draft_snippet = "\n".join(
                f"[{i}] [{f.type}] {f.text[:150]}"
                for i, f in enumerate(fragments[:30])
            )

            try:
                result = llm.chat_json(
                    system_prompt="你是设定一致性检查器。检查续写内容是否与已有设定矛盾。只返回 JSON。",
                    user_prompt=(
                        f"## 已有设定\n{json.dumps(settings, ensure_ascii=False, indent=2)}\n\n"
                        f"## 草稿内容\n{draft_snippet}\n\n"
                        f"检查是否有设定矛盾。返回 JSON: {{\"issues\": [...]}}"
                    ),
                    temperature=0.2,
                    max_tokens=1024,
                )
                if isinstance(result, dict):
                    return json.dumps(result, ensure_ascii=False)
            except Exception as e:
                logger.warning("check_setting_consistency LLM 调用失败: %s", e)

            return json.dumps({"issues": []}, ensure_ascii=False)

        return [
            check_character_consistency,
            check_timeline,
            check_setting_consistency,
        ]
